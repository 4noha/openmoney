"""支出分類 Web UI 用の定数群。

framework 定数 (CATEGORIES / CARD_BANKS / SITE_BANKS / STORE_TAG_BANKS) は
ここで直接定義する。一方ジャンルキーワード等の **個人化レイヤ**
(*_KEYWORDS / STORE_TAG_KEYWORDS) は `config/keywords.toml` (テンプレート) +
`config/keywords.local.toml` (個人差分) に分離され、`src.personal` モジュール
が読み込む。本ファイルは互換のため `from src.personal import *` で再 export
し、既存呼び出し (`from src.server.constants import CAR_KEYWORDS`) を維持。

詳細: docs/personal_layer_plan.md
"""
from src.personal import (  # noqa: F401  (re-export)
    CAR_KEYWORDS,
    BOOK_KEYWORDS,
    DRINK_KEYWORDS,
    CONVENIENCE_KEYWORDS,
    SUPERMARKET_KEYWORDS,
    SUPERMARKET_EXCLUDE_KEYWORDS,
    RESTAURANT_KEYWORDS,
    HOMECENTER_KEYWORDS,
    LANDLORD_KEYWORDS,
    FACILITY_KEYWORDS,
    INVEST_KEYWORDS,
    INSURANCE_KEYWORDS,
    MEDICAL_KEYWORDS,
    SERVICE_KEYWORDS,
    ACTIVITY_KEYWORDS,
    FANTASY_KEYWORDS,
    OUTGOING_KEYWORDS,
    SALARY_KEYWORDS,
    SALES_INCOME_KEYWORDS,
    RENT_INCOME_KEYWORDS,
    INSURANCE_INCOME_KEYWORDS,
    VPASS_REFUND_KEYWORDS,
    STORE_TAG_KEYWORDS,
)


CATEGORIES = ["", "経費", "今回は経費", "個人支出", "今回は個人支出", "出金",
              "給与", "家賃収入", "副業収入", "売上", "返金", "保険金", "非課税"]
EXPENSE_CATS = ["", "経費", "今回は経費", "個人支出", "今回は個人支出", "出金"]
INCOME_CATS  = ["", "給与", "家賃収入", "副業収入", "売上", "返金", "保険金", "非課税"]

CARD_BANKS = frozenset([
    "VPASS", "MUFGAmex", "Orico", "メルカード",
    # AmazonPay も「決済明細が取れる」面では card と同等。
    # 実体はカード発行体ではないがプラットフォームから merchant + 金額が取得可能。
    "AmazonPay",
])
SITE_BANKS = frozenset(["Amazon", "Yahoo!ショッピング", "楽天市場", "ヤフオク購入",
                         "Makuake", "CAMPFIRE", "Mercari", "メルカード", "AmazonPay",
                         "PayPal", "AliExpress",
                         # povo は通信サービスだが、 site_matched 機構で
                         # 「povo 通話料 transaction (= 月別、 PDF レシートあり)
                         # ↔ VPASS 同月同金額のｐｏｖｮご利用料金 」 を突合するため
                         # site 側として扱う。 /ui の merge mode で重複を隠せる
                         "povo",
                         # Wonder Studio (旧 Stripe / 新 Autodesk) も同様。
                         # bank='Wonder Studio' の月次 tx と VPASS のオートデスク
                         # 請求 (¥14,300 / ¥15,400 等) を金額一致で突合する
                         "Wonder Studio",
                         # DeepL Pro (= 月額¥1,200) も同様。 VPASS の
                         # "DEEPL* SUB:1340113 CUS (KOLN )" と日付近接 + 同金額で突合
                         "DeepL",
                         # Discord (= PayPal 経由) ↔ VPASS "PAYPAL *DISCORD"
                         "Discord",
                         # Google Play (= 1 注文 = 1 メール) ↔ VPASS "GOOGLE *<app>"
                         "Google Play",
                         "vidIQ",
                         "AWS"])

STORE_TAG_BANKS = frozenset([
    "Amazon",
    # AmazonPay は決済手段なので店舗扱いせず、別途 'amazonpay' メタタグを
    # _fetch_base_rows で emit する (paypay / stripe と同じ patterns)
    "Yahoo!ショッピング",
    "楽天市場",
    "ヤフオク購入", "ヤフオク売上",
    "Makuake", "CAMPFIRE",
    "Mercari", "Mercari売上",
    "AliExpress",
    "povo",  # bank='povo' (= povo PDF の通話料 transaction) に store:povo タグ
    "Wonder Studio",  # bank='Wonder Studio' (= 旧 Stripe USD + 新 Autodesk JPY)
    "DeepL",  # bank='DeepL' (= 月額¥1,200 課金、 領収書メールから取得)
    "Discord",
    "Google Play",
    "vidIQ",
    "AWS",
    # bank='レシート' は store:レシート ではなく ツールタグ 'receipt' (= 証憑あり)
    # に集約する。 紙レシートと PDF レシートを内部で区別せず、 receipt_ids あり
    # の全 tx を「🧾 レシート」 1 タグでフィルタする運用 (ユーザ指針)
])
