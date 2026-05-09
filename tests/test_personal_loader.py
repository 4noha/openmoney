"""個人化レイヤ loader (src/personal/__init__.py) の単体テスト。

config/keywords.toml + config/keywords.local.toml が defaults + local の concat /
override で正しく合成されるかを検証する。

テストでは src.personal モジュールの内部関数 (_load_toml, _merged_list,
_merged_store_tags) を直接呼ぶ形で、本番ファイルに依存しないユニットテストとする。
"""
from __future__ import annotations

from pathlib import Path

from src import personal as P


def _write_toml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_load_toml_returns_dict_or_empty(tmp_path):
    """存在する TOML は辞書、無いファイルは空 dict。"""
    p = tmp_path / "x.toml"
    assert P._load_toml(p) == {}
    _write_toml(p, '[FOO]\nkeywords = ["a", "b"]\n')
    assert P._load_toml(p) == {"FOO": {"keywords": ["a", "b"]}}


def test_merged_list_concats_defaults_and_local(monkeypatch):
    """[CATEGORY].keywords = defaults + local の concat。"""
    monkeypatch.setattr(P, "_DEFAULTS", {"CAR": {"keywords": ["ETC", "ENEOS"]}})
    monkeypatch.setattr(P, "_LOCAL",    {"CAR": {"keywords": ["セルフつちうら"]}})
    assert P._merged_list("CAR") == ["ETC", "ENEOS", "セルフつちうら"]


def test_merged_list_works_when_only_defaults(monkeypatch):
    """local 不在でも defaults だけで動く。"""
    monkeypatch.setattr(P, "_DEFAULTS", {"BOOK": {"keywords": ["Kindle"]}})
    monkeypatch.setattr(P, "_LOCAL", {})
    assert P._merged_list("BOOK") == ["Kindle"]


def test_merged_list_works_when_only_local(monkeypatch):
    """defaults 不在で local のみでも動く (新カテゴリの追加など)。"""
    monkeypatch.setattr(P, "_DEFAULTS", {})
    monkeypatch.setattr(P, "_LOCAL", {"NEW_CAT": {"keywords": ["abc"]}})
    assert P._merged_list("NEW_CAT") == ["abc"]


def test_merged_list_returns_empty_when_neither(monkeypatch):
    monkeypatch.setattr(P, "_DEFAULTS", {})
    monkeypatch.setattr(P, "_LOCAL", {})
    assert P._merged_list("MISSING") == []


def test_merged_store_tags_local_overrides_defaults(monkeypatch):
    """STORE_TAGS は同一キーの場合 local が defaults を上書き (リスト置換)。"""
    monkeypatch.setattr(P, "_DEFAULTS", {
        "STORE_TAGS": {
            "Amazon": ["AMAZON.CO.JP"],
            "楽天市場": ["RAKUTEN"],
        }
    })
    monkeypatch.setattr(P, "_LOCAL", {
        "STORE_TAGS": {
            "Amazon": ["AMAZON.CO.JP", "AMAZONJP"],     # 上書き
            "MyShop": ["MYSHOP"],                        # 新規
        }
    })
    out = P._merged_store_tags()
    assert out["Amazon"] == ["AMAZON.CO.JP", "AMAZONJP"]
    assert out["楽天市場"] == ["RAKUTEN"]
    assert out["MyShop"] == ["MYSHOP"]


def test_real_config_loads_without_error():
    """本番の config/keywords.toml が tomllib で読める + 21 リスト全て list 型。"""
    for name in (
        "CAR_KEYWORDS", "BOOK_KEYWORDS", "DRINK_KEYWORDS",
        "CONVENIENCE_KEYWORDS", "SUPERMARKET_KEYWORDS",
        "RESTAURANT_KEYWORDS", "HOMECENTER_KEYWORDS",
        "LANDLORD_KEYWORDS", "FACILITY_KEYWORDS",
        "INVEST_KEYWORDS", "INSURANCE_KEYWORDS", "MEDICAL_KEYWORDS",
        "SERVICE_KEYWORDS", "ACTIVITY_KEYWORDS",
        "FANTASY_KEYWORDS", "OUTGOING_KEYWORDS",
        "SALARY_KEYWORDS", "SALES_INCOME_KEYWORDS",
        "INSURANCE_INCOME_KEYWORDS", "VPASS_REFUND_KEYWORDS",
    ):
        v = getattr(P, name)
        assert isinstance(v, list), f"{name} should be list"
    # STORE_TAG_KEYWORDS は dict[str, list[str]]
    assert isinstance(P.STORE_TAG_KEYWORDS, dict)
    for k, v in P.STORE_TAG_KEYWORDS.items():
        assert isinstance(k, str)
        assert isinstance(v, list)


