# OCI Always Free Auto-Retry

OCI (Oracle Cloud Infrastructure) の Always Free 枠で `VM.Standard.A1.Flex` (ARM Ampere) インスタンスを自動で起動するためのツールです。

Availability Domain のリソース枯渇 ("Out of host capacity") で即時起動できない場合、GitHub Actions の cron で **5分毎** に起動を試行し、成功したら停止します。

---

## 🎯 解決する課題

- OCI Always Free の ARM Ampere (VM.Standard.A1.Flex) インスタンスは人気が高く、AD のリソース枯渇で即座には建てられない。
- 手動で「Launch」ボタンを連打するのは非現実的。
- 本ツールは GitHub Actions 上で 5分毎に自動リトライし、リソースが空いた瞬間にインスタンスを確保する。

---

## 📁 ファイル構成

```
oci-freenium-retry/
├── .github/workflows/oci-retry.yml   # GitHub Actions 定義 (cron + workflow_dispatch)
├── scripts/
│   ├── launch.py                      # メインスクリプト
│   └── requirements.txt              # Python 依存 (oci, requests)
├── state.example.json                 # state.json のサンプル
├── .gitignore                         # state.json を git 管理外に
├── README.md                          # このファイル
└── docs/
    └── architecture.md                # Mermaid 構成図 + 詳細設計
```

---

## 🔑 必要な OCI API キーの発行手順

OCI コンソールで API キーを発行し、以下の情報を手元に揃える。

### 1. テナンシ OCID / ユーザ OCID の確認

1. OCI コンソール右上のプロファイル → **Tenancy** を開き、**OCID** をコピーする (`ocid1.tenancy.oc1.....`)。
2. プロファイル → **My profile** → **My profile** を開き、ユーザの **OCID** をコピーする (`ocid1.user.oc1.......`)。

### 2. API キーの追加

1. プロファイル → **My profile** → **API keys** を開く。
2. **Add API key** → **Generate API key pair** をクリック。
3. **Download private key** で PEM ファイルをダウンロード (例: `oci_api_key.pem`)。**このファイルは再発行不可。厳重に保管。**
4. **Add** をクリックすると **Configuration file preview** が表示される。以下の値をメモする:
   - `tenancy` (テナンシ OCID)
   - `user` (ユーザ OCID)
   - `fingerprint` (API キーのフィンガープリント)
   - `region` (例: `ap-tokyo-1`)
   - `key_file` (ローカルパス、CI では使用しない)

### 3. フィンガープリントの確認

API キー追加後に表示される `fingerprint` (`xx:xx:xx:...` 形式) を控える。後で Secrets に登録する。

### 4. PEM 秘密鍵の中身を取得

ダウンロードした `.pem` ファイルをテキストエディタで開き、**全行** (`-----BEGIN PRIVATE KEY-----` から `-----END PRIVATE KEY-----` まで) をコピーする。これを GitHub Secret `OCI_API_KEY_CONTENT` に登録する。

> ⚠️ 秘密鍵は絶対に Git リポジトリに commit しないこと。`.gitignore` で `*.pem` を除外済み。

---

## 🔐 GitHub Secrets 一覧

リポジトリの **Settings → Secrets and variables → Actions → New repository secret** で以下を登録する。

| Secret 名 | 値 | 例 |
|---|---|---|
| `OCI_TENANCY_OCID` | テナンシ OCID | `ocid1.tenancy.oc1..aaaa...` |
| `OCI_USER_OCID` | ユーザ OCID | `ocid1.user.oc1..aaaa...` |
| `OCI_COMPARTMENT_OCID` | コンパートメント OCID (ルートならテナンシ OCID と同じ) | `ocid1.tenancy.oc1..aaaa...` |
| `OCI_REGION` | リージョン識別子 | `ap-tokyo-1` |
| `OCI_VCN_OCID` | VCN OCID | `ocid1.vcn.oc1.ap-tokyo-1.aaaa...` |
| `OCI_SUBNET_OCID` | サブネット OCID | `ocid1.subnet.oc1.ap-tokyo-1.aaaa...` |
| `OCI_API_KEY_CONTENT` | PEM 秘密鍵の**全行** | `-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----` |
| `OCI_API_KEY_FINGERPRINT` | API キーのフィンガープリント | `12:34:56:78:...` |
| `OCI_SSH_PUBLIC_KEY` | インスタンスに登録する SSH 公開鍵 | `ssh-ed25519 AAAA... user@host` |
| `DISCORD_WEBHOOK_URL` | Discord Webhook URL (成功/エラー通知用) | `https://discordapp.com/api/webhooks/...` |

