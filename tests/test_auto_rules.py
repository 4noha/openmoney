"""auto_rules 経費判断ルールエンジンのテスト (#34 / Step 3)。

config/auto_rules.toml は本番で使う実データなので、ここでは TOML を直接
モックせず engine の挙動 (apply_field_match / apply_keyword_match /
apply_pattern_match / apply_propagate) を直接呼ぶユニットテストを書く。
"""
from __future__ import annotations

from datetime import datetime

from src.personal import auto_rules as AR


def _insert_tx(con, bank: str, debit: int = 0, credit: int = 0,
               desc: str = "test", date: str = "2026/05/01") -> int:
    cur = con.execute(
        "INSERT INTO transactions (bank, date, description, description_normalized, "
        "debit, credit, fetched_at, category) VALUES (?,?,?,?,?,?,?,?)",
        (bank, date, desc, desc, debit, credit, datetime.now().isoformat(), ""),
    )
    return cur.lastrowid


def _category_of(con, tx_id: int) -> str:
    row = con.execute("SELECT category FROM transactions WHERE id=?", (tx_id,)).fetchone()
    return row["category"] if row else ""


# ─────────────────────────────────────────────
# field_match
# ─────────────────────────────────────────────


def test_field_match_amazon_points_full(mem_db):
    """bank='Amazon' AND debit=0 → '個人支出'。他 bank の 0 円行は対象外。"""
    a = _insert_tx(mem_db, "Amazon", debit=0, desc="ポイント購入")
    b = _insert_tx(mem_db, "VPASS",  debit=0, desc="ゼロ円")  # 違う bank
    c = _insert_tx(mem_db, "Amazon", debit=500, desc="現金購入")  # 違う debit
    rule = AR.FieldMatchRule(
        id="amz0", description="", where_unset_only=True,
        when={"bank": "Amazon", "debit": 0}, set_category="個人支出",
    )
    n = AR.apply_field_match(mem_db, rule)
    assert n == 1
    assert _category_of(mem_db, a) == "個人支出"
    assert _category_of(mem_db, b) == ""
    assert _category_of(mem_db, c) == ""


def test_field_match_skips_already_classified(mem_db):
    """where_unset_only=True なら既存 category を上書きしない。"""
    a = _insert_tx(mem_db, "Amazon", debit=0)
    mem_db.execute("UPDATE transactions SET category='経費' WHERE id=?", (a,))
    rule = AR.FieldMatchRule(id="x", when={"bank": "Amazon", "debit": 0},
                             set_category="個人支出", where_unset_only=True)
    n = AR.apply_field_match(mem_db, rule)
    assert n == 0
    assert _category_of(mem_db, a) == "経費"


# ─────────────────────────────────────────────
# keyword_match
# ─────────────────────────────────────────────


def test_keyword_match_resolves_via_callable(mem_db):
    """keyword_resolver(name) で取った keywords にヒットしたら category 付与。"""
    a = _insert_tx(mem_db, "MUFG", debit=10000, desc="フリコミ ヤチン")
    b = _insert_tx(mem_db, "MUFG", debit=10000, desc="フリコミ ABC")
    c = _insert_tx(mem_db, "VPASS", debit=10000, desc="フリコミ ヤチン")  # bank 違い

    rule = AR.KeywordMatchRule(
        id="out", when={"bank": "MUFG", "debit_gt": 0},
        keyword_list="MY_OUT", field_name="description_normalized",
        set_category="出金",
    )
    n = AR.apply_keyword_match(mem_db, rule, lambda name: ["ヤチン"] if name == "MY_OUT" else [])
    assert n == 1
    assert _category_of(mem_db, a) == "出金"
    assert _category_of(mem_db, b) == ""
    assert _category_of(mem_db, c) == ""  # bank 違いで対象外


