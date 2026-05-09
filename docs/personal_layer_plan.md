# 個人化レイヤと 4noha ブランチ — 計画ドキュメント

このリポジトリには現在、ジャンルタグのキーワード・自動分類ルール・カード突合マップなど、4noha 個人の生活圏・契約サービス・所有カード等に強く依存するデータが Python 定数としてハードコードされている。これらを「テンプレート + ユーザ差分」の二層構造に分け、テンプレートは main で共有・差分は **4noha ブランチで管理** する形に再編する。

## 1. 現状: 個人データの所在

| 領域 | 場所 | 規模 |
|---|---|---|
| ジャンルキーワード 21 種 (CAR/BOOK/DRINK/CONVENIENCE/SUPERMARKET/RESTAURANT/HOMECENTER/LANDLORD/FACILITY/INVEST/INSURANCE/MEDICAL/SERVICE/ACTIVITY/FANTASY/OUTGOING/SALARY/SALES_INCOME/INSURANCE_INCOME/VPASS_REFUND) | `src/server/constants.py` | ~330 行 |
| STORE_TAG_KEYWORDS (ショップ識別) | 同上 | 1 dict |
| CARD_KEYWORD_MAP (MUFG 振替 desc → カード名) | `src/matching.py:20` | 3 件 |
| SHOP_CARD_MAP (ショップ × カード × 突合パラメータ) | `src/matching.py:216` | 1 dict |
| _RETURN_KEYWORDS (返品判定キーワード) | `src/server/categorize.py:202` | 4 件 |
| インボイスマスター (税控除の vendor 一覧) | `tax_vendors` テーブル + 入力 UI | DB 内 |

## 2. 二層モデル

### 2-1. テンプレート (main で共有)

「**新規ユーザが clone してすぐ動く**」ベースライン。一般性の高い抽象パターンを持つ。

- 「ENEOS / 出光 / アポロステーション」など全国規模の汎用キーワード
- 「コンビニ」「ガソリンスタンド」「マクドナルド」など普遍的な店舗
- 大手キャリア (povo / BIGLOBE 等)、大手 SaaS (Discord / OpenAI / Steam 等)
- **インボイスマスター** = 税申告で使う「振込先 / カード会社 / 大手プラットフォーム」の vendor 名 ↔ インボイス登録番号 (T で始まる 13 桁) の対応表 — 全ユーザ共通の公的データなので main にロードできる

### 2-2. 個人差分 (4noha ブランチで管理)

「特定ユーザの生活圏・購読・所有カードに依存する」固有データ。

- 「セルフつちうら」「ペトラス浦和美園店」など個人の通うガソスタ
- 「アルティマニア」「天体戦士サンレッド」など個人の所有 IP
- 「ピクシブ FANBOX」など個人の購読
- 個人所有のカード会社 (= CARD_KEYWORD_MAP)
- 個人がよく使うショップ × カードの組み合わせ (= SHOP_CARD_MAP)
- 個人の家賃支払先 (= LANDLORD_KEYWORDS)

## 3. ファイル構成案 (TOML ベース)

Python 定数のままだと「テンプレートと差分」を 2 ファイルでマージしにくいので、**設定ファイル化** して loader が両方を読む形にする。

```
config/
  keywords.toml              ← main で共有 (テンプレート)
  keywords.local.toml        ← .gitignore (or 4noha ブランチで管理)
  cards.toml                 ← main: 一般的な振替パターン
  cards.local.toml           ← 4noha: 個人所有カード
  invoice_master.toml        ← main: 公的 vendor master (大手プラットフォーム等)
src/personal/
  __init__.py                ← config/*.toml を読み込んで merge する loader
  README.md                  ← 「ここで読み込んだ値が constants に流れます」の説明
```

### TOML 例 (`config/keywords.toml`)