### Variables (任意・デフォルト値で良ければ未登録で OK)

**Settings → Secrets and variables → Actions → Variables** タブで登録可能。

| Variable 名 | デフォルト | 説明 |
|---|---|---|
| `OCI_SHAPE` | `VM.Standard.A1.Flex` | インスタンス形状 |
| `OCI_OCPUS` | `2` | OCPU 数 |
| `OCI_MEMORY_GB` | `12` | メモリ (GB) |
| `OCI_DISPLAY_NAME` | `auto-retry-freenium` | インスタンス表示名 |
| `OCI_OS` | `Canonical Ubuntu` | OS 名 |
| `OCI_OS_VERSION` | `24.04` | OS バージョン |
| `OCI_ARCH` | `ARM` | アーキテクチャ |
| `OCI_BOOT_VOLUME_SIZE_GB` | `50` | ブートボリュームサイズ (GB) |

---

## 🚀 初回デプロイ手順

1. **リポジトリに push**
   ```bash
   git init
   git add .
   git commit -m "feat: OCI Always Free auto-retry"
   git branch -M main
   git remote add origin <YOUR_REPO_URL>
   git push -u origin main
   ```

2. **Secrets 登録**: 上記「GitHub Secrets 一覧」の全項目を登録する。

3. **Actions を有効化**
   - リポジトリの **Actions** タブを開く。
   - 初回は workflow が無効化されている場合があるので **"I understand my workflows, go ahead and enable them"** をクリック。

4. **手動実行で動作確認**
   - Actions → **OCI Always Free Auto-Retry** → **Run workflow** をクリック。
   - ログを確認し、`Out of host capacity` で exit 0 すれば正常 (次回 cron で再試行される)。

---

## 🔧 運用方法

### 5分毎の自動実行

`cron: "*/5 * * * *"` で自動実行される。何もしなくて良い。

### 手動実行

Actions タブ → **Run workflow** で任意のタイミングで実行できる。

### 状態リセット (再挑戦したい場合)

インスタンスを削除して再度リトライさせたい場合は、`state.json` をリセットする:

```bash
# state.json を初期状態に戻す
cp state.example.json state.json
git add -f state.json
git commit -m "chore(state): reset state.json"
git push
```

次回 cron 実行時に再び launch を試行するようになる。

---

## 🩺 トラブルシューティング

### "Out of host capacity" が解消されない

- **リージョンを変える**: `ap-tokyo-1` は特に混雑する。`us-ashburn-1` / `us-phoenix-1` / `ap-osaka-1` 等を検討。
- **AD を増やす**: 単一 AD のリージョンより、複数 AD のリージョンの方が確率が上がる。
- **時間帯を変える**: 日本時間の深夜〜早朝は比較的空きやすい。
- **OCI コンソールで手動起動を試す**: アカウント自体に制限がかかっている可能性 (Always Free の A1 枠はテナンシあたり 4 OCPU / 24GB まで)。

### workflow が実行されない

- Actions タブで workflow が有効化されているか確認。
- `cron` は GitHub 側の負荷状況により最大 10-15 分遅れることがある (即時性は保証されない)。
- `workflow_dispatch` で手動実行できるか確認。

### state.json が commit されない

- `permissions: contents: write` が workflow に設定されているか確認 (本リポジトリでは設定済み)。
- ブランチ保護ルールで `GITHUB_TOKEN` の push が弾かれていないか確認。個人リポジトリなら通常問題ない。
- `persist-credentials: true` が checkout ステップにあるか確認 (本リポジトリでは設定済み)。

### Discord 通知が来ない

- `DISCORD_WEBHOOK_URL` が正しいか確認。
- Webhook URL が無効化されていないか、Discord 側チャンネル設定を確認。

### API 認証エラー

- `OCI_API_KEY_FINGERPRINT` と `OCI_API_KEY_CONTENT` がペアで正しいか確認。
- ユーザが属するグループに適切なポリシー (`allow group ... to manage compute in tenancy`) が付与されているか確認。

---

## 📚 関連ドキュメント

- [設計・アーキテクチャ (docs/architecture.md)](docs/architecture.md)
- [OCI Python SDK ドキュメント](https://docs.oracle.com/en-us/iaas/tools/python/latest/)
- [OCI Always Free リソース](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)