# OpenMoney

**AI と一緒につくる、自分だけの家計簿ツール。**

銀行・EC サイトの取引履歴を自動取得し、カード明細・銀行引落・レシートを **ひとつの取引として突合** する。
「この引落は何の支払い？」を AI と会話しながら分類・タグ付けし、自動化していく。

---

## なにができるか

### 📊 突合（マッチング）が強い

| 突合の種類 | 例 |
|---|---|
| カード明細 ↔ 銀行引落 | MUFG の口座振替 ↔ 楽天カード月次請求 |
| ショップ購入 ↔ カード明細 | Amazon 注文 ↔ カード個別請求 |
| 紙レシート ↔ カード明細 | スーパーのレシート OCR ↔ 引落 |
| Amazon 複数配送 | 1 注文 → 複数カード請求を自動統合 |

「支払い方法が違う」「請求日がズレる」「分割配送」— これらをまとめて **ひとつの取引** として見せる。

### 🤖 AI コーディングで自分仕様に育てる

新しい分類ルールやタグの追加は、コードを書かずに AI に話しかけるだけ。

```
「スタバと松屋を「飲食店」タグに追加して」
→ AI が config/keywords.local.toml を編集
→ 次回から自動で適用される
```

```
「Amazon で本を買ったら自動で「書籍」に分類して」
→ AI が config/auto_rules.local.toml にルールを追加
→ 以降は自動実行
```

AI への指示がそのままコードとデータに残るので、**一度設定すれば繰り返さなくていい**。

### 📱 レシート撮影 → 自動突合

Android アプリでレシートを撮影すると Claude Vision が OCR し、金額・日付でカード明細と自動突合する。経費の記録も写真 1 枚で完結。

レシート OCR には Claude の Vision 機能を活用しており、手書きや印字がかすれた紙レシートでも高精度に読み取る。品目・金額・日付・店名を一括抽出し、カード明細の突合まで自動で行う。

---

## セキュリティ

**クラウドには何も預けない** のが基本思想。

- **すべてローカル動作**: スクレイピング・DB（SQLite）・UI はすべて `localhost` で完結
- **認証情報は暗号化保管**: `.env` のパスワード類は AES-256-GCM + scrypt で暗号化。マスターパスワードで解錠するまで平文は存在しない
- **クラウドは 2FA だけ**: 二段階認証が必要な瞬間だけ Firebase 経由で Android に通知。取引データ・カード番号はクラウドを通らない
- **OTP プッシュは月 ¥1,000 のアプリで**: OpenMoney アプリを使うと、2FA プッシュ通知 + AI プロキシ + レシート中継がセットで **月 ¥1,000** で利用できる
- **AI は自分のキーに切り替え可能**: `.env` に自前の Anthropic API キーを設定すればアプリの AI プロキシを介さず直接 Claude を呼ぶ
- **AI で改造すれば完全無料にもできる**: Firebase の代わりにローカル LAN 通知を実装するようAIに頼めば、クラウド費用ゼロで同等の動作が可能。プラグインと同様に、コードを直接書かなくても AI との会話で実現できる

---

## 拡張性

新しい銀行・ショップのスクレイパーは **プラグイン** として 1 ディレクトリ追加するだけ。

```
plugins/my_bank/
  plugin.py    # 宣言（銀行名・認証情報・スキーマ）
  scraper.py   # 実装
```

外部パッケージとして配布された 3rd-party プラグインも `pip install` で自動取り込み。
プラグインを書くのも AI に頼める。

---

## デフォルト対応プラグイン

| プラグイン | 取得内容 |
|---|---|
| **三菱UFJ銀行** | 入出金明細・あんしんパス QR 認証対応 |
| **Amazon** | 注文履歴・商品明細・領収書 PDF・AmazonPay |
| **楽天市場** | 注文履歴・領収書 PDF |

その他のプラグインは `plugins/` ディレクトリに同梱されています。`config/plugins.toml` で有効化できます。

---

## セットアップ

### 必要なもの

