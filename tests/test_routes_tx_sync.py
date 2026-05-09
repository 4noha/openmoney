"""tx category 変更時の関連テーブル同期 (= _sync_tax_expense_items) regression test。

ユーザが「経費 → 個人支出」 等に分類を直しても tax_expense_items に経費時代の
レコードが残ると、 health check の tax_source_not_expense 警告が drift として
残る事象があった。 api_set_category / bulk-category 内でコード側で恒久同期する。
"""
from __future__ import annotations

import sqlite3

import pytest

from src.server.routes_tx import _sync_tax_expense_items


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""
        CREATE TABLE tax_expense_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year TEXT,
            amount INTEGER DEFAULT 0,
            source_tx_id INTEGER
        )
    """)
    # source_tx_id 100 / 101 / 102 を持つ tax_expense_items 行を 3 件挿入
    c.executemany(
        "INSERT INTO tax_expense_items (year, amount, source_tx_id) VALUES (?, ?, ?)",
        [("2026", 1000, 100), ("2026", 2000, 101), ("2026", 3000, 102)],
    )
    c.commit()
    yield c
    c.close()


def _count(con, source_tx_ids):
    placeholders = ",".join("?" * len(source_tx_ids))
    return con.execute(
        f"SELECT COUNT(*) FROM tax_expense_items WHERE source_tx_id IN ({placeholders})",
        source_tx_ids,
    ).fetchone()[0]


def test_keeps_when_kept_as_keihi(con):
    """category='経費' のままなら削除しない。"""
    n = _sync_tax_expense_items(con, [100], "経費")
    assert n == 0
    assert _count(con, [100]) == 1


def test_keeps_when_konkaiwa_keihi(con):
    """category='今回は経費' は経費として扱うので削除しない。"""
    n = _sync_tax_expense_items(con, [101], "今回は経費")
    assert n == 0
    assert _count(con, [101]) == 1


def test_deletes_when_changed_to_personal(con):
    """category='個人支出' に変わったら削除 (= drift 防止)。"""
    n = _sync_tax_expense_items(con, [100], "個人支出")
    assert n == 1
    assert _count(con, [100]) == 0


def test_deletes_when_changed_to_konkaiwa_personal(con):
    """category='今回は個人支出' でも削除 (= 経費系から外れた)。"""
    n = _sync_tax_expense_items(con, [101], "今回は個人支出")
    assert n == 1
    assert _count(con, [101]) == 0


def test_deletes_when_changed_to_shukin(con):
    """category='出金' に変わっても削除。"""
    n = _sync_tax_expense_items(con, [102], "出金")
    assert n == 1
    assert _count(con, [102]) == 0


def test_bulk_delete(con):
    """複数 tx 一括削除。"""
    n = _sync_tax_expense_items(con, [100, 101, 102], "個人支出")
    assert n == 3
    assert _count(con, [100, 101, 102]) == 0


def test_empty_tx_ids_noop(con):
    """tx_ids が空なら何もしない。"""
    n = _sync_tax_expense_items(con, [], "個人支出")
    assert n == 0


def test_unrelated_source_tx_not_affected(con):
    """関係ない source_tx_id (= 999) を渡しても他の行は触らない。"""
    n = _sync_tax_expense_items(con, [999], "個人支出")
    assert n == 0
    assert _count(con, [100, 101, 102]) == 3
