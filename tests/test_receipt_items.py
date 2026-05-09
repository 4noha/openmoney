"""receipt_items の集計分割 (= 1 receipt → 複数事業者) regression test。

水道料金レシートのように 1 枚に上水道料金 (T7800020002178) と下水道料金
(T8800020002177) が併記されるケースで、 compute_invoice_summary が正しく
別事業者として集計分割することを保証。
"""
from __future__ import annotations

import sqlite3

import pytest

from src.aoiro.invoice_summary import compute_invoice_summary, _collect_breakdowns


def _setup(con: sqlite3.Connection) -> None:
    """テスト用ミニスキーマ。"""
    con.executescript("""
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY,
            bank TEXT, date TEXT, description TEXT,
            description_normalized TEXT,
            debit INTEGER DEFAULT 0, credit INTEGER DEFAULT 0,
            category TEXT
        );
        CREATE TABLE receipts (
            id INTEGER PRIMARY KEY,
            filename TEXT, receipt_date TEXT, merchant TEXT,
            invoice_number TEXT, amount INTEGER, transaction_id INTEGER,
            raw_json TEXT, saved_at TEXT
        );
        CREATE TABLE receipt_links (
            receipt_id INTEGER, transaction_id INTEGER,
            PRIMARY KEY (receipt_id, transaction_id)
        );
        CREATE TABLE receipt_items (
            id INTEGER PRIMARY KEY, receipt_id INTEGER,
            label TEXT, merchant TEXT, invoice_number TEXT,
            amount INTEGER, sort_order INTEGER DEFAULT 0
        );
        CREATE TABLE invoice_vendors (
            id INTEGER PRIMARY KEY, service_name TEXT, company_name TEXT,
            invoice_number TEXT
        );
    """)


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _setup(c)
    yield c
    c.close()


def _insert_water_receipt(con, *, tx_id, tx_date, amount, ueki, geki):
    """水道料金 1 件 (= 上水 ueki + 下水 geki) を挿入。"""
    con.execute(
        "INSERT INTO transactions (id, bank, date, description, description_normalized, debit, category) "
        "VALUES (?, 'MUFG', ?, '水道 クサツシ スイドウ', '水道 クサツシ スイドウ', ?, '経費')",
        (tx_id, tx_date, amount),
    )
    con.execute(
        "INSERT INTO receipts (id, filename, receipt_date, merchant, invoice_number, amount) "
        "VALUES (?, 'water.jpg', ?, '草津市水道お客様センター', 'T7800020002178', ?)",
        (tx_id + 1000, tx_date, amount),
    )
    con.execute("INSERT INTO receipt_links VALUES (?, ?)", (tx_id + 1000, tx_id))
    con.execute(
        "INSERT INTO receipt_items (receipt_id, label, merchant, invoice_number, amount, sort_order) "
        "VALUES (?, '上水道料金', '草津市水道事業会計', 'T7800020002178', ?, 0), "
        "       (?, '下水道料金', '草津市下水道事業会計', 'T8800020002177', ?, 1)",
        (tx_id + 1000, ueki, tx_id + 1000, geki),
    )
    con.commit()


def test_collect_breakdowns_matches(con):
    """内訳合計が tx 金額と一致 → 内訳リストを返す。"""
    _insert_water_receipt(con, tx_id=1, tx_date="2026/04/02", amount=6426,
                            ueki=2950, geki=3476)
    bd = _collect_breakdowns(con, 1, 6426)
    assert bd is not None
    assert len(bd) == 2
    assert {b["invoice_number"] for b in bd} == {"T7800020002178", "T8800020002177"}


def test_collect_breakdowns_mismatch_returns_none(con):
    """内訳合計が tx 金額と不一致 → None (= 親 receipt ベース fallback)。"""
    _insert_water_receipt(con, tx_id=1, tx_date="2026/04/02", amount=6426,
                            ueki=2950, geki=3476)  # 合計 6426
    bd = _collect_breakdowns(con, 1, 9999)  # 不一致
    assert bd is None


def test_summary_splits_by_invoice(con):
    """compute_invoice_summary は内訳ごとに別事業者として集計する。"""
    _insert_water_receipt(con, tx_id=1, tx_date="2026/04/02", amount=6426,
                            ueki=2950, geki=3476)
    _insert_water_receipt(con, tx_id=2, tx_date="2026/02/02", amount=7178,
                            ueki=3306, geki=3872)

    s = compute_invoice_summary(con, 2026)

    # 2 receipts × 2 内訳 = 4 行が with_invoice として集計
    assert s["with_invoice"]["count"] == 4
    assert s["with_invoice"]["total_tax_in"] == 6426 + 7178

    # by_vendor で別事業者として分かれる
    invoices = {v["invoice_number"]: v for v in s["by_vendor"]}
    assert "T7800020002178" in invoices
    assert "T8800020002177" in invoices
    # 上水道計 = 2950 + 3306 = 6256
    assert invoices["T7800020002178"]["total"] == 2950 + 3306
    # 下水道計 = 3476 + 3872 = 7348
    assert invoices["T8800020002177"]["total"] == 3476 + 3872


def test_summary_falls_back_when_no_items(con):
    """receipt_items が無い tx は親 receipt の invoice_number で集計 (旧挙動互換)。"""
    con.execute(
        "INSERT INTO transactions (id, bank, date, description_normalized, debit, category) "
        "VALUES (1, 'VPASS', '2026/03/15', 'foo', 1100, '経費')"
    )
    con.execute(
        "INSERT INTO receipts (id, filename, receipt_date, merchant, invoice_number, amount) "
        "VALUES (101, 'r.jpg', '2026/03/15', 'Foo Co', 'T9999', 1100)"
    )
    con.execute("INSERT INTO receipt_links VALUES (101, 1)")
    con.commit()

    s = compute_invoice_summary(con, 2026)
    assert s["with_invoice"]["count"] == 1
    assert s["with_invoice"]["total_tax_in"] == 1100
