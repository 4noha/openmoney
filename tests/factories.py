"""テスト用サンプルデータ挿入ヘルパー。

`seed_db` fixture や個別テストから呼んで使う。
実際のスクレイパーが書くデータに近い形式にしてある。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime


_NOW = datetime.now().isoformat()


def insert_tx(
    con: sqlite3.Connection,
    bank: str,
    date: str,
    description: str,
    debit: int = 0,
    credit: int = 0,
    balance: int = 0,
    category: str = "",
    description_normalized: str | None = None,
) -> int:
    cur = con.execute(
        """INSERT OR IGNORE INTO transactions
           (bank, date, description, description_normalized,
            debit, credit, balance, fetched_at, category)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            bank, date, description,
            description_normalized if description_normalized is not None else description,
            debit, credit, balance, _NOW, category,
        ),
    )
    if cur.lastrowid:
        return cur.lastrowid
    # UNIQUE 衝突時は既存行の id を返す
    row = con.execute(
        "SELECT id FROM transactions WHERE bank=? AND date=? AND description=? AND debit=? AND credit=?",
        (bank, date, description, debit, credit),
    ).fetchone()
    return row[0]


def insert_receipt(
    con: sqlite3.Connection,
    merchant: str,
    amount: int,
    receipt_date: str,
    invoice_number: str = "",
    filename: str | None = None,
    transaction_id: int | None = None,
) -> int:
    cur = con.execute(
        """INSERT INTO receipts
           (filename, receipt_date, merchant, invoice_number, amount, transaction_id)
           VALUES (?,?,?,?,?,?)""",
        (filename, receipt_date, merchant, invoice_number, amount, transaction_id),
    )
    return cur.lastrowid


def insert_tx_link(
    con: sqlite3.Connection,
    tx_a_id: int | None,
    tx_b_id: int | None,
    link_type: str,
    diff: int = 0,
) -> int:
    cur = con.execute(
        """INSERT OR IGNORE INTO tx_links (tx_a_id, tx_b_id, link_type, diff)
           VALUES (?,?,?,?)""",
        (tx_a_id, tx_b_id, link_type, diff),
    )
    return cur.lastrowid or 0


# ─────────────────────────────────────────────
# シナリオ別ファクトリ
# ─────────────────────────────────────────────

def seed_mufg_vpass(con: sqlite3.Connection) -> dict[str, int]:
    """MUFG 口座振替 → VPASS 月次請求のマッチングシナリオ。"""
    # MUFG 側: 4 月分の VPASS 引き落とし
    mufg_id = insert_tx(
        con, "MUFG", "2026/04/26",
        "口座振替 ミツイスミトモカ－ド",
        debit=52_800,
        description_normalized="口座振替 ミツイスミトモカ－ド",
    )
    # VPASS 側: 3 月の利用明細
    v1 = insert_tx(con, "VPASS", "2026/03/10", "Amazon.co.jp", debit=12_800)
    v2 = insert_tx(con, "VPASS", "2026/03/18", "楽天市場", debit=22_000)
    v3 = insert_tx(con, "VPASS", "2026/03/25", "スターバックス", debit=18_000)
    con.commit()
    return {"mufg": mufg_id, "vpass": [v1, v2, v3]}


def seed_mufg_orico(con: sqlite3.Connection) -> dict[str, int]:
    """MUFG 口座振替 → Orico 月次請求のマッチングシナリオ。"""
    mufg_id = insert_tx(
        con, "MUFG", "2026/04/27",
        "口座振替 オリコ",
        debit=30_000,
        description_normalized="口座振替 オリコ",
    )
    o1 = insert_tx(con, "Orico", "2026/03/05", "ヨドバシカメラ", debit=30_000)
    con.commit()
    return {"mufg": mufg_id, "orico": [o1]}


def seed_amazon(con: sqlite3.Connection) -> dict[str, list[int]]:
    """Amazon 注文のデモシナリオ。

    シナリオ A: 物理商品 1 点 (配送料あり) + カード突合済み
    シナリオ B: Kindle 電子書籍 (is_kindle=1)
    シナリオ C: 複数商品 + ポイント利用 + 2 配送に分かれたカード突合
    シナリオ D: AmazonPay 外部加盟店

    各シナリオで amazon_order_details / amazon_order_items を正しく投入することで
    UI の「小計 / 送料 / ポイント」詳細行が表示される。
    """
    now = datetime.now().isoformat()

    # ── A: 物理商品 1 点 ──────────────────────────────────────────
    oid_a = "250-0000001-0000001"
    tx_a = insert_tx(
        con, "Amazon", "2026/03/10",
        f"[{oid_a}] Anker USB-C 充電器 65W (窒化ガリウム)",
        debit=3_280,
    )
    con.execute(
        "INSERT OR IGNORE INTO amazon_order_details"
        " (order_id, order_date, item_subtotal, shipping, order_total, items_fetched, fetched_at)"
        " VALUES (?,?,?,?,?,1,?)",
        (oid_a, "2026/03/10", 2_780, 500, 3_280, now),
    )
    con.execute(
        "INSERT OR IGNORE INTO amazon_order_items (order_id, seq, title, price, quantity, is_kindle)"
        " VALUES (?,1,?,?,1,0)",
        (oid_a, "Anker USB-C 充電器 65W 窒化ガリウム", 2_780),
    )
    # カード突合 (shop_card)
    card_a = insert_tx(
        con, "VPASS", "2026/03/11", "AMAZON CO JP",
        debit=3_280,
        description_normalized="AMAZON CO JP",
    )
    con.execute(
        "INSERT OR IGNORE INTO tx_links (tx_a_id, tx_b_id, link_type, diff) VALUES (?,?,?,?)",
        (tx_a, card_a, "shop_card", 0),
    )

    # ── B: Kindle 電子書籍 ───────────────────────────────────────
    oid_b = "250-0000002-0000002"
    tx_b = insert_tx(
        con, "Amazon", "2026/03/15",
        f"[{oid_b}] 鬼滅の刃 (1) Kindle版",
        debit=693,
    )
    con.execute(
        "INSERT OR IGNORE INTO amazon_order_details"
        " (order_id, order_date, item_subtotal, order_total, items_fetched, fetched_at)"
        " VALUES (?,?,?,?,1,?)",
        (oid_b, "2026/03/15", 693, 693, now),
    )
    con.execute(
        "INSERT OR IGNORE INTO amazon_order_items (order_id, seq, title, price, quantity, is_kindle)"
        " VALUES (?,1,?,?,1,1)",
        (oid_b, "鬼滅の刃 (1)", 693),
    )
    card_b = insert_tx(
        con, "VPASS", "2026/03/15", "AMAZON CO JP",
        debit=693,
        description_normalized="AMAZON CO JP",
    )
    con.execute(
        "INSERT OR IGNORE INTO tx_links (tx_a_id, tx_b_id, link_type, diff) VALUES (?,?,?,?)",
        (tx_b, card_b, "shop_card", 0),
    )

    # ── C: 複数商品 + ポイント利用 + 2 配送に分かれた amazon_split ───
    oid_c = "250-0000003-0000003"
    tx_c = insert_tx(
        con, "Amazon", "2026/03/20",
        f"[{oid_c}] Anker PowerBank 20000mAh ほか1点",
        debit=4_300,   # ポイント 500 P 利用後の請求額
    )
    con.execute(
        "INSERT OR IGNORE INTO amazon_order_details"
        " (order_id, order_date, item_subtotal, points_used, order_total, items_fetched, fetched_at)"
        " VALUES (?,?,?,?,?,1,?)",
        (oid_c, "2026/03/20", 4_800, 500, 4_300, now),
    )
    con.execute(
        "INSERT OR IGNORE INTO amazon_order_items (order_id, seq, title, price, quantity, is_kindle)"
        " VALUES (?,1,?,?,1,0),(?,2,?,?,1,0)",
        (oid_c, "Anker PowerBank 20000mAh", 3_000,
         oid_c, "USB-C ケーブル 2m", 1_800),
    )
    # 2 配送に分かれてカードに計上 → amazon_split
    card_c1 = insert_tx(
        con, "VPASS", "2026/03/21", "AMAZON CO JP",
        debit=3_000,
        description_normalized="AMAZON CO JP",
    )
    card_c2 = insert_tx(
        con, "VPASS", "2026/03/23", "AMAZON CO JP",
        debit=1_300,   # 1800 - 500P
        description_normalized="AMAZON CO JP",
    )
    con.execute(
        "INSERT OR IGNORE INTO tx_links (tx_a_id, tx_b_id, link_type, diff) VALUES (?,?,?,?)",
        (tx_c, card_c1, "amazon_split", 0),
    )
    con.execute(
        "INSERT OR IGNORE INTO tx_links (tx_a_id, tx_b_id, link_type, diff) VALUES (?,?,?,?)",
        (tx_c, card_c2, "amazon_split", 500),
    )

    # ── D: AmazonPay 外部加盟店 ──────────────────────────────────
    pay_d = insert_tx(
        con, "AmazonPay", "2026/03/25",
        "ヨドバシ.com",
        debit=12_800,
        description_normalized="ヨドバシ.com",
    )

    con.commit()
    return {
        "amazon_physical": [tx_a, card_a],
        "amazon_kindle":   [tx_b, card_b],
        "amazon_multi":    [tx_c, card_c1, card_c2],
        "amazon_pay":      [pay_d],
    }


