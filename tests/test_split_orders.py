"""split_orders の親→子分割ロジックのテスト。
mem_db (空 SQLite + ensure_schema) に最小限のデータを入れて呼ぶ。
"""
from __future__ import annotations

from datetime import datetime

from plugins.amazon.scraper.orders import split_orders


def _insert_parent(con, oid: str, debit: int, title: str = "title", date: str = "2026/01/02"):
    con.execute(
        "INSERT INTO transactions (bank, date, description, debit, credit, balance, fetched_at, category) "
        "VALUES (?,?,?,?,0,0,?,?)",
        ("Amazon", date, f"[{oid}] {title}", debit, datetime.now().isoformat(), ""),
    )


def _insert_item(con, oid: str, seq: int, price: int, qty: int = 1, title: str = "item"):
    con.execute(
        "INSERT INTO amazon_order_items (order_id, seq, title, price, quantity, is_kindle) "
        "VALUES (?,?,?,?,?,0)",
        (oid, seq, f"{title} {seq}", price, qty),
    )


def _insert_details(con, oid: str, item_subtotal: int = 0, shipping: int = 0,
                    discount: int = 0, points_used: int = 0, gift_card: int = 0,
                    order_total: int = 0, items_fetched: int = 1):
    con.execute(
        "INSERT INTO amazon_order_details (order_id, order_date, item_subtotal, "
        "shipping, discount, points_used, gift_card, order_total, items_fetched, fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (oid, "2026/01/02", item_subtotal, shipping, discount, points_used,
         gift_card, order_total, items_fetched, datetime.now().isoformat()),
    )


def test_split_multi_item_creates_subs(mem_db):
    """複数 item: parent.debit が cash 一致なら sub 行が price×qty で作られる。"""
    oid = "250-TEST-1"
    _insert_parent(mem_db, oid, debit=2000)
    _insert_details(mem_db, oid, item_subtotal=2000)
    _insert_item(mem_db, oid, 1, price=800, qty=1)
    _insert_item(mem_db, oid, 2, price=1200, qty=1)
    mem_db.commit()

    split_orders(mem_db)
    rows = mem_db.execute(
        "SELECT description, debit FROM transactions WHERE description LIKE ?",
        (f"[{oid}/%",)
    ).fetchall()
    assert len(rows) == 2
    debits = sorted(r["debit"] for r in rows)
    assert debits == [800, 1200]


def test_split_zero_parent_creates_zero_subs(mem_db):
    """ポイント全額 (parent.debit=0) で複数 item: sub 行 debit=0 で作る。"""
    oid = "D01-TEST-Z"
    _insert_parent(mem_db, oid, debit=0)
    _insert_details(mem_db, oid, item_subtotal=66, points_used=66, order_total=0)
    _insert_item(mem_db, oid, 1, price=33, qty=1)
    _insert_item(mem_db, oid, 2, price=33, qty=1)
    mem_db.commit()

    split_orders(mem_db)
    rows = mem_db.execute(
        "SELECT debit FROM transactions WHERE description LIKE ?", (f"[{oid}/%",)
    ).fetchall()
    assert len(rows) == 2
    assert all(r["debit"] == 0 for r in rows)


def test_split_single_item_qty_appends_suffix(mem_db):
    """単品 × qty>1: サブ行を作らず親 description に ×N サフィックス追加。"""
    oid = "250-TEST-Q"
    _insert_parent(mem_db, oid, debit=6907, title="綾鷹 ×24本")
    _insert_details(mem_db, oid, item_subtotal=7056, points_used=149)
    _insert_item(mem_db, oid, 1, price=2352, qty=3, title="綾鷹")
    mem_db.commit()

    split_orders(mem_db)
    desc = mem_db.execute(
        "SELECT description FROM transactions WHERE description LIKE ? AND description NOT LIKE ?",
        (f"[{oid}]%", f"[{oid}/%")
    ).fetchone()["description"]
    assert desc.endswith(" ×3")
    # サブ行は作られない
    sub_count = mem_db.execute(
        "SELECT COUNT(*) FROM transactions WHERE description LIKE ?", (f"[{oid}/%",)
    ).fetchone()[0]
    assert sub_count == 0


def test_split_with_shipping_creates_shipping_row(mem_db):
    """parent.debit > Σ items: 差額が shipping と一致 → 「送料」ラベルの補完行。"""
    oid = "250-TEST-S"
    _insert_parent(mem_db, oid, debit=1300)
    _insert_details(mem_db, oid, item_subtotal=1100, shipping=200)
    _insert_item(mem_db, oid, 1, price=600, qty=1)
    _insert_item(mem_db, oid, 2, price=500, qty=1)
    mem_db.commit()

    split_orders(mem_db)
    ship = mem_db.execute(
        "SELECT description, debit FROM transactions WHERE description LIKE ?",
        (f"[{oid}/送料]%",)
    ).fetchone()
    assert ship is not None
    assert ship["debit"] == 200


def test_split_idempotent(mem_db):
    """split_orders を 2 回呼んでも結果は変わらない (UPSERT 動作)。"""
    oid = "250-TEST-I"
    _insert_parent(mem_db, oid, debit=1000)
    _insert_details(mem_db, oid, item_subtotal=1000)
    _insert_item(mem_db, oid, 1, price=400)
    _insert_item(mem_db, oid, 2, price=600)
    mem_db.commit()

    split_orders(mem_db)
    first_count = mem_db.execute(
        "SELECT COUNT(*) FROM transactions WHERE description LIKE ?", (f"[{oid}/%",)
    ).fetchone()[0]
    split_orders(mem_db)
    second_count = mem_db.execute(
        "SELECT COUNT(*) FROM transactions WHERE description LIKE ?", (f"[{oid}/%",)
    ).fetchone()[0]
    assert first_count == second_count == 2