def test_keyword_match_empty_keywords_does_nothing(mem_db):
    a = _insert_tx(mem_db, "MUFG", debit=1000, desc="x")
    rule = AR.KeywordMatchRule(id="x", when={"bank": "MUFG"},
                                keyword_list="EMPTY", field_name="description_normalized",
                                set_category="出金")
    n = AR.apply_keyword_match(mem_db, rule, lambda name: [])
    assert n == 0
    assert _category_of(mem_db, a) == ""


# ─────────────────────────────────────────────
# pattern_match
# ─────────────────────────────────────────────


def test_pattern_match_suica(mem_db):
    """VPASS の Suica チャージ → 出金。"""
    a = _insert_tx(mem_db, "VPASS", debit=3000, desc="モバイル Suica チャージ")
    b = _insert_tx(mem_db, "VPASS", debit=3000, desc="ゲーム購入")
    c = _insert_tx(mem_db, "MUFG", debit=3000, desc="Suica")  # bank 違い

    rule = AR.PatternMatchRule(
        id="suica", when={"bank": "VPASS"},
        pattern="%Suica%", field_name="description_normalized",
        set_category="出金",
    )
    n = AR.apply_pattern_match(mem_db, rule)
    assert n == 1
    assert _category_of(mem_db, a) == "出金"
    assert _category_of(mem_db, b) == ""
    assert _category_of(mem_db, c) == ""


# ─────────────────────────────────────────────
# propagate (隣接行カテゴリ継承)
# ─────────────────────────────────────────────


def test_propagate_inherits_prev_same_bank_same_date(mem_db):
    """ATM 出金 → 直後の手数料行が出金カテゴリを継承する。"""
    a = _insert_tx(mem_db, "MUFG", debit=30000, desc="ATM 引出", date="2026/05/01")
    mem_db.execute("UPDATE transactions SET category='出金' WHERE id=?", (a,))
    fee = _insert_tx(mem_db, "MUFG", debit=220, desc="手数料", date="2026/05/01")

    rule = AR.PropagateRule(
        id="fee", target_patterns=("手数料",),
        field_name="description_normalized", inherit_from="prev_same_bank_same_date",
    )
    n = AR.apply_propagate(mem_db, rule)
    assert n == 1
    assert _category_of(mem_db, fee) == "出金"


def test_propagate_skips_when_no_prev_classified(mem_db):
    """直前同行同日に classified 行が無ければ何もしない。"""
    fee = _insert_tx(mem_db, "MUFG", debit=220, desc="手数料", date="2026/05/01")
    rule = AR.PropagateRule(id="x", target_patterns=("手数料",),
                            field_name="description_normalized",
                            inherit_from="prev_same_bank_same_date")
    n = AR.apply_propagate(mem_db, rule)
    assert n == 0
    assert _category_of(mem_db, fee) == ""


# ─────────────────────────────────────────────
# load_rules / apply_all
# ─────────────────────────────────────────────


def test_load_rules_returns_typed_objects():
    """本番 TOML が dataclass にマップされる。"""
    rules = AR.load_rules()
    assert any(isinstance(r, AR.FieldMatchRule) for r in rules)
    assert any(isinstance(r, AR.KeywordMatchRule) for r in rules)
    assert any(isinstance(r, AR.PatternMatchRule) for r in rules)
    assert any(isinstance(r, AR.PropagateRule) for r in rules)


def test_apply_all_amazon_zero_via_real_toml(mem_db):
    """実 config/auto_rules.toml で Amazon 0 円行が 個人支出 になる (apply_all 経由)。"""
    a = _insert_tx(mem_db, "Amazon", debit=0, desc="ポイント全額")
    AR.apply_all(mem_db, lambda: None)
    assert _category_of(mem_db, a) == "個人支出"


def test_when_clause_builder_supports_all_ops():
    sql, params = AR._build_when_clause({"bank": "MUFG", "debit_gt": 0})
    assert "bank = ?" in sql
    assert "debit > ?" in sql
    assert params == ["MUFG", 0]


def test_when_clause_builder_rejects_unknown_key():
    import pytest
    with pytest.raises(ValueError):
        AR._build_when_clause({"unknown_field": "x"})
