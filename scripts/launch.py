#!/usr/bin/env python3
"""OCI Always Free 自動リトライ起動スクリプト.

GitHub Actions の cron (5分毎) で実行され、VM.Standard.A1.Flex (ARM Ampere)
インスタンスの起動を試行する。リソース枯渇 ("Out of host capacity") による
失敗はサイレントに exit 0 し次回 cron を待つ。成功時は state.json を更新し
Discord 通知した上で exit 0 する。

全ての OCID / 認証情報 / 設定値は環境変数経由で取得する (ハードコード禁止)。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# logging (print は使わない)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("oci-retry")


# ---------------------------------------------------------------------------
# 環境変数取得 (未設定なら即エラー終了)
# ---------------------------------------------------------------------------
def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        logger.error("必須環境変数 %s が未設定です。GitHub Secrets を確認してください。", name)
        sys.exit(1)
    return value


def _optional_env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


# ---------------------------------------------------------------------------
# state.json 読み書き
# ---------------------------------------------------------------------------
def state_path() -> Path:
    """state.json のパスを返す (working directory = apps/oci-freenium-retry/)."""
    return Path.cwd() / "state.json"


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        logger.info("state.json が存在しません。初回実行として続行します。")
        return _fresh_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("state.json の読み込みに失敗しました (%s)。初期状態で続行します。", exc)
        return _fresh_state()
    if not isinstance(data, dict):
        logger.warning("state.json のフォーマットが不正です。初期状態で続行します。")
        return _fresh_state()
    return data


def _fresh_state() -> dict[str, Any]:
    return {
        "success": False,
        "instance_ocid": None,
        "public_ip": None,
        "created_at": None,
        "last_attempt_at": None,
        "last_error": None,
        "attempt_count": 0,
    }


def save_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logger.info("state.json を更新しました: %s", path)


# ---------------------------------------------------------------------------
# Discord 通知
# ---------------------------------------------------------------------------
def discord_notify(embed: dict[str, Any]) -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logger.info("DISCORD_WEBHOOK_URL 未設定のため通知をスキップします。")
        return
    payload = {"embeds": [embed]}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        if resp.status_code >= 400:
            logger.warning("Discord 通知が失敗しました (status=%s body=%s)", resp.status_code, resp.text[:200])
        else:
            logger.info("Discord 通知を送信しました。")
    except requests.RequestException as exc:
        logger.warning("Discord 通知で例外が発生しました: %s", exc)


def notify_success(
    display_name: str,
    instance_ocid: str,
    public_ip: str | None,
    region: str,
    ad: str,
    ocpus: str,
    memory_gb: str,
    created_at: str,
    already_exists: bool,
) -> None:
    title = "✅ OCI Instance Already Exists" if already_exists else "🎉 OCI Instance Launched!"
    embed = {
        "title": title,
        "color": 0x00B050 if already_exists else 0x57F287,
        "fields": [
            {"name": "display_name", "value": display_name, "inline": False},
            {"name": "instance_ocid", "value": instance_ocid, "inline": False},
            {"name": "public_ip", "value": public_ip or "(取得失敗)", "inline": True},
            {"name": "region", "value": region, "inline": True},
            {"name": "availability_domain", "value": ad, "inline": True},
            {"name": "ocpus", "value": ocpus, "inline": True},
            {"name": "memory_gb", "value": memory_gb, "inline": True},
            {"name": "created_at", "value": created_at, "inline": False},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    discord_notify(embed)


def notify_error(code: str, message: str, status: str | int) -> None:
    embed = {
        "title": "❌ OCI Launch Error",
        "color": 0xED4245,
        "fields": [
            {"name": "code", "value": code, "inline": True},
            {"name": "status", "value": str(status), "inline": True},
            {"name": "message", "value": message[:1000], "inline": False},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    discord_notify(embed)


# ---------------------------------------------------------------------------
# OCI クライアント構築
# ---------------------------------------------------------------------------
def build_oci_config() -> dict[str, Any]:
    tenancy = _require_env("OCI_TENANCY_OCID")
    user = _require_env("OCI_USER_OCID")
    fingerprint = _require_env("OCI_API_KEY_FINGERPRINT")
    region = _require_env("OCI_REGION")
    key_content = os.environ.get("OCI_API_KEY_CONTENT")
    key_file = os.environ.get("OCI_API_KEY_FILE")

    config: dict[str, Any] = {
        "user": user,
        "tenancy": tenancy,
        "fingerprint": fingerprint,
        "region": region,
    }

    if key_content:
        # PEM 内容を一時ファイルに書き出す (CI 環境では内容を Secret として渡す)
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False, encoding="utf-8")
        tmp.write(key_content)
        tmp.flush()
        tmp.close()
        os.chmod(tmp.name, 0o600)
        config["key_file"] = tmp.name
        logger.info("OCI API キーを一時ファイルに書き出しました: %s", tmp.name)
    elif key_file:
        if not Path(key_file).exists():
            logger.error("OCI_API_KEY_FILE で指定されたファイルが存在しません: %s", key_file)
            sys.exit(1)
        config["key_file"] = key_file
    else:
        logger.error("OCI_API_KEY_CONTENT または OCI_API_KEY_FILE のいずれかを設定してください。")
        sys.exit(1)

    return config


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------
def main() -> int:
    # 設定値 (全て環境変数経由)
    tenancy_ocid = _require_env("OCI_TENANCY_OCID")
    compartment_ocid = _require_env("OCI_COMPARTMENT_OCID")
    region = _require_env("OCI_REGION")
    subnet_ocid = _require_env("OCI_SUBNET_OCID")
    ssh_public_key = _require_env("OCI_SSH_PUBLIC_KEY")

    shape = _optional_env("OCI_SHAPE", "VM.Standard.A1.Flex")
    ocpus = _optional_env("OCI_OCPUS", "2")
    memory_gb = _optional_env("OCI_MEMORY_GB", "12")
    display_name = _optional_env("OCI_DISPLAY_NAME", "auto-retry-freenium")
    os_name = _optional_env("OCI_OS", "Canonical Ubuntu")
    os_version = _optional_env("OCI_OS_VERSION", "24.04")
    arch = _optional_env("OCI_ARCH", "ARM")
    boot_volume_size_gb = int(_optional_env("OCI_BOOT_VOLUME_SIZE_GB", "50"))

    # state.json 読み込み
    state = load_state()

    # 1. 既に成功済みならスキップ
    if state.get("success") is True:
        logger.info("state.json が success=true のためスキップします。")
        return 0

    # OCI SDK import (遅延 import で env 未設定時の早期 exit を優先)
    try:
        import oci
        from oci.core.models import (  # type: ignore
            CreateVnicDetails,
            InstanceSourceViaImageDetails,
            LaunchInstanceDetails,
            LaunchInstanceShapeConfigDetails,
        )
        from oci.exceptions import ServiceError
    except ImportError as exc:
        logger.error("oci SDK の import に失敗しました: %s", exc)
        return 1

    config = build_oci_config()
    identity_client = oci.identity.IdentityClient(config)
    compute_client = oci.core.ComputeClient(config)
    network_client = oci.core.VirtualNetworkClient(config)

    state["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
    state["attempt_count"] = int(state.get("attempt_count", 0)) + 1

    # 2. 既存インスタンス検索 (display_name 一致 + PROVISIONING/RUNNING/STARTING)
    try:
        existing = _find_existing_instance(compute_client, compartment_ocid, display_name)
    except ServiceError as exc:
        logger.error("既存インスタンス検索で ServiceError: code=%s status=%s message=%s", exc.code, exc.status, exc.message)
        state["last_error"] = f"{exc.code}: {exc.message}"
        save_state(state)
        notify_error(exc.code, exc.message, exc.status)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error("既存インスタンス検索で例外: %s", exc)
        state["last_error"] = str(exc)
        save_state(state)
        return 1

    if existing is not None:
        logger.info("既存インスタンスを発見しました (id=%s state=%s)。", existing.id, existing.lifecycle_state)
        public_ip = _get_public_ip(compute_client, network_client, compartment_ocid, existing.id)
        now = datetime.now(timezone.utc).isoformat()
        state.update(
            {
                "success": True,
                "instance_ocid": existing.id,
                "public_ip": public_ip,
                "created_at": existing.time_created.isoformat() if existing.time_created else now,
                "last_error": None,
            }
        )
        save_state(state)
        notify_success(
            display_name=display_name,
            instance_ocid=existing.id,
            public_ip=public_ip,
            region=region,
            ad=existing.availability_domain or "(unknown)",
            ocpus=ocpus,
            memory_gb=memory_gb,
            created_at=state["created_at"],
            already_exists=True,
        )
        return 0

    # 4. Image OCID 検索 (ハードコード厳禁: list_images で最新 aarch64 イメージ取得)
    try:
        image_id = _find_latest_image(
            compute_client,
            tenancy_ocid,
            os_name,
            os_version,
            shape,
            arch,
        )
    except ServiceError as exc:
        logger.error("イメージ検索で ServiceError: code=%s status=%s message=%s", exc.code, exc.status, exc.message)
        state["last_error"] = f"{exc.code}: {exc.message}"
        save_state(state)
        notify_error(exc.code, exc.message, exc.status)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error("イメージ検索で例外: %s", exc)
        state["last_error"] = str(exc)
        save_state(state)
        return 1

    logger.info("使用イメージ OCID: %s", image_id)

    # 5. AD 列挙
    try:
        ads = identity_client.list_availability_domains(compartment_id=tenancy_ocid).data
    except ServiceError as exc:
        logger.error("AD 列挙で ServiceError: code=%s status=%s message=%s", exc.code, exc.status, exc.message)
        state["last_error"] = f"{exc.code}: {exc.message}"
        save_state(state)
        notify_error(exc.code, exc.message, exc.status)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error("AD 列挙で例外: %s", exc)
        state["last_error"] = str(exc)
        save_state(state)
        return 1

    if not ads:
        logger.error("Availability Domain が1つも取得できませんでした。")
        state["last_error"] = "no availability domains"
        save_state(state)
        return 1

    logger.info("AD リスト: %s", [a.name for a in ads])

    # 6. AD ローテーション試行 (毎回リスト順)
    for ad in ads:
        ad_name = ad.name
        logger.info("AD %s で launch を試行します。", ad_name)
        launch_details = LaunchInstanceDetails(
            availability_domain=ad_name,
            compartment_id=compartment_ocid,
            display_name=display_name,
            shape=shape,
            shape_config=LaunchInstanceShapeConfigDetails(ocpus=float(ocpus), memory_in_gbs=float(memory_gb)),
            source_details=InstanceSourceViaImageDetails(
                image_id=image_id,
                boot_volume_size_in_gbs=boot_volume_size_gb,
            ),
            create_vnic_details=CreateVnicDetails(
                subnet_id=subnet_ocid,
                assign_public_ip=True,
            ),
            metadata={"ssh_authorized_keys": ssh_public_key},
        )

        try:
            response = compute_client.launch_instance(launch_details)
            instance = response.data
            logger.info("launch 成功! instance_ocid=%s", instance.id)

            # public IP 取得 (VNIC が attach されるまで短時間ポーリング)
            public_ip = _get_public_ip(compute_client, network_client, compartment_ocid, instance.id, max_wait=90)
            created_at = instance.time_created.isoformat() if instance.time_created else datetime.now(timezone.utc).isoformat()

            state.update(
                {
                    "success": True,
                    "instance_ocid": instance.id,
                    "public_ip": public_ip,
                    "created_at": created_at,
                    "last_error": None,
                }
            )
            save_state(state)
            notify_success(
                display_name=display_name,
                instance_ocid=instance.id,
                public_ip=public_ip,
                region=region,
                ad=ad_name,
                ocpus=ocpus,
                memory_gb=memory_gb,
                created_at=created_at,
                already_exists=False,
            )
            return 0

        except ServiceError as exc:
            # 8. 例外処理
            msg = exc.message or ""
            code = exc.code or ""
            logger.warning("launch で ServiceError: code=%s status=%s message=%s", code, exc.status, msg)

            # Out of host capacity 系はサイレントに次回 cron 待ち
            if _is_out_of_capacity(code, msg, exc.status):
                logger.info("Out of host capacity と判定。サイレントに次回 cron を待ちます。")
                state["last_error"] = f"{code}: {msg}"
                save_state(state)
                return 0

            # その他の ServiceError は Discord エラー通知して exit 1
            state["last_error"] = f"{code}: {msg}"
            save_state(state)
            notify_error(code, msg, exc.status)
            return 1
        except Exception as exc:  # noqa: BLE001
            logger.error("launch で予期しない例外: %s", exc)
            state["last_error"] = str(exc)
            save_state(state)
            return 1

    # 全 AD で失敗 (Out of capacity 系で抜けてきた場合)
    logger.info("全 AD で launch に失敗しました。次回 cron を待ちます。")
    save_state(state)
    return 0


def _is_out_of_capacity(code: str, message: str, status: Any) -> bool:
    """'Out of host capacity' 系のエラーか判定する."""
    msg_lower = (message or "").lower()
    if code == "InternalError" and "out of host capacity" in msg_lower:
        return True
    if status == 500 and "out of capacity" in msg_lower:
        return True
    if "out of host capacity" in msg_lower:
        return True
    return False


def _find_existing_instance(compute_client: Any, compartment_ocid: str, display_name: str) -> Any | None:
    """display_name 一致 + LIFECYCLE_STATE in (PROVISIONING, RUNNING, STARTING) のインスタンスを返す."""
    active_states = {"PROVISIONING", "RUNNING", "STARTING"}
    instances = compute_client.list_instances(compartment_id=compartment_ocid).data
    for inst in instances:
        if inst.display_name == display_name and inst.lifecycle_state in active_states:
            return inst
    return None


def _find_latest_image(
    compute_client: Any,
    compartment_ocid: str,
    os_name: str,
    os_version: str,
    shape: str,
    arch: str,
) -> str:
    """最新の aarch64 Ubuntu イメージ OCID を list_images で取得する (ハードコード厳禁).

    OCI list_images には architecture パラメータが無いため、shape で ARM 互換イメージを
    絞り込み、結果を OS/バージョン でフィルタした上で最新 (TIMECREATED DESC) を採用する。
    """
    images = compute_client.list_images(
        compartment_id=compartment_ocid,
        operating_system=os_name,
        operating_system_version=os_version,
        shape=shape,
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data

    if not images:
        raise RuntimeError(
            f"指定条件 (os={os_name}, version={os_version}, shape={shape}, arch={arch}) に合致するイメージが見つかりませんでした。"
        )

    # 念のため architecture 互換 (aarch64 / arm64) を含むものを優先
    for img in images:
        # OCI の base_image_public_id や operating_system 等からは arch は直接取れないことが多い。
        # shape=VM.Standard.A1.Flex で絞り込んだ結果は全て aarch64 互換なので先頭を採用する。
        return img.id
    raise RuntimeError("イメージが取得できませんでした。")


def _get_public_ip(
    compute_client: Any,
    network_client: Any,
    compartment_ocid: str,
    instance_id: str,
    max_wait: int = 90,
) -> str | None:
    """VNIC が attach されるまで短時間ポーリングして public IP を取得する."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            vnics = compute_client.list_vnic_attachments(
                compartment_id=compartment_ocid,
                instance_id=instance_id,
            ).data
            for vnic in vnics:
                if not vnic.vnic_id:
                    continue
                vnic_data = network_client.get_vnic(vnic.vnic_id).data
                if vnic_data.public_ip:
                    return vnic_data.public_ip
        except Exception as exc:  # noqa: BLE001
            logger.debug("public IP 取得中の例外 (リトライ継続): %s", exc)
        time.sleep(5)
    logger.warning("public IP を %s 秒以内に取得できませんでした。", max_wait)
    return None


if __name__ == "__main__":
    sys.exit(main())