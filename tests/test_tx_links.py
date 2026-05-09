"""tx_links 統一表 (#19) のスキーマ + ヘルパーのテスト。

Phase 2 では旧テーブルと二重書き込みするので、ヘルパー単体の挙動を担保する。
読取側切り替え (Phase 4) のテストは別途。
"""
from __future__ import annotations

import json

from src.matching import _upsert_tx_link


def _insert_tx(con, bank: str, date: str, debit: int = 0, desc: str = "test") -> int:
    cur = con.execute(
        "INSERT INTO transactions (bank, date, description, debit, credit, fetched_at, category) "
        "VALUES (?,?,?,?,0,?,?)",
        (bank, date, desc, debit, "2026-05-08T00:00:00", ""),
    )
    return cur.lastrowid


def test_tx_links_table_exists(mem_db):
    """ensure_schema で tx_links テーブルが作られる。"""
    rows = mem_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tx_links'"
    ).fetchall()
    assert len(rows) == 1


def test_upsert_tx_link_two_sided(mem_db):
    """両側 tx_id を持つ shop_card 形式の link が UPSERT される。"""
    a = _insert_tx(mem_db, "Amazon", "2026/04/01", debit=1000, desc="ショップ購入")
    b = _insert_tx(mem_db, "VPASS",  "2026/04/02", debit=1000, desc="AMAZON 課金")

    _upsert_tx_link(mem_db, tx_a_id=a, tx_b_id=b, link_type="shop_card", diff=0)
    rows = mem_db.execute("SELECT * FROM tx_links").fetchall()
    assert len(rows) == 1
    assert rows[0]["link_type"] == "shop_card"
    assert rows[0]["tx_a_id"] == a
    assert rows[0]["tx_b_id"] == b

    # 同じキーで再呼び出し → idempotent (件数増えない)
    _upsert_tx_link(mem_db, tx_a_id=a, tx_b_id=b, link_type="shop_card", diff=10)
    rows = mem_db.execute("SELECT * FROM tx_links").fetchall()
    assert len(rows) == 1
    assert rows[0]["diff"] == 10  # diff は更新される


def test_upsert_tx_link_one_sided_monthly_aggregate(mem_db):
    """tx_b_id=None (月次集計) の link は (tx_a, link_type) 単位で 1 行に維持される。"""
    a = _insert_tx(mem_db, "MUFG", "2026/05/26", debit=28765, desc="口座振替 ミツイスミトモカード")
    extra = {"card_bank": "VPASS", "billing_month": "2026/04", "card_sum": 28765}

    _upsert_tx_link(mem_db, tx_a_id=a, tx_b_id=None, link_type="card_bank_billing",
                    diff=0, extra=extra)
    rows = mem_db.execute("SELECT * FROM tx_links").fetchall()
    assert len(rows) == 1
    assert rows[0]["tx_b_id"] is None
    assert json.loads(rows[0]["extra_json"])["billing_month"] == "2026/04"

    # 再度呼ぶと UPDATE される (NULL を含む UNIQUE は SQLite では別個扱いになるが
    # ヘルパーが既存行を SELECT で見つけて UPDATE する規約)
    _upsert_tx_link(mem_db, tx_a_id=a, tx_b_id=None, link_type="card_bank_billing",
                    diff=5, extra={**extra, "card_sum": 28760})
    rows = mem_db.execute("SELECT * FROM tx_links").fetchall()
    assert len(rows) == 1
    assert rows[0]["diff"] == 5
    assert json.loads(rows[0]["extra_json"])["card_sum"] == 28760


def test_upsert_tx_link_amazon_split_one_to_n(mem_db):
    """1 つの Amazon 親注文に対して N 件のカード明細が link されるケース。"""
    parent = _insert_tx(mem_db, "Amazon", "2026/04/01", debit=10000,
                        desc="[D01-1234567] 注文")
    card1 = _insert_tx(mem_db, "VPASS", "2026/04/02", debit=4000, desc="AMAZON 1")
    card2 = _insert_tx(mem_db, "VPASS", "2026/04/03", debit=6000, desc="AMAZON 2")

    _upsert_tx_link(mem_db, tx_a_id=parent, tx_b_id=card1, link_type="amazon_split",
                    extra={"order_id": "D01-1234567"})
    _upsert_tx_link(mem_db, tx_a_id=parent, tx_b_id=card2, link_type="amazon_split",
                    extra={"order_id": "D01-1234567"})

    rows = mem_db.execute(
        "SELECT * FROM tx_links WHERE tx_a_id=? AND link_type='amazon_split' ORDER BY tx_b_id",
        (parent,)
    ).fetchall()
    assert len(rows) == 2
    assert {r["tx_b_id"] for r in rows} == {card1, card2}


def test_tx_links_indexes_exist(mem_db):
    """検索高速化用のインデックスが ensure_schema で作られる。"""
    indexes = {
        r[0] for r in mem_db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tx_links'"
        ).fetchall()
    }
    assert "idx_tx_links_a" in indexes
    assert "idx_tx_links_b" in indexes
    assert "idx_tx_links_type" in indexes
