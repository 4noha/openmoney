"""仕訳ブレ判定 (= detect_category_conflict_keys) の regression test。

「経費」 と「個人支出」 の混在検知 + 年度内判定の動作を保証する。
過去に全期間判定だったため 2026 年表示中なのに 2025 年の個人支出に
引きずられて 2026 年経費 tx も conflict 判定される事象があった。
"""
from __future__ import annotations

import sqlite3

import pytest

from src.server.categorize import detect_category_conflict_keys


def _setup_tx_table(con: sqlite3.Connection) -> None:
    """テスト用に最小限の transactions テーブルを作る (= mem_db fixture を使うと
    ensure_schema 経由で本番スキーマが入るが、 ここでは独立したミニ schema を使い
    judgement ロジックだけ検証する)。"""
    con.execute("""
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bank TEXT,
            date TEXT,
            description TEXT,
            description_normalized TEXT,
            debit INTEGER DEFAULT 0,
            credit INTEGER DEFAULT 0,
            category TEXT
        )
    """)


def _insert(con, *, bank, date, desc_norm, category, debit=1000):
    con.execute(
        "INSERT INTO transactions (bank, date, description, description_normalized, "
        "debit, category) VALUES (?, ?, ?, ?, ?, ?)",
        (bank, date, desc_norm, desc_norm, debit, category),
    )


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _setup_tx_table(c)
    yield c
    c.close()


def test_detects_expense_vs_personal_mix(con):
    """同 (bank, desc_norm) で 経費 と 個人支出 が混在 → conflict 検出。"""
    _insert(con, bank="VPASS", date="2026/03/10", desc_norm="セルフX", category="経費")
    _insert(con, bank="VPASS", date="2026/03/15", desc_norm="セルフX", category="個人支出")
    keys = detect_category_conflict_keys(con)
    assert ("VPASS", "セルフX") in keys


def test_no_conflict_when_unified(con):
    """全件「経費」 → conflict なし。"""
    _insert(con, bank="VPASS", date="2026/03/10", desc_norm="セルフY", category="経費")
    _insert(con, bank="VPASS", date="2026/03/15", desc_norm="セルフY", category="経費")
    assert detect_category_conflict_keys(con) == set()


def test_kongai_excluded(con):
    """「今回は経費」 「今回は個人支出」 は意図的な一時分類なので conflict 対象外。"""
    _insert(con, bank="VPASS", date="2026/03/10", desc_norm="セルフZ", category="経費")
    _insert(con, bank="VPASS", date="2026/03/15", desc_norm="セルフZ", category="今回は個人支出")
    # 「経費」 vs 「今回は個人支出」 は 今回は系を除外するため非 conflict
    assert detect_category_conflict_keys(con) == set()


def test_shukin_excluded(con):
    """「出金」 (= 為替手数料 等が親振込に追従) は conflict 対象外。"""
    _insert(con, bank="MUFG", date="2026/03/10", desc_norm="手数料", category="経費")
    _insert(con, bank="MUFG", date="2026/03/15", desc_norm="手数料", category="出金")
    assert detect_category_conflict_keys(con) == set()


def test_year_scoped_no_conflict_within_year(con):
    """年度別判定: 2025 年は個人支出のみ、 2026 年は経費のみ → 各年度内では非 conflict。"""
    _insert(con, bank="VPASS", date="2025/12/10", desc_norm="セルフW", category="個人支出")
    _insert(con, bank="VPASS", date="2026/03/15", desc_norm="セルフW", category="経費")
    # 全期間判定だと conflict (= 旧挙動)
    assert ("VPASS", "セルフW") in detect_category_conflict_keys(con)
    # 2025 年のみで判定 → 経費がないので conflict なし
    assert detect_category_conflict_keys(con, year="2025") == set()
    # 2026 年のみで判定 → 個人支出がないので conflict なし
    assert detect_category_conflict_keys(con, year="2026") == set()


def test_year_scoped_conflict_within_year(con):
    """同年度内で 経費 + 個人支出 が混在 → 該当年度のみ conflict 判定。"""
    _insert(con, bank="VPASS", date="2026/01/10", desc_norm="セルフV", category="経費")
    _insert(con, bank="VPASS", date="2026/05/15", desc_norm="セルフV", category="個人支出")
    _insert(con, bank="VPASS", date="2025/03/10", desc_norm="セルフV", category="経費")  # 2025 は経費のみ
    assert detect_category_conflict_keys(con, year="2026") == {("VPASS", "セルフV")}
    assert detect_category_conflict_keys(con, year="2025") == set()


def test_empty_desc_normalized_excluded(con):
    """description_normalized が空の行は判定対象外 (= 曖昧マッチ防止)。"""
    con.execute(
        "INSERT INTO transactions (bank, date, description, description_normalized, "
        "debit, category) VALUES (?, ?, ?, ?, ?, ?)",
        ("VPASS", "2026/03/10", "blah", "", 100, "経費"),
    )
    con.execute(
        "INSERT INTO transactions (bank, date, description, description_normalized, "
        "debit, category) VALUES (?, ?, ?, ?, ?, ?)",
        ("VPASS", "2026/03/15", "blah", "", 100, "個人支出"),
    )
    assert detect_category_conflict_keys(con) == set()


def test_different_banks_independent(con):
    """同じ desc_norm でも bank が違えば別 group。"""
    _insert(con, bank="VPASS", date="2026/03/10", desc_norm="商品A", category="経費")
    _insert(con, bank="VPASS", date="2026/03/15", desc_norm="商品A", category="経費")
    _insert(con, bank="MUFG",  date="2026/03/10", desc_norm="商品A", category="個人支出")
    # それぞれ単独 category なので conflict なし
    assert detect_category_conflict_keys(con) == set()
