"""
単品購入テーブル (order_items) とレシートテーブル (invoice_receipts) の管理。

order_items: ショップ横断の個別商品行。amazon_order_items の汎用版。
invoice_receipts: PDFレシート1件1行。shop_tx_id / card_tx_id で突合を記録。
                  Amazon Kindle のまとめ請求突合にも使用。
"""
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).parent.parent
DB_PATH = _ROOT / "transactions.db"
_INVOICE_ROOT = _ROOT / "invoices"

# shop name → invoices サブディレクトリ（PDFが格納されている場所）
_SHOP_DIRS: dict[str, list[Path]] = {
    "Amazon":   [_INVOICE_ROOT / "amazon"],
    "楽天市場": [_INVOICE_ROOT / "rakuten"],
    "メルカリ": [_INVOICE_ROOT / "mercari" / "purchases"],
}


# ─────────────────────────────────────────────
# DDL
# ─────────────────────────────────────────────

def _ensure_tables(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS order_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            bank        TEXT NOT NULL,
            order_id    TEXT NOT NULL,
            item_name   TEXT NOT NULL,
            unit_price  INTEGER NOT NULL DEFAULT 0,
            quantity    INTEGER NOT NULL DEFAULT 1,
            subtotal    INTEGER NOT NULL DEFAULT 0,
            is_digital  INTEGER NOT NULL DEFAULT 0,
            tx_id       INTEGER REFERENCES transactions(id),
            fetched_at  TEXT,
            UNIQUE(bank, order_id, item_name, unit_price)
        );

        CREATE TABLE IF NOT EXISTS invoice_receipts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            bank            TEXT NOT NULL,
            order_id        TEXT NOT NULL,
            invoice_date    TEXT,
            order_total     INTEGER NOT NULL DEFAULT 0,
            pdf_path        TEXT,
            charged_amount  INTEGER,
            charged_card    TEXT,
            shop_tx_id      INTEGER REFERENCES transactions(id),
            card_tx_id      INTEGER REFERENCES transactions(id),
            matched_at      TEXT,
            UNIQUE(bank, order_id)
        );
    """)
    con.commit()


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


# ─────────────────────────────────────────────
# PDF ファイル名から (date, order_id) を解析
# ─────────────────────────────────────────────

def _parse_pdf_name(name: str) -> tuple[str, str] | None:
    """
    例:
      2026-01-02_D01-xxx.pdf           → ("2026/01/02", "D01-xxx")
      2026-04-12_331717-20260412-xxx.pdf → ("2026/04/12", "331717-20260412-xxx")
      2026-02-23_m10038063893.pdf      → ("2026/02/23", "m10038063893")
    """
    stem = Path(name).stem
    m = re.match(r"(\d{4}-\d{2}-\d{2})_(.+)", stem)
    if not m:
        return None
    date_str = m.group(1).replace("-", "/")
    order_id = m.group(2)
    return date_str, order_id


# ─────────────────────────────────────────────
# PDF ファイル群から invoice_receipts を生成
# ─────────────────────────────────────────────

def _find_tx_id(con: sqlite3.Connection, bank: str, order_id: str) -> int | None:
    """transactions テーブルから order_id に対応する行を探す。"""
    row = con.execute(
        "SELECT id FROM transactions WHERE bank=? AND description LIKE ? LIMIT 1",
        (bank, f"%{order_id}%"),
    ).fetchone()
    return row["id"] if row else None


def populate_invoice_receipts(verbose: bool = True) -> int:
    """
    invoices/ 以下の PDF ファイルを走査して invoice_receipts に登録する。
    order_total は transactions テーブルの debit から取る。
    既存行は IGNORE（重複登録しない）。
    戻り値: 新規登録件数。
    """
    con = _db()
    _ensure_tables(con)

    inserted = 0
    for bank, dirs in _SHOP_DIRS.items():
        for base_dir in dirs:
            if not base_dir.exists():
                continue
            for pdf in sorted(base_dir.rglob("*.pdf")):
                parsed = _parse_pdf_name(pdf.name)
                if not parsed:
                    continue
                invoice_date, order_id = parsed
                rel_path = str(pdf.relative_to(_ROOT))

                tx_id = _find_tx_id(con, bank, order_id)
                order_total = 0
                if tx_id:
                    # Amazon はポイント控除後の order_total を優先
                    aod_row = con.execute(
                        "SELECT order_total FROM amazon_order_details WHERE order_id=? AND order_total > 0",
                        (order_id,),
                    ).fetchone()
                    if aod_row:
                        order_total = aod_row["order_total"]
                    else:
                        row = con.execute(
                            "SELECT debit FROM transactions WHERE id=?", (tx_id,)
                        ).fetchone()
                        if row:
                            order_total = row["debit"]

                cur = con.execute(
                    """INSERT OR IGNORE INTO invoice_receipts
                       (bank, order_id, invoice_date, order_total, pdf_path, shop_tx_id)
                       VALUES (?,?,?,?,?,?)""",
                    (bank, order_id, invoice_date, order_total, rel_path, tx_id),
                )
                inserted += cur.rowcount

    con.commit()
    if verbose:
        total = con.execute("SELECT COUNT(*) FROM invoice_receipts").fetchone()[0]
        print(f"[invoices] {inserted} 件追加（合計 {total} 件）")
    con.close()
    return inserted


# ─────────────────────────────────────────────
# amazon_order_items → order_items へのマイグレーション
# ─────────────────────────────────────────────

def migrate_amazon_order_items(verbose: bool = True) -> int:
    """
    既存の amazon_order_items テーブルを order_items へコピーする。
    tx_id は transactions テーブルから order_id で引く。
    """
    con = _db()
    _ensure_tables(con)

    rows = con.execute("""
        SELECT ai.order_id, ai.seq, ai.title, ai.price, ai.is_kindle,
               ad.order_date
        FROM amazon_order_items ai
        LEFT JOIN amazon_order_details ad ON ai.order_id = ad.order_id
    """).fetchall()

    inserted = 0
    for r in rows:
        tx_id = _find_tx_id(con, "Amazon", r["order_id"])
        cur = con.execute(
            """INSERT OR IGNORE INTO order_items
               (bank, order_id, item_name, unit_price, quantity, subtotal,
                is_digital, tx_id, fetched_at)
               VALUES ('Amazon',?,?,?,1,?,?,?,?)""",
            (r["order_id"], r["title"] or "", r["price"], r["price"],
             r["is_kindle"], tx_id, r["order_date"]),
        )
        inserted += cur.rowcount

    con.commit()
    if verbose:
        total = con.execute(
            "SELECT COUNT(*) FROM order_items WHERE bank='Amazon'"
        ).fetchone()[0]
        print(f"[invoices] order_items: {inserted} 件追加（Amazon合計 {total} 件）")
    con.close()
    return inserted


# ─────────────────────────────────────────────
# Kindle まとめ請求の突合
# ─────────────────────────────────────────────

def _parse_date(s: str) -> date:
    parts = [int(x) for x in s.split("/")]
    return date(parts[0], parts[1], parts[2])


def match_kindle_bundles(day_window: int = 10, verbose: bool = True) -> int:
    """
    D01- の invoice_receipts をカード明細（VPASS / MUFGAmex）と突合する。

    戦略:
      1. 1:1 突合 — order_total == card.debit かつ日付が近い（同日〜+10日）。
         各カード行は1件のKindleにのみ割り当て（先着順）。
      2. グループ突合 — 同日注文の合計 == card.debit。

    day_window: 注文日からカード請求日までの最大日数
    戻り値: 突合件数
    """
    con = _db()
    _ensure_tables(con)

    # 未突合の D01- レシートを取得
    kindle_rows = con.execute("""
        SELECT id, order_id, invoice_date, order_total
        FROM invoice_receipts
        WHERE bank='Amazon' AND order_id LIKE 'D01-%'
          AND card_tx_id IS NULL AND order_total > 0
        ORDER BY invoice_date
    """).fetchall()

    if not kindle_rows:
        if verbose:
            print("[invoices] 突合対象の Kindle レシートなし")
        con.close()
        return 0

    # カード明細（Amazon キーワード付き）を取得。YYYY/MM/DD 形式のまま。
    card_rows = con.execute("""
        SELECT id, bank AS card_bank, date, debit
        FROM transactions
        WHERE bank IN ('VPASS', 'MUFGAmex')
          AND (description_normalized LIKE '%AMAZON%' OR description_normalized LIKE '%アマゾン%')
          AND debit > 0
        ORDER BY date
    """).fetchall()

    # 使用済みカード行ID（重複割り当て防止）
    used_card_ids: set[int] = set()

    def _date_in_window(inv_date_str: str, card_date_str: str) -> bool:
        """どちらも YYYY/MM/DD。inv_date から [−2, +day_window] 日以内か。"""
        d_inv  = _parse_date(inv_date_str)
        d_card = _parse_date(card_date_str)
        delta  = (d_card - d_inv).days
        return -2 <= delta <= day_window

    matched = 0

    # ── ステップ1: 1:1 突合 ──────────────────────────────────────────
    for row in kindle_rows:
        inv_date   = row["invoice_date"]
        order_total = row["order_total"]
        for card in card_rows:
            if card["id"] in used_card_ids:
                continue
            if card["debit"] != order_total:
                continue
            if not _date_in_window(inv_date, card["date"]):
                continue
            # 突合成立
            con.execute(
                """UPDATE invoice_receipts
                   SET card_tx_id=?, charged_amount=?, charged_card=?,
                       matched_at=datetime('now','localtime')
                   WHERE id=?""",
                (card["id"], card["debit"], card["card_bank"], row["id"]),
            )
            used_card_ids.add(card["id"])
            matched += 1
            if verbose:
                print(f"[invoices] Kindle 1:1 {inv_date} ¥{order_total:,} → {card['card_bank']} {card['date']}")
            break

    # ── ステップ2: グループ突合（1:1 未突合のもの）─────────────────────
    from collections import defaultdict
    remaining = con.execute("""
        SELECT id, order_id, invoice_date, order_total
        FROM invoice_receipts
        WHERE bank='Amazon' AND order_id LIKE 'D01-%'
          AND card_tx_id IS NULL AND order_total > 0
        ORDER BY invoice_date
    """).fetchall()

    by_date: dict[str, list] = defaultdict(list)
    for r in remaining:
        by_date[r["invoice_date"]].append(r)

    for inv_date, group in by_date.items():
        group_total = sum(r["order_total"] for r in group)
        for card in card_rows:
            if card["id"] in used_card_ids:
                continue
            if card["debit"] != group_total:
                continue
            if not _date_in_window(inv_date, card["date"]):
                continue
            for r in group:
                con.execute(
                    """UPDATE invoice_receipts
                       SET card_tx_id=?, charged_amount=?, charged_card=?,
                           matched_at=datetime('now','localtime')
                       WHERE id=?""",
                    (card["id"], card["debit"], card["card_bank"], r["id"]),
                )
            used_card_ids.add(card["id"])
            matched += len(group)
            if verbose:
                print(f"[invoices] Kindle グループ {inv_date} {len(group)}件 ¥{group_total:,} → {card['card_bank']} {card['date']}")
            break

    con.commit()
    con.close()
    if verbose:
        print(f"[invoices] Kindle 突合合計: {matched} 件")
    return matched


# ─────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────

if __name__ == "__main__":
    populate_invoice_receipts()
    migrate_amazon_order_items()
    match_kindle_bundles()