def test_constants_re_exports_match(monkeypatch):
    """src.server.constants.CAR_KEYWORDS が src.personal.CAR_KEYWORDS と同じオブジェクト。"""
    from src.server import constants
    assert constants.CAR_KEYWORDS is P.CAR_KEYWORDS
    assert constants.STORE_TAG_KEYWORDS is P.STORE_TAG_KEYWORDS


# ─────────────────────────────────────────────
# Step 2: cards.toml (CARD_KEYWORD_MAP / SHOP_CARD_MAP / RETURN_KEYWORDS)
# ─────────────────────────────────────────────


def test_card_keyword_map_loaded():
    """MUFG 振替キーワード → カード会社名の dict が空でないこと。"""
    assert isinstance(P.CARD_KEYWORD_MAP, dict)
    assert "ミツイスミトモカ－ド" in P.CARD_KEYWORD_MAP
    assert P.CARD_KEYWORD_MAP["ミツイスミトモカ－ド"] == "VPASS"


def test_shop_card_map_loaded_as_tuples():
    """SHOP_CARD_MAP[shop] = (card_banks, keywords, day_tol, amount_tol) tuple 形式。"""
    assert isinstance(P.SHOP_CARD_MAP, dict)
    assert "Amazon" in P.SHOP_CARD_MAP
    card_banks, keywords, day_tol, amount_tol = P.SHOP_CARD_MAP["Amazon"]
    assert isinstance(card_banks, list)
    assert "VPASS" in card_banks
    assert isinstance(keywords, list)
    assert "AMAZON" in keywords
    assert isinstance(day_tol, int) and day_tol > 0
    assert isinstance(amount_tol, float) and 0 < amount_tol < 1


def test_return_keywords_loaded():
    """返品判定の RETURN_KEYWORDS が dict[str, list[str]] で空でない。"""
    assert isinstance(P.RETURN_KEYWORDS, dict)
    assert "Amazon" in P.RETURN_KEYWORDS
    assert "AMAZON" in P.RETURN_KEYWORDS["Amazon"]


def test_merged_dict_local_overrides_defaults(monkeypatch):
    """CARD_KEYWORD_MAP / RETURN_KEYWORDS は dict 系で同名キー local 上書き。"""
    monkeypatch.setattr(P, "_CARDS_DEFAULTS", {
        "CARD_KEYWORD_MAP": {"OldName": "VPASS"}
    })
    monkeypatch.setattr(P, "_CARDS_LOCAL", {
        "CARD_KEYWORD_MAP": {"OldName": "Orico", "NewCard": "JACCS"}
    })
    out = P._merged_dict("CARD_KEYWORD_MAP")
    assert out["OldName"] == "Orico"   # local 上書き
    assert out["NewCard"] == "JACCS"   # local 追加


def test_merged_shop_card_map_tuple_unpack_with_local(monkeypatch):
    """SHOP_CARD_MAP は subtable から tuple に変換 + local 上書き。"""
    monkeypatch.setattr(P, "_CARDS_DEFAULTS", {
        "SHOP_CARD_MAP": {
            "Amazon": {
                "card_banks": ["VPASS"],
                "keywords": ["AMAZON"],
                "day_tol": 30,
                "amount_tol": 0.01,
            }
        }
    })
    monkeypatch.setattr(P, "_CARDS_LOCAL", {
        "SHOP_CARD_MAP": {
            "Amazon": {  # 既存 override
                "card_banks": ["VPASS", "MUFGAmex"],
                "keywords": ["AMAZON", "アマゾン"],
                "day_tol": 30,
                "amount_tol": 0.05,
            },
            "BOOTH": {   # 新規追加
                "card_banks": ["VPASS"],
                "keywords": ["BOOTH"],
                "day_tol": 14,
                "amount_tol": 0.02,
            },
        }
    })
    out = P._merged_shop_card_map()
    cb, kw, day, amt = out["Amazon"]
    assert cb == ["VPASS", "MUFGAmex"]
    assert kw == ["AMAZON", "アマゾン"]
    assert day == 30
    assert amt == 0.05
    assert "BOOTH" in out


def test_matching_module_uses_loader_value():
    """src.matching.CARD_KEYWORD_MAP / SHOP_CARD_MAP が src.personal と同一 object。"""
    import src.matching as M
    assert M.CARD_KEYWORD_MAP is P.CARD_KEYWORD_MAP
    assert M.SHOP_CARD_MAP is P.SHOP_CARD_MAP