```toml
# テンプレート: 一般的な車関連キーワード
[CAR]
keywords = [
    "ETC", "高速", "NEXCO", "深夜割引",
    "ENEOS", "エネオス", "出光", "イデミツ", "アポロステーション", "コスモ",
    "ガソリン", "給油",
    "リパ", "タイムズ", "パーキング", "駐車",
    # 自動車部品の一般名
    "タイヤ", "ホイール", "オルタネーター",
]

[BOOK]
keywords = ["Kindle"]
# 個人の蔵書タイトルは local.toml で追加

[STORE_TAGS]
"AliExpress" = ["ALIPAY", "ALIEXPRESS", "アリエクスプレス"]
```

### 個人差分例 (`config/keywords.local.toml`)

```toml
# 個人差分: 4noha が通う特定店舗
[CAR]
keywords = ["セルフつちうら", "ペトラス"]      # → CAR の defaults と concat される

[BOOK]
keywords = ["アルティマニア", "天体戦士サンレッド", "運命など存在しない", "地球へ..."]

[FANTASY]
keywords = ["ピクシブ"]
```

### Loader 動作

```python
# src/personal/__init__.py の概略
from pathlib import Path
import tomllib

ROOT = Path(__file__).parent.parent.parent
DEFAULTS = tomllib.loads((ROOT / "config" / "keywords.toml").read_text())
LOCAL = {}
local_path = ROOT / "config" / "keywords.local.toml"
if local_path.exists():
    LOCAL = tomllib.loads(local_path.read_text())

def merged(category: str, key: str = "keywords") -> list:
    return list(DEFAULTS.get(category, {}).get(key, [])) \
         + list(LOCAL.get(category, {}).get(key, []))

CAR_KEYWORDS = merged("CAR")
BOOK_KEYWORDS = merged("BOOK")
# ...
```

`src/server/constants.py` は `from src.personal import CAR_KEYWORDS, BOOK_KEYWORDS, ...` で再エクスポート (既存呼び出しの後方互換)。

## 4. ブランチ戦略

```
main             ← framework + config/*.toml (template) + 空の config/*.local.toml.example
                   .gitignore: config/*.local.toml
                   tests は template だけで通る (個人データ非依存)
4noha (個人運用)  ← main を merge した上で config/*.local.toml を追加コミット
                   日常運用はこのブランチで動く
```

`config/*.local.toml` は **main では .gitignore 対象**、4noha ブランチでは追跡対象。

main → 4noha への upstream 更新は `git merge main` (or rebase)。`config/*.local.toml` は main で変更されない (ignore) ので merge コンフリクトを起こさない。

## 5. インボイスマスターの扱い

`tax_vendors` テーブルを config 化するアプローチ:

- `config/invoice_master.toml`: 大手プラットフォーム (Amazon Web Services / Google / 楽天 / 三井住友カード 等) の vendor name ↔ 法人番号 / インボイス登録番号
- 起動時 (or `/api/tax/import-master` 等) で TOML を読み、未登録の vendor を `tax_vendors` に挿入 (idempotent: 既存は update せず新規のみ)
- 個人特有の取引先は従来通り UI から手入力 (= local 扱い、4noha でコミットしたければ別 .local.toml に書ける)

これで「税申告で使う公的 vendor リスト」は main で共有・更新でき、ユーザは個別事業者だけ自分で追加する形になる。

## 6. 実装ステップ (5 コミット想定)

| # | コミット | ブランチ |
|---|---|---|
| 1 | `src/personal/` loader + `config/keywords.toml` template + 既存定数を loader 経由に置換 (動作維持) | main |
| 2 | `config/cards.toml` template + matching.py の `CARD_KEYWORD_MAP` / `SHOP_CARD_MAP` を loader 経由に置換 | main |
| 3 | `config/invoice_master.toml` template + 起動時マスター取込ジョブ追加 | main |
| 4 | tests を template-only でも通るよう個別 inject 化、CI スモーク追加 | main |
| 5 | 4noha ブランチを切って `config/*.local.toml` をコミット (個人実データ移行) | 4noha |

各 main コミット後に 4noha を rebase → 最終的に 4noha が `config/*.local.toml` だけ余分に持つ運用に収束。

