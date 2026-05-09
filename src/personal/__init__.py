"""個人化レイヤ loader (Phase 1: keywords)。

`config/keywords.toml` (テンプレート、main で全員に共有) と
`config/keywords.local.toml` (個人差分、各参加者の personal branch で管理)
を読み込んでマージし、`KEYWORDS` 系の Python 定数として公開する。

各 list は `defaults + local` で **concat** される (どちらかが無くても動く)。
STORE_TAG_KEYWORDS dict はキー単位で local が defaults を上書き (両方にある
ショップは local 優先)。

`src/server/constants.py` がここから再 export しているので、既存呼び出し
(`from src.server.constants import CAR_KEYWORDS`) はそのまま動く。

詳細: docs/personal_layer_plan.md
"""
from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
_CONFIG_DIR = ROOT / "config"


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


# keywords (Step 1): defaults / local
_DEFAULTS = _load_toml(_CONFIG_DIR / "keywords.toml")
_LOCAL = _load_toml(_CONFIG_DIR / "keywords.local.toml")

# cards (Step 2): defaults / local
_CARDS_DEFAULTS = _load_toml(_CONFIG_DIR / "cards.toml")
_CARDS_LOCAL = _load_toml(_CONFIG_DIR / "cards.local.toml")


def _merged_list(category: str) -> list[str]:
    """[CATEGORY].keywords = [...] を defaults + local で concat する。"""
    defaults = _DEFAULTS.get(category, {}).get("keywords", [])
    local = _LOCAL.get(category, {}).get("keywords", [])
    return list(defaults) + list(local)


# 21 ジャンルキーワード (constants.py から移管)
CAR_KEYWORDS              = _merged_list("CAR")
BOOK_KEYWORDS             = _merged_list("BOOK")
DRINK_KEYWORDS            = _merged_list("DRINK")
CONVENIENCE_KEYWORDS      = _merged_list("CONVENIENCE")
SUPERMARKET_KEYWORDS      = _merged_list("SUPERMARKET")
# {TAG}_EXCLUDE: そのタグの判定から除外したい description キーワード。
# 部分一致誤検出 (例: 「ライフ」 で SUPERMARKET ヒットする「株式会社ライフ日鋼」 =
# ガソリンスタンド) を防ぐためのアンチキーワード。 detect_tags で TAG キーワードに
# マッチしても、 description が EXCLUDE keyword を含めばタグを付与しない。
SUPERMARKET_EXCLUDE_KEYWORDS = _merged_list("SUPERMARKET_EXCLUDE")
RESTAURANT_KEYWORDS       = _merged_list("RESTAURANT")
HOMECENTER_KEYWORDS       = _merged_list("HOMECENTER")
LANDLORD_KEYWORDS         = _merged_list("LANDLORD")
FACILITY_KEYWORDS         = _merged_list("FACILITY")
INVEST_KEYWORDS           = _merged_list("INVEST")
INSURANCE_KEYWORDS        = _merged_list("INSURANCE")
MEDICAL_KEYWORDS          = _merged_list("MEDICAL")
SERVICE_KEYWORDS          = _merged_list("SERVICE")
ACTIVITY_KEYWORDS         = _merged_list("ACTIVITY")
FANTASY_KEYWORDS          = _merged_list("FANTASY")
OUTGOING_KEYWORDS         = _merged_list("OUTGOING")
SALARY_KEYWORDS           = _merged_list("SALARY")
SALES_INCOME_KEYWORDS     = _merged_list("SALES_INCOME")
RENT_INCOME_KEYWORDS      = _merged_list("RENT_INCOME")
INSURANCE_INCOME_KEYWORDS = _merged_list("INSURANCE_INCOME")
VPASS_REFUND_KEYWORDS     = _merged_list("VPASS_REFUND")


# STORE_TAG_KEYWORDS: dict[shop_name -> list[str]]
# defaults の dict をベースに local の dict を merge (重複キーは local 優先)
def _merged_store_tags() -> dict[str, list[str]]:
    out = {k: list(v) for k, v in _DEFAULTS.get("STORE_TAGS", {}).items()
           if isinstance(v, list)}
    for k, v in _LOCAL.get("STORE_TAGS", {}).items():
        if isinstance(v, list):
            out[k] = list(v)
    return out


STORE_TAG_KEYWORDS: dict[str, list[str]] = _merged_store_tags()


# ─────────────────────────────────────────────
# cards (Step 2): CARD_KEYWORD_MAP / SHOP_CARD_MAP / RETURN_KEYWORDS
# defaults と local が同じ section を持つ場合、key 単位で local が override する
# (= 個人差分はカード会社追加 / 削除 / 突合パラメータの調整に使う)
# ─────────────────────────────────────────────


def _merged_dict(section: str) -> dict:
    """[SECTION] テーブル全体を defaults + local の浅い merge で返す。"""
    out = dict(_CARDS_DEFAULTS.get(section, {}))
    out.update(_CARDS_LOCAL.get(section, {}))
    return out


# MUFG 振替 description → カード会社名 (例: "ミツイスミトモカ－ド" -> "VPASS")
CARD_KEYWORD_MAP: dict[str, str] = _merged_dict("CARD_KEYWORD_MAP")


def _merged_shop_card_map() -> dict[str, tuple[list[str], list[str], int, float]]:
    """SHOP_CARD_MAP: 各ショップ名 -> (card_banks, keywords, day_tol, amount_tol) tuple。
    TOML 上は subtable 形式で書かれているので tuple に変換する。"""
    raw = dict(_CARDS_DEFAULTS.get("SHOP_CARD_MAP", {}))
    raw.update(_CARDS_LOCAL.get("SHOP_CARD_MAP", {}))  # 同名ショップは local 優先
    out: dict[str, tuple[list[str], list[str], int, float]] = {}
    for shop, conf in raw.items():
        if not isinstance(conf, dict):
            continue
        out[shop] = (
            list(conf.get("card_banks", [])),
            list(conf.get("keywords", [])),
            int(conf.get("day_tol", 14)),
            float(conf.get("amount_tol", 0.01)),
        )
    return out


SHOP_CARD_MAP = _merged_shop_card_map()

# ショップ購入と同額のカード credit を返品判定するためのキーワード
# (auto_categorize_returned_purchases で使用)
RETURN_KEYWORDS: dict[str, list[str]] = _merged_dict("RETURN_KEYWORDS")