- macOS / Linux（Windows は動作確認中）
- Python 3.11+（[uv](https://docs.astral.sh/uv/) 推奨）
- [Claude Code](https://claude.ai/code)（`brew install claude-code` または公式サイトからインストール）
- Android スマートフォン（2FA 承認・レシート撮影用）

### 手順

```bash
# 1. クローン
git clone https://github.com/4noha/openmoney.git && cd openmoney

# 2. 起動 (= 初回も日常も同じコマンド)
bash start.sh
```

`start.sh` は idempotent で、初回はペアリング・Firebase 設定・初期パスワード設定までインタラクティブに案内し、2 回目以降は完了済みステップを自動でスキップしてサーバ（daemon）と埋め込み Claude Code を起動します。以降は Claude Code との会話でセットアップを進めます。

> `bash setup.sh` も従来どおり動作します（中身は `start.sh` への薄いエイリアス）。

### Claude Code を使わず単体で動かす場合

OpenMoney 本体（daemon + Web UI + スクレイパー）は Claude Code と独立して動かせます。レシート OCR を含む AI 機能を使うときだけ `.env` の `ANTHROPIC_API_KEY` を消費しますが、それ以外（明細表示・残高集計・スクレイプ実行・設定 UI）はオフラインで完結します。

```bash
# 1. 依存をインストール
uv sync

# 2. .env を用意 (=各金融機関の認証情報を入れる)
cp .env.example .env
$EDITOR .env

# 3. daemon を起動 (= スケジューラ + Web サーバ)
uv run python -m src.daemon
```

ブラウザで `http://localhost:8765/ui` を開く。初回は `/login` で初期パスワードを設定すると `.env` が暗号化されます (= 🔒 マーカー付きに変換)。

**スクレイパーだけ手動実行したい場合**:

```bash
uv run python -m plugins.mufg.scraper      # 三菱UFJ銀行
uv run python -m plugins.amazon.scraper    # Amazon
uv run python -m plugins.rakuten.scraper   # 楽天市場
uv run python -m src.matching              # 突合レポート
```

**ペアリング (= Android アプリ連携 / AI Proxy 利用) はスキップ可**

ペアリングは「Android アプリからの 2FA 中継」「OpenMoney backend 経由で Claude を従量課金で使う」ための機能です。これらが不要なら `.env` に銀行の認証情報を直接書き込むだけで動きます。`OPENMONEY_PC_TOKEN` 等が無くても daemon は起動します。

> AI 機能 (レシート OCR 等) を Anthropic API で直接使う場合は、`.env` に `ANTHROPIC_API_KEY=sk-ant-...` を設定すれば backend プロキシを介さず Anthropic に直接リクエストできます。

### Linux デスクトップアプリとして使う

pywebview でラップした専用ウィンドウで起動できる。ブラウザ不要、DevTools も使えるのでデバッグしやすい。

```bash
# 1. WebKit2GTK のシステムパッケージを入れる（Ubuntu/Debian）
sudo apt install python3-gi python3-gi-cairo \
    gir1.2-gtk-3.0 gir1.2-webkit2-4.1

# 2. Python 依存をインストール
uv sync

# 3. 起動（ターミナルにログが流れる）
uv run python desktop.py
```

ウィンドウ内で右クリック → 「要素を検証」で DevTools が開く。

**アプリメニューへの登録（任意）**

```bash
bash scripts/install_desktop.sh
```

`~/.local/share/applications/openmoney.desktop` がインストールされ、デスクトップ環境のアプリランチャーから起動できるようになる。

---

## 使い方

```bash
# 個別スクレイパーの手動実行
uv run python -m plugins.mufg.scraper      # 三菱UFJ銀行
uv run python -m plugins.amazon.scraper   # Amazon
uv run python -m plugins.rakuten.scraper  # 楽天市場

# 突合レポート
uv run python -m src.matching
```

---

## 関連プロジェクト

| プロジェクト | 役割 |
|---|---|
| **openmoney**（本リポジトリ）| スクレイピング・突合・UI |
| **[openmoney-backend](https://github.com/4noha/openmoney-backend)** | AI プロキシ・ペアリング・課金（SaaS 層） |
| **OpenMoney Eclipse**（Android）| 2FA 承認・レシート撮影 |

---

## ライセンス

MIT