## 7. リスク / 留意点

- **後方互換**: `from src.server.constants import CAR_KEYWORDS` を呼んでいる箇所が複数あるため、`constants.py` は loader 経由でも同じ名前を export し続ける。
- **NFKC キャッシュ**: `categorize.py` のモジュール起動時に `_CAR_KW = _nk(CAR_KEYWORDS)` がある。loader が起動時 1 回読めばよい。
- **テストデータ**: `tests/test_categorize.py` は本物のキーワード (ENEOS / マクドナルド) を前提にしている → template に最低限残すか、test fixture でモックする。
- **書込み権限**: config は read-only。UI から書き換える機能はつけない方針 (= ユーザは git で編集)。
- **TOML の表現力**: list / dict / nested は問題なし。`SHOP_CARD_MAP` の tuple 値はリスト化して loader でアンパック。

## 8. 経費判断ロジックの設定ファイル化

`src/server/categorize.py` の `auto_categorize_*` 群は、現在 Python 関数として
ハードコードされている (各々 SQL + 条件ロジック)。これらも宣言的なルール定義
として config 化し、ロジックは loader が読んでビルドする方式にする。

### ルールの分類

| 種別 | 例 (現行 Python 実装) | 4noha 固有度 |
|---|---|---|
| **キーワード一致 → カテゴリ** | `OUTGOING_KEYWORDS` ヒット → "出金" / `SALARY` ヒット → "給与" | 低 (ロジックは普遍、対象キーワードは個人) |
| **bank/debit/credit の単純条件** | bank='Amazon' AND debit=0 → "個人支出" | 普遍に近い (現行の挙動でほぼ全ユーザに通用) |
| **正規表現パターン** | description LIKE '%Suica%' → "出金" | 個人 (Suica チャージは 4noha の電車利用) |
| **N行間の伝播** | 手数料行 → 直前の同行同日カテゴリ継承 | 普遍 |
| **複雑な join** | 返品判定 (60日以内に同額 credit + ショップ名キーワード) | 普遍 |
| **履歴学習** | 過去の手動分類から (bank, desc) → category マップ生成 | 普遍 |

→ **シンプルなルールは TOML で宣言、複雑な join/学習は Python 関数のまま**
というハイブリッド方針が現実的。

### TOML スキーマ案 (`config/auto_rules.toml`)

```toml
# 全ユーザ共通: bank / debit パターンによる単純判定
[[rules]]
id = "amazon_points_full"
description = "Amazon ポイント全額決済 (debit=0) → 個人支出"
type = "field_match"
when = { bank = "Amazon", debit = 0, category_in = ["", "_null_"] }
set_category = "個人支出"

# 全ユーザ共通: ジャンルキーワードに紐づくカテゴリ自動付与
[[rules]]
id = "outgoing_by_keyword"
description = "MUFG の OUTGOING キーワードヒット → 出金"
type = "keyword_match"
when = { bank = "MUFG", debit_gt = 0, category_in = ["", "_null_"] }
keyword_list = "OUTGOING"     # config/keywords.toml の section
field = "description_normalized"
set_category = "出金"

[[rules]]
id = "salary"
description = "credit > 0 で SALARY キーワードヒット → 給与"
type = "keyword_match"
when = { credit_gt = 0, category_in = ["", "_null_"] }
keyword_list = "SALARY"
field = "description_normalized"
set_category = "給与"

# 個人差分: VPASS の Suica チャージ → 出金 (4noha のみ電車利用)
# → config/auto_rules.local.toml に置く
[[rules]]
id = "vpass_suica"
type = "pattern_match"
when = { bank = "VPASS", category_in = ["", "_null_"] }
pattern = "%Suica%"
field = "description_normalized"
set_category = "出金"

# 種別: 隣接行カテゴリ継承
[[propagate_rules]]
id = "fee_inherit"
description = "手数料行 → 直前同行同日のカテゴリ継承"
target_patterns = [
    "%フリコミ%テスウリヨウ%",
    "手数料",
    "%振込手数料%",
    "%為替手数料%",
]
inherit_from = "prev_same_bank_same_date"
```

