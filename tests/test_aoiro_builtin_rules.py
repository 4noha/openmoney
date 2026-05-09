"""aoiro builtin journal rules の seed regression test。

builtin rule (= 「家賃収入 → 4100 不動産収入」 「保険金 → 4002 雑収入」 等) が
冪等に upsert され、 マッチロジックも正しく動くことを保証する。

過去にユーザが「ニホンセーフテイ家賃収入が 4001 売上高勘定 に入っている」
と気付き、 income_rent ルール + 家賃収入 builtin rule を追加した経緯あり。
"""
from __future__ import annotations

import sqlite3

import pytest

from src.aoiro.schema import ensure_aoiro_schema
from src.aoiro.rules import seed_builtin_rules, matches


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_aoiro_schema(c)
    yield c
    c.close()


def _rule_by_name(con, name):
    r = con.execute(
        "SELECT * FROM aoiro_journal_rules WHERE name=?", (name,)
    ).fetchone()
    return dict(r) if r else None


def test_seeds_rent_income_rule(con):
    """家賃収入 → credit_account=4100 (不動産収入) ルールが seed される。"""
    seed_builtin_rules(con)
    r = _rule_by_name(con, "家賃収入 (不動産)")
    assert r is not None
    assert r["category_match"] == "家賃収入"
    assert r["credit_account"] == "4100"
    assert r["is_builtin"] == 1
    assert r["is_active"] == 1


def test_seeds_insurance_rule(con):
    """保険金 → credit_account=4002 (雑収入) ルールが seed される。"""
    seed_builtin_rules(con)
    r = _rule_by_name(con, "保険金 (雑収入)")
    assert r is not None
    assert r["category_match"] == "保険金"
    assert r["credit_account"] == "4002"


def test_seeds_sales_rule(con):
    """売上 → credit_account=4001 (売上高) ルールが既存の builtin として残る。"""
    seed_builtin_rules(con)
    r = _rule_by_name(con, "売上 (Stripe / 振込 等)")
    assert r is not None
    assert r["category_match"] == "売上"
    assert r["credit_account"] == "4001"


def test_idempotent(con):
    """seed_builtin_rules は冪等 (= 2 回呼んでも重複しない)。"""
    seed_builtin_rules(con)
    n_first = con.execute(
        "SELECT COUNT(*) FROM aoiro_journal_rules WHERE is_builtin=1"
    ).fetchone()[0]
    seed_builtin_rules(con)
    n_second = con.execute(
        "SELECT COUNT(*) FROM aoiro_journal_rules WHERE is_builtin=1"
    ).fetchone()[0]
    assert n_first == n_second


def test_matches_rent_income_only(con):
    """家賃収入 ルールは category='家賃収入' のみマッチする (= 売上 ルールに hit しない)。"""
    seed_builtin_rules(con)
    rent = _rule_by_name(con, "家賃収入 (不動産)")
    sales = _rule_by_name(con, "売上 (Stripe / 振込 等)")
    # category='家賃収入' のとき: rent ルールが match、 sales は match しない
    assert matches(rent,  category="家賃収入", tags=set(), bank="MUFG", description="x", amount=100000)
    assert not matches(sales, category="家賃収入", tags=set(), bank="MUFG", description="x", amount=100000)
    # category='売上' のとき: 逆
    assert matches(sales, category="売上", tags=set(), bank="MUFG", description="x", amount=100000)
    assert not matches(rent, category="売上", tags=set(), bank="MUFG", description="x", amount=100000)


def test_user_disabled_is_preserved(con):
    """ユーザが is_active=0 にした builtin rule は再 seed しても is_active が維持される。"""
    seed_builtin_rules(con)
    # ユーザが家賃収入ルールを無効化
    con.execute(
        "UPDATE aoiro_journal_rules SET is_active=0 WHERE name='家賃収入 (不動産)'"
    )
    con.commit()
    seed_builtin_rules(con)  # 再 seed (= 内容更新だが is_active は触らない仕様)
    r = _rule_by_name(con, "家賃収入 (不動産)")
    assert r["is_active"] == 0