def seed_receipt(con: sqlite3.Connection) -> dict[str, int]:
    """レシート OCR → カード明細リンクのシナリオ。"""
    tx_id = insert_tx(con, "VPASS", "2026/03/15", "イオン", debit=4_320)
    r_id = insert_receipt(
        con,
        merchant="イオンリテール株式会社",
        amount=4_320,
        receipt_date="2026/03/15",
        invoice_number="T1234567890123",
        filename="receipt_20260315.jpg",
        transaction_id=tx_id,
    )
    con.commit()
    return {"tx": tx_id, "receipt": r_id}


def seed_joyful_receipt(con: sqlite3.Connection) -> dict[str, int]:
    """ジョイフル本田（ホームセンター）レシート → VPASS iD 明細リンクのシナリオ。
    実際の money_forward2 データから移植。画像は invoices/receipts/ にバンドル済み。
    """
    tx_id = insert_tx(
        con,
        bank="VPASS",
        date="2026/03/21",
        description="ジョイフル本田 荒川沖店／ｉＤ",
        debit=11_712,
        description_normalized="ジョイフル本田 荒川沖店",
    )
    r_id = insert_receipt(
        con,
        merchant="株式会社ジョイフル本田",
        amount=11_712,
        receipt_date="2026/03/21",
        invoice_number="T6050001009303",
        filename="PXL_20260503_061902633.jpg",
        transaction_id=tx_id,
    )
    _JOYFUL_ITEMS = [
        ("アサダ ナイログ 30・R",       2_280),
        ("パナソニック 15A・20A",        848),
        ("エアコン用被覆銅管",           4_280),
        ("ワッシャー ユニクロ (4入)",    55),
        ("長ネジ ユニクロ M10X1",        210),
        ("ユニクロゆるみ止めナット",     148),
        ("シーティーアンカー CT1040",    136),
        ("ITハンガー ITL101",            2_688),
        ("レジ袋 Mサイズ",               3),
    ]
    for seq, (label, amount) in enumerate(_JOYFUL_ITEMS):
        con.execute(
            "INSERT INTO receipt_items (receipt_id, label, merchant, invoice_number, amount, sort_order)"
            " VALUES (?,?,?,?,?,?)",
            (r_id, label, "株式会社ジョイフル本田", "T6050001009303", amount, seq),
        )
    con.commit()
    return {"tx": tx_id, "receipt": r_id}


def seed_all(con: sqlite3.Connection) -> dict:
    """全シナリオを一括投入する。seed_db fixture から呼ばれる。"""
    result = {}
    result["mufg_vpass"] = seed_mufg_vpass(con)
    result["mufg_orico"] = seed_mufg_orico(con)
    result["amazon"] = seed_amazon(con)
    result["receipt"] = seed_receipt(con)
    result["joyful_receipt"] = seed_joyful_receipt(con)
    return result