### Loader / Engine

```python
# src/personal/auto_rules.py の概略
@dataclass
class FieldMatchRule: when: dict; set_category: str
@dataclass
class KeywordMatchRule: when: dict; keyword_list: str; field: str; set_category: str
@dataclass
class PatternMatchRule: when: dict; pattern: str; field: str; set_category: str
@dataclass
class PropagateRule: target_patterns: list[str]; inherit_from: str

def load_rules() -> list[Rule]: ...    # TOML を読んでルールオブジェクト化
def apply_field_match(con, rule, invalidate_cache): ...
def apply_keyword_match(con, rule, invalidate_cache): ...
def apply_pattern_match(con, rule, invalidate_cache): ...
def apply_propagate(con, rule, invalidate_cache): ...

def run_all_auto_rules(con, invalidate_cache):
    for rule in load_rules():
        if isinstance(rule, FieldMatchRule):    apply_field_match(con, rule, invalidate_cache)
        elif isinstance(rule, KeywordMatchRule): apply_keyword_match(con, rule, invalidate_cache)
        elif isinstance(rule, PatternMatchRule): apply_pattern_match(con, rule, invalidate_cache)
        elif isinstance(rule, PropagateRule):    apply_propagate(con, rule, invalidate_cache)
```

`refresh_categorization()` は `run_all_auto_rules()` + 既存の Python 関数
(`auto_categorize_returned_purchases` / `auto_categorize_from_history`) を順次呼ぶ。

### 残す Python 関数 (config 化しないもの)

- `auto_categorize_returned_purchases`: 60日窓 + 同額 + 銀行マッピング dict (`_RETURN_KEYWORDS`) で複雑な join。
  → 入力データ (`_RETURN_KEYWORDS`) のみ TOML 化、関数本体は残す。
- `auto_categorize_from_history`: 過去の手動分類から学習。アルゴリズム自体が ML 風で TOML 化に馴染まない。

## 9. 実装ステップ (改訂版、6 コミット想定)

| # | コミット | ブランチ | 内容 |
|---|---|---|---|
| 1 | `src/personal/` loader + `config/keywords.toml` template + constants.py を loader 経由に | main | キーワードのみ |
| 2 | `config/cards.toml` template + matching.py の MAP を loader 経由に | main | カード関連 |
| 3 | `config/auto_rules.toml` template + auto_rules engine + `categorize.py` の単純ルール (`outgoing` / `amazon_zero` / `income` / 一部) を rule engine に置換 | main | ロジック化 |
| 4 | `config/invoice_master.toml` + 起動時マスター取込ジョブ | main | インボイス |
| 5 | tests を template-only で通るよう調整 + CI スモーク | main | テスト |
| 6 | 4noha ブランチを切り `config/*.local.toml` (個人実データ) をコミット | 4noha | 個人差分 |

## 10. これで満たされる動作

- 新規ユーザが `git clone` 後に `uv run python -m src.daemon` を起動するとテンプレートのキーワード + 共通の自動分類ルールで動く (= ENEOS が "car" タグ、AWS 請求が invoice master からヒット、「給料 / 給与」が "給与" カテゴリに自動分類 等)
- 4noha ブランチは `config/*.local.toml` で:
  - 個人キーワード追加 (例: 蔵書名 / 通うガソスタ)
  - 個人カードマッピング (持ってない MUFGAmex を消したり、新しいカード会社を追加したり)
  - 個人ルール追加 (例: VPASS の Suica チャージ → 出金 = 4noha の電車利用)
- main の更新 (例: 新しい大手 SaaS を template に追加) は 4noha に merge で取り込み、`config/*.local.toml` は main 側で変更されないのでコンフリクトを起こさない
- 経費判断ルールが TOML で宣言されているので、新しいルールを足したいときは Python を書かずに toml 編集で完結する。複雑な join 系 (返品判定 / 履歴学習) は Python 関数のまま — 適材適所のハイブリッド。
