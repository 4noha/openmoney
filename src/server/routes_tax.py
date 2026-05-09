"""確定申告 インボイス管理（事業者マスター・経費明細）のルート群。"""
from __future__ import annotations

import re
import sqlite3

from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from src.server import (
    app,
    DB_PATH,
    _YAHOO_SHOPPING_INVOICE_DIR,
    _RAKUTEN_INVOICE_DIR,
    _amazon_invoice_order_ids,
    _mercari_invoice_tx_ids,
    _generic_invoice_ids,
    _campfire_invoice_ids,
    _build_tax_html,
)


_CORP_PREFIXES = (
    '株式会社', '有限会社', '合同会社', '一般社団法人', '公益社団法人',
    '医療法人', 'NPO法人', '学校法人', '宗教法人', '社会福祉法人',
)


def _split_merchant(merchant: str) -> tuple[str, str]:
    """OCR merchant → (service_name, company_name)
    '株式会社ジョイフル本田 荒川沖店' → ('ジョイフル本田 荒川沖店', '株式会社ジョイフル本田 荒川沖店')
    'apollostation (株)REXオート ひたち野うしく' → ('apollostation ひたち野うしく', '株式会社REXオート')
    'ジョイフル本田 荒川沖店' → ('ジョイフル本田 荒川沖店', '')
    """
    for prefix in _CORP_PREFIXES:
        if merchant.startswith(prefix):
            rest = merchant[len(prefix):]
            if not rest.strip():
                return merchant, merchant
            brand = rest.strip()
            company_name = f'{prefix}{brand.split()[0]}' if brand else merchant
            return brand, company_name
    m_kabushiki = re.search(r'[\(（]株[\)）](\S+)', merchant)
    if m_kabushiki:
        company_name = f'株式会社{m_kabushiki.group(1)}'
        service_name = re.sub(r'\s*[\(（]株[\)）]\S+\s*', ' ', merchant).strip()
        return service_name, company_name
    return merchant, ''


def _tax_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS invoice_vendors (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            service_name   TEXT NOT NULL DEFAULT '',
            company_name   TEXT NOT NULL DEFAULT '',
            invoice_number TEXT NOT NULL DEFAULT ''
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS tax_expense_items (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            year           TEXT NOT NULL,
            service_name   TEXT NOT NULL DEFAULT '',
            company_name   TEXT NOT NULL DEFAULT '',
            invoice_number TEXT NOT NULL DEFAULT '',
            amount         INTEGER NOT NULL DEFAULT 0,
            payment_method TEXT NOT NULL DEFAULT 'card',
            date           TEXT NOT NULL DEFAULT '',
            description    TEXT NOT NULL DEFAULT '',
            source_tx_id   INTEGER,
            created_at     TEXT DEFAULT (datetime('now','localtime')),
            updated_at     TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    con.commit()
    vendor_cols = {r[1] for r in con.execute("PRAGMA table_info(invoice_vendors)")}
    if "service_name" not in vendor_cols:
        con.execute("""
            CREATE TABLE invoice_vendors_v2 (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                service_name   TEXT NOT NULL DEFAULT '',
                company_name   TEXT NOT NULL DEFAULT '',
                invoice_number TEXT NOT NULL DEFAULT ''
            )
        """)
        con.execute("""
            INSERT INTO invoice_vendors_v2 (id, service_name, company_name, invoice_number)
                SELECT id, company_name, '', invoice_number FROM invoice_vendors
        """)
        con.execute("DROP TABLE invoice_vendors")
        con.execute("ALTER TABLE invoice_vendors_v2 RENAME TO invoice_vendors")
        con.commit()
    item_cols = {r[1] for r in con.execute("PRAGMA table_info(tax_expense_items)")}
    if "source_tx_id" not in item_cols:
        con.execute("ALTER TABLE tax_expense_items ADD COLUMN source_tx_id INTEGER")
    if "service_name" not in item_cols:
        con.execute("ALTER TABLE tax_expense_items ADD COLUMN service_name TEXT NOT NULL DEFAULT ''")
    _KNOWN_BANKS = ("Amazon", "楽天市場", "Mercari", "Mercari売上",
                    "Yahoo!ショッピング", "CAMPFIRE", "Makuake",
                    "ヤフオク購入", "ヤフオク売上", "VPASS", "MUFGAmex", "Orico", "MUFG")
    for bank in _KNOWN_BANKS:
        con.execute(
            "UPDATE tax_expense_items SET service_name=company_name, company_name='' "
            "WHERE service_name='' AND company_name=? AND source_tx_id IS NOT NULL",
            (bank,),
        )
    con.commit()
    return con


def _detect_payment_method(con: sqlite3.Connection, tx_id: int, bank: str,
                           has_receipt: bool) -> str:
    """取引の payment_method を推定する。
    card   : VPASS/MUFGAmex/Orico、または tx_links に shop_card link がある
    bank   : MUFG（口座振替）
    receipt: 紙レシートで上記いずれにも該当しない（現金等）

    #19 Phase 4-A: shop_card_matches → tx_links に切替。link_type='shop_card' で
    tx_a_id (shop 側 transaction) が紐付いた link が 1 件でもあれば card 払い。
    """
    if bank in ("VPASS", "MUFGAmex", "Orico"):
        return "card"
    if bank == "MUFG":
        return "bank"
    matched = con.execute(
        "SELECT 1 FROM tx_links WHERE tx_a_id=? AND link_type='shop_card' LIMIT 1",
        (tx_id,),
    ).fetchone()
    if matched:
        return "card"
    if has_receipt or bank == "レシート":
        return "receipt"
    return "card"


def _tax_invoice_ids() -> dict[str, set[str]]:
    """銀行名 → 領収書IDセットのマップを返す。"""
    return {
        "Amazon":             _amazon_invoice_order_ids(),
        "Mercari":            _mercari_invoice_tx_ids(),
        "Mercari売上":        _mercari_invoice_tx_ids(),
        "Yahoo!ショッピング": _generic_invoice_ids(_YAHOO_SHOPPING_INVOICE_DIR),
        "楽天市場":           _generic_invoice_ids(_RAKUTEN_INVOICE_DIR),
        "CAMPFIRE":           _campfire_invoice_ids(),
    }


@app.post("/api/tax/sync")
async def api_tax_sync():
    """
    has_invoice=True かつカテゴリ設定済みの取引を tax_expense_items に自動追加し、
    会社名を invoice_vendors にも登録する。
    既存データから事業者番号マスターを一括作成する用途にも使用。
    """
    inv_map = _tax_invoice_ids()
    con = _tax_db()

    receipt_tx_ids: set[int] = {
        r[0] for r in con.execute(
            "SELECT DISTINCT transaction_id FROM receipt_links WHERE transaction_id IS NOT NULL"
        ).fetchall()
    }

    # 親行 [oid] は UI で隠れるため、ユーザーが直接カテゴリを編集できるのはサブ行のみ。
    # → 分割注文ではサブが Source of Truth。親はスキップしサブを計上する。
    # サブ行が無い注文（1商品のみ）は親をそのまま使う。
    rows = con.execute("""
        SELECT id, date, bank, description, debit, category
        FROM transactions t
        WHERE debit > 0
          AND category = '経費'
          AND NOT (
              -- 分割サブが存在する親行はスキップ
              bank IN ('Amazon', 'Yahoo!ショッピング')
              AND description LIKE '[%]%'
              AND description NOT LIKE '[%/%]%'
              AND EXISTS (
                  SELECT 1 FROM transactions s
                  WHERE s.bank = t.bank
                    AND s.description LIKE
                        ('[' || substr(t.description, 2, instr(t.description, ']') - 2) || '/%')
                    AND s.debit > 0
              )
          )
        ORDER BY date
    """).fetchall()

    vendor_added = 0
    item_added = 0
    item_updated = 0

    for r in rows:
        bank = r["bank"]
        desc = r["description"] or ""
        m = re.match(r'\[([^/\]]+)', desc)
        tid = m.group(1) if m else ""
        has_pdf_inv = bool(tid and bank in inv_map and tid in inv_map[bank])
        has_receipt  = r["id"] in receipt_tx_ids
        if not has_pdf_inv and not has_receipt:
            continue

        year = r["date"][:4]

        receipt_merchant = ''
        receipt_inv_num  = ''
        # has_receipt のうち、 紐付いた receipt の invoice_number が空 (= 海外法人で
        # 日本の適格事業者番号なし、 例: DeepL SE / Wonder Dynamics, Inc.) なら
        # tax_expense_items 登録スキップ。 仕入税額控除対象外なので /invoices に
        # 載せない (= ユーザ要望: DeepL は米国インボイス番号なので無視)
        if has_receipt and not (bank == 'レシート'):
            inv_check = con.execute(
                """SELECT MAX(CASE WHEN r2.invoice_number IS NOT NULL
                                  AND r2.invoice_number != '' THEN 1 ELSE 0 END) AS has_inv
                   FROM receipts r2
                   JOIN receipt_links rl ON rl.receipt_id = r2.id
                   WHERE rl.transaction_id = ?""",
                (r["id"],),
            ).fetchone()
            if not (inv_check and inv_check[0]):
                continue
        if bank == 'レシート' or has_receipt:
            rec_row = con.execute(
                """SELECT r2.merchant, r2.invoice_number
                   FROM receipts r2
                   JOIN receipt_links rl ON rl.receipt_id = r2.id
                   WHERE rl.transaction_id = ?
                   ORDER BY r2.id DESC LIMIT 1""",
                (r["id"],),
            ).fetchone()
            if rec_row:
                receipt_merchant = rec_row["merchant"] or ''
                receipt_inv_num  = rec_row["invoice_number"] or ''

        if receipt_merchant:
            svc_from_ocr, corp_from_ocr = _split_merchant(receipt_merchant)
        else:
            svc_from_ocr, corp_from_ocr = '', ''
        service_name = svc_from_ocr if svc_from_ocr else bank

        existing_vendor = None
        if receipt_inv_num:
            existing_vendor = con.execute(
                "SELECT id, service_name, company_name, invoice_number FROM invoice_vendors WHERE invoice_number=?",
                (receipt_inv_num,)
            ).fetchone()
            if existing_vendor:
                service_name = existing_vendor["service_name"]
                if corp_from_ocr and not existing_vendor["company_name"]:
                    con.execute(
                        "UPDATE invoice_vendors SET company_name=? WHERE id=?",
                        (corp_from_ocr, existing_vendor["id"])
                    )
        if not existing_vendor:
            existing_vendor = con.execute(
                "SELECT id, service_name, company_name, invoice_number FROM invoice_vendors WHERE service_name=?",
                (service_name,)
            ).fetchone()
        if not existing_vendor:
            con.execute(
                "INSERT INTO invoice_vendors (service_name, company_name, invoice_number) VALUES (?,?,?)",
                (service_name, corp_from_ocr, receipt_inv_num),
            )
            vendor_added += 1
            inv_num = receipt_inv_num
        else:
            inv_num = existing_vendor["invoice_number"] or receipt_inv_num

        method = _detect_payment_method(con, r["id"], bank, has_receipt)

        existing_item = con.execute(
            "SELECT id, amount, payment_method, date FROM tax_expense_items WHERE source_tx_id=?",
            (r["id"],),
        ).fetchone()
        if not existing_item:
            con.execute(
                """INSERT INTO tax_expense_items
                   (year, service_name, company_name, invoice_number, amount, payment_method,
                    date, description, source_tx_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (year, service_name, corp_from_ocr, inv_num,
                 r["debit"], method, r["date"], desc[:80], r["id"]),
            )
            item_added += 1
        else:
            # 既存アイテムを取引現状に合わせて同期。
            # - amount: scraper が後から送料込みに更新した場合（ヤフオク等）に追従
            # - date  : 同上
            # - payment_method: receipt → card への自動昇格
            updates = {}
            if existing_item["amount"] != r["debit"]:
                updates["amount"] = r["debit"]
            if existing_item["date"] != r["date"]:
                updates["date"] = r["date"]
            if existing_item["payment_method"] == "receipt" and method == "card":
                updates["payment_method"] = method
            if updates:
                set_clause = ", ".join(f"{k}=?" for k in updates) + ", updated_at=datetime('now','localtime')"
                con.execute(
                    f"UPDATE tax_expense_items SET {set_clause} WHERE id=?",
                    (*updates.values(), existing_item["id"]),
                )
                item_updated += 1

    con.commit()
    con.close()
    return {"vendors_added": vendor_added, "items_added": item_added, "items_updated": item_updated}


class TaxItemBody(BaseModel):
    year: str
    service_name: str = ""
    company_name: str = ""
    invoice_number: str = ""
    amount: int
    payment_method: str = "card"
    date: str = ""
    description: str = ""


class VendorBody(BaseModel):
    service_name: str = ""
    company_name: str = ""
    invoice_number: str = ""


@app.get("/api/tax/vendors")
async def api_tax_vendors():
    con = _tax_db()
    rows = con.execute(
        "SELECT * FROM invoice_vendors ORDER BY service_name, company_name"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


@app.post("/api/tax/vendors")
async def api_tax_vendor_upsert(body: VendorBody):
    # invoice_number を正規化 (= T 抜け 13 桁 → T 補完、 不正は 400)
    from src.server.routes_receipts import normalize_invoice_number
    normalized = normalize_invoice_number(body.invoice_number)
    if normalized is None:
        raise HTTPException(
            status_code=400,
            detail="invoice_number の形式が不正 (= T + 13 桁数字、 もしくは 13 桁数字)",
        )
    body.invoice_number = normalized
    # bank 名 (= 決済手段、 適格事業者ではない) を service_name にする登録は拒否。
    # 「レシート」 / VPASS / MUFG / MUFGAmex / Orico / メルカード 等。
    # SITE_BANKS (= Amazon / Mercari 等のショッピングサイト) はサイト自身が
    # 適格事業者となるため許容。
    from src.server.constants import CARD_BANKS as _CARD_BANKS
    _BANK_BLOCK = set(_CARD_BANKS) | {"レシート", "MUFG"}
    sn_strip = (body.service_name or "").strip()
    if sn_strip in _BANK_BLOCK:
        raise HTTPException(
            status_code=400,
            detail=f"service_name='{sn_strip}' は決済手段の bank 名で適格事業者ではないため登録できません",
        )
    con = _tax_db()
    if body.service_name:
        existing = con.execute(
            "SELECT id FROM invoice_vendors WHERE service_name=?", (body.service_name,)
        ).fetchone()
        if existing:
            con.execute(
                "UPDATE invoice_vendors SET company_name=?, invoice_number=CASE "
                "  WHEN ? != '' THEN ? ELSE invoice_number END WHERE id=?",
                (body.company_name, body.invoice_number, body.invoice_number, existing["id"]),
            )
            row_id = existing["id"]
        else:
            cur = con.execute(
                "INSERT INTO invoice_vendors (service_name, company_name, invoice_number) VALUES (?,?,?)",
                (body.service_name, body.company_name, body.invoice_number),
            )
            row_id = cur.lastrowid
    else:
        existing = con.execute(
            "SELECT id FROM invoice_vendors WHERE company_name=? AND service_name=''",
            (body.company_name,),
        ).fetchone()
        if existing:
            if body.invoice_number:
                con.execute(
                    "UPDATE invoice_vendors SET invoice_number=? WHERE id=?",
                    (body.invoice_number, existing["id"]),
                )
            row_id = existing["id"]
        else:
            cur = con.execute(
                "INSERT INTO invoice_vendors (service_name, company_name, invoice_number) VALUES ('',?,?)",
                (body.company_name, body.invoice_number),
            )
            row_id = cur.lastrowid
    con.commit()
    row = con.execute("SELECT * FROM invoice_vendors WHERE id=?", (row_id,)).fetchone()
    con.close()
    return dict(row)


@app.delete("/api/tax/vendors/{vendor_id}")
async def api_tax_vendor_delete(vendor_id: int):
    """事業者番号マスターを削除。 使われていない場合のみ。

    使用中判定: tax_expense_items / receipts / receipt_items のいずれかで
    この vendor の service_name / company_name / invoice_number が参照
    されていれば「使用中」 として 409 Conflict で拒否する。
    """
    con = _tax_db()
    v = con.execute(
        "SELECT * FROM invoice_vendors WHERE id=?", (vendor_id,)
    ).fetchone()
    if not v:
        con.close()
        raise HTTPException(status_code=404, detail="vendor not found")
    sn = (v["service_name"] or "").strip()
    cn = (v["company_name"] or "").strip()
    inv = (v["invoice_number"] or "").strip()
    # tax_expense_items / receipts / receipt_items 各々チェック
    used_in: list[str] = []
    if sn or cn or inv:
        sql = (
            "SELECT 1 FROM tax_expense_items WHERE "
            "(? != '' AND service_name = ?) OR "
            "(? != '' AND company_name = ?) OR "
            "(? != '' AND invoice_number = ?) LIMIT 1"
        )
        if con.execute(sql, (sn, sn, cn, cn, inv, inv)).fetchone():
            used_in.append("tax_expense_items")
    # receipts / receipt_items は /ui Pull の元データなので、 invoice_vendors の
    # 削除は集計に影響しない (= /tax UI 側の master 削除のみ)。 使用中判定対象外。
    if used_in:
        con.close()
        raise HTTPException(
            status_code=409,
            detail=f"vendor is used by: {', '.join(used_in)}",
        )
    con.execute("DELETE FROM invoice_vendors WHERE id=?", (vendor_id,))
    con.commit()
    con.close()
    return {"ok": True}


def _items_query(con, year: str = ""):
    """
    tax_expense_items に invoice_vendors を JOIN して表示用会社名を解決する。
    resolved_company: ベンダーマスターの company_name → item の company_name → service_name の優先順。
    receipt_ids: source_tx_id に紐付くレシートIDのカンマ区切り（なければ NULL）
    source_bank / source_description: 元取引の bank と description（PDF請求書リンク生成用）
    """
    # resolved_invoice_number / resolved_company の優先順:
    # 1. receipts (= 取引に紐付いた実物 receipt の invoice_number / merchant)
    #    → ストア別 T 番号 (= Amazon マーケットプレイス出品者ごと、 Yahoo!ショッピング ストアごと)
    # 2. invoice_vendors (= ベンダーマスター登録分、 Amazon ベンダー T3040001028447 等)
    # 3. tax_expense_items (= /api/tax/sync が書いた値)
    base = """
        SELECT i.*,
               COALESCE(
                 NULLIF((SELECT r.merchant FROM receipts r
                         JOIN receipt_links rl ON rl.receipt_id = r.id
                         WHERE rl.transaction_id = i.source_tx_id
                           AND r.invoice_number IS NOT NULL AND r.invoice_number != ''
                         LIMIT 1), ''),
                 NULLIF(v.company_name,''),
                 NULLIF(i.company_name,''),
                 i.service_name
               ) AS resolved_company,
               COALESCE(
                 NULLIF((SELECT r.invoice_number FROM receipts r
                         JOIN receipt_links rl ON rl.receipt_id = r.id
                         WHERE rl.transaction_id = i.source_tx_id
                           AND r.invoice_number IS NOT NULL AND r.invoice_number != ''
                         LIMIT 1), ''),
                 NULLIF(v.invoice_number,''),
                 i.invoice_number
               ) AS resolved_invoice_number,
               (SELECT GROUP_CONCAT(rl.receipt_id)
                FROM receipt_links rl
                WHERE rl.transaction_id = i.source_tx_id) AS receipt_ids,
               tx.bank AS source_bank,
               tx.description AS source_description
        FROM tax_expense_items i
        LEFT JOIN invoice_vendors v ON v.service_name = i.service_name AND i.service_name != ''
        LEFT JOIN transactions tx ON tx.id = i.source_tx_id
    """
    if year:
        return con.execute(base + " WHERE i.year=? ORDER BY i.date DESC, i.id DESC", (year,)).fetchall()
    return con.execute(base + " ORDER BY i.year DESC, i.date DESC, i.id DESC").fetchall()


@app.get("/api/tax/items")
async def api_tax_items(year: str = ""):
    """インボイス管理の表示データ。

    設計ポリシー: /tax は /ui (transactions + receipts + receipt_items) を Pull
    して自動でデータを構成する。 既存 tax_expense_items は手入力/旧データ用に
    残し、 source='manual' で区別。 /ui Pull 行は source='ui' で 1 receipt_item =
    1 行 (= 1 receipt が複数事業者のとき内訳ごとに別行)。

    両 source を 1 つのリストにマージして返す。 UI は source 列でグループ化 or
    フィルタする。
    """
    con = _tax_db()
    manual = [dict(r) for r in _items_query(con, year)]
    for m in manual:
        m["source"] = "manual"

    # /ui Pull: receipts.invoice_number または receipt_items.invoice_number を持つ
    # 経費 tx を行展開する。 1 receipt_item / 1 receipt = 1 行。 内訳がある receipt
    # は item ごと、 内訳がなく親 receipt のみなら親で 1 行。
    # payment_method を bank から判別 (= card / bank / receipt)
    _payment_method_case = (
        "CASE "
        "  WHEN t.bank IN ('VPASS', 'MUFGAmex', 'Orico', 'メルカード', 'AmazonPay') THEN 'card' "
        "  WHEN t.bank = 'レシート' THEN 'receipt' "
        "  ELSE 'bank' "
        "END"
    )
    sql_items = f"""
        SELECT
          'i:' || i.id AS row_key,
          t.id AS source_tx_id,
          t.bank AS source_bank,
          t.date,
          t.description AS source_description,
          t.description_normalized AS service_name,
          i.merchant AS company_name,
          i.invoice_number AS invoice_number,
          i.amount AS amount,
          i.label AS description,
          {_payment_method_case} AS payment_method
        FROM transactions t
        JOIN receipt_links rl ON rl.transaction_id = t.id
        JOIN receipts r ON r.id = rl.receipt_id
        JOIN receipt_items i ON i.receipt_id = r.id
        WHERE t.category IN ('経費', '今回は経費')
          -- 内訳行は invoice_number 必須 (= 日本の適格請求書のみ仕入税額控除対象)。
          -- merchant のみで invoice_number 無しは海外発行 / 未取得なので /invoices
          -- には載せない (= ユーザ要望: DeepL 等の米国 / 海外法人は無視)
          AND i.invoice_number IS NOT NULL AND i.invoice_number != ''
          -- レシート tx で別 bank 経費にも link 済なら除外 (二重カウント防止)
          AND NOT (
            t.bank = 'レシート' AND EXISTS (
              SELECT 1 FROM receipt_links rl2
              JOIN transactions t2 ON t2.id = rl2.transaction_id
              WHERE rl2.receipt_id = r.id AND t2.bank != 'レシート'
                AND t2.category IN ('経費', '今回は経費')
            )
          )
    """
    sql_receipts_only = f"""
        SELECT
          'r:' || r.id || ':' || t.id AS row_key,
          t.id AS source_tx_id,
          t.bank AS source_bank,
          t.date,
          t.description AS source_description,
          t.description_normalized AS service_name,
          r.merchant AS company_name,
          r.invoice_number AS invoice_number,
          t.debit AS amount,
          '' AS description,
          {_payment_method_case} AS payment_method
        FROM transactions t
        JOIN receipt_links rl ON rl.transaction_id = t.id
        JOIN receipts r ON r.id = rl.receipt_id
        WHERE t.category IN ('経費', '今回は経費')
          -- 親 receipt は invoice_number 必須 (= 日本の適格請求書のみ仕入税額控除対象)。
          -- merchant のみで invoice_number 無しは /invoices には載せない
          AND r.invoice_number IS NOT NULL AND r.invoice_number != ''
          AND NOT EXISTS (
            SELECT 1 FROM receipt_items i WHERE i.receipt_id = r.id
              AND ((i.invoice_number IS NOT NULL AND i.invoice_number != '')
                OR (i.merchant IS NOT NULL AND i.merchant != ''))
          )
          -- 同 tx に「items 持ちの別 receipt」 が link されているなら除外
          -- (= 内訳が sql_items 側で表示されるため、 親レベルの行は二重カウント)
          AND NOT EXISTS (
            SELECT 1 FROM receipt_links rl2
            JOIN receipts r2 ON r2.id = rl2.receipt_id
            JOIN receipt_items i2 ON i2.receipt_id = r2.id
            WHERE rl2.transaction_id = t.id
              AND ((i2.invoice_number IS NOT NULL AND i2.invoice_number != '')
                OR (i2.merchant IS NOT NULL AND i2.merchant != ''))
          )
          AND NOT (
            t.bank = 'レシート' AND EXISTS (
              SELECT 1 FROM receipt_links rl2
              JOIN transactions t2 ON t2.id = rl2.transaction_id
              WHERE rl2.receipt_id = r.id AND t2.bank != 'レシート'
                AND t2.category IN ('経費', '今回は経費')
            )
          )
    """
    params: list = []
    where_year = ""
    if year:
        where_year = " AND substr(t.date, 1, 4) = ?"
        params.append(year)
    pulled_items = con.execute(sql_items + where_year, params).fetchall()
    pulled_receipts = con.execute(sql_receipts_only + where_year, params).fetchall()
    pulled = []
    # source_tx_id ごとの receipt_ids cache (= filename あり = 実画像のみ表示用)
    _rid_cache: dict[int, str] = {}
    def _rids_for(tx_id: int) -> str:
        if tx_id in _rid_cache:
            return _rid_cache[tx_id]
        rows = con.execute(
            "SELECT GROUP_CONCAT(rl.receipt_id) FROM receipt_links rl "
            "JOIN receipts r ON r.id = rl.receipt_id "
            "WHERE rl.transaction_id = ? "
            "  AND r.filename IS NOT NULL AND r.filename != ''",
            (tx_id,),
        ).fetchone()
        rids = (rows[0] if rows else "") or ""
        _rid_cache[tx_id] = rids
        return rids
    for src in (pulled_items, pulled_receipts):
        for r in src:
            d = dict(r)
            d["source"] = "ui"
            d["id"] = d.pop("row_key")  # 既存 manual の id と区別 ('i:42' / 'r:7:112')
            d["resolved_invoice_number"] = d["invoice_number"]
            d["resolved_company"] = d["company_name"]
            d["receipt_ids"] = _rids_for(d["source_tx_id"]) if d.get("source_tx_id") else ""
            pulled.append(d)
    # 注: skip_tx_ids 計算 SQL を実行するため con は ここではまだ閉じない

    # 各 manual に stale_reason を付ける (= /ui Pull に置き換わる予定の旧データ識別用)。
    # クリーンアップ画面で視覚化 + 一括削除のために、 即除外せずタグだけ付ける。
    # ただし return 値から実際に除外するのは下のロジックで行う (skip_tx_ids ベース)。

    # 同じ source_tx_id を持つ manual は /ui Pull に置き換える (= /ui Pull 優先)。
    # 旧 manual は単一事業者の総額しか持たないため、 内訳を持つ /ui Pull を信頼する。
    # 例: 水道料金 ¥6,426 manual (= 上水のみで総額) は /ui Pull で
    # 「上水 ¥2,950 + 下水 ¥3,476」 (= 2 別事業者の正しい内訳) に置き換わる。
    # manual の source_tx_id がレシート tx (= 26854 等) で、 /ui Pull が同 receipt
    # の元経費 tx (= MUFG 等) を link しているケースもあるため、 receipt 経由で
    # 同一取引と判定される全 tx_id を skip 対象に拡張する。
    ui_source_tx_ids = {p.get("source_tx_id") for p in pulled if p.get("source_tx_id")}
    skip_tx_ids = set(ui_source_tx_ids)
    if ui_source_tx_ids:
        placeholders = ",".join("?" * len(ui_source_tx_ids))
        for r in con.execute(
            f"SELECT DISTINCT rl2.transaction_id FROM receipt_links rl1 "
            f"JOIN receipt_links rl2 ON rl1.receipt_id = rl2.receipt_id "
            f"WHERE rl1.transaction_id IN ({placeholders})",
            list(ui_source_tx_ids),
        ).fetchall():
            skip_tx_ids.add(r[0])
    manual = [m for m in manual if m.get("source_tx_id") not in skip_tx_ids]
    con.close()

    return manual + pulled


@app.post("/api/tax/items")
async def api_tax_item_create(body: TaxItemBody):
    con = _tax_db()
    cur = con.execute(
        """INSERT INTO tax_expense_items
           (year, service_name, company_name, invoice_number, amount, payment_method, date, description)
           VALUES (?,?,?,?,?,?,?,?)""",
        (body.year, body.service_name, body.company_name, body.invoice_number, body.amount,
         body.payment_method, body.date, body.description),
    )
    con.commit()
    rows = _items_query(con, "")
    row = next((r for r in rows if r["id"] == cur.lastrowid), None)
    con.close()
    return dict(row) if row else {}


@app.put("/api/tax/items/{item_id}")
async def api_tax_item_update(item_id: int, body: TaxItemBody):
    con = _tax_db()
    con.execute(
        """UPDATE tax_expense_items
           SET year=?, service_name=?, company_name=?, invoice_number=?, amount=?,
               payment_method=?, date=?, description=?, updated_at=datetime('now','localtime')
           WHERE id=?""",
        (body.year, body.service_name, body.company_name, body.invoice_number, body.amount,
         body.payment_method, body.date, body.description, item_id),
    )
    con.commit()
    rows = _items_query(con, "")
    row = next((r for r in rows if r["id"] == item_id), None)
    con.close()
    return dict(row) if row else {}


@app.delete("/api/tax/items/{item_id}")
async def api_tax_item_delete(item_id: int):
    con = _tax_db()
    con.execute("DELETE FROM tax_expense_items WHERE id=?", (item_id,))
    con.commit()
    con.close()
    return {"ok": True}


def _stale_manual_items(con, year: str = "") -> list[dict]:
    """旧 manual tax_expense_items のうち、 /ui Pull (= receipts/receipt_items) で
    同等以上の情報が得られるものを「stale (= 削除推奨)」 として返す。

    判定条件:
    - manual の source_tx_id が、 /ui Pull の source_tx_id 集合 (= receipt 経由で
      繋がる全 tx) のいずれかに該当する。 つまり receipts 側でその取引のインボイス
      情報が既に持たれている → manual は重複。
    """
    # /ui Pull 側で扱える tx_id 集合を計算 (= compute_invoice_summary と同条件)
    pulled_ids = set()
    rows = con.execute(
        """
        SELECT DISTINCT t.id FROM transactions t
        JOIN receipt_links rl ON rl.transaction_id = t.id
        JOIN receipts r ON r.id = rl.receipt_id
        WHERE t.category IN ('経費', '今回は経費')
          AND ((r.invoice_number IS NOT NULL AND r.invoice_number != '')
            OR (r.merchant IS NOT NULL AND r.merchant != ''))
        """
    ).fetchall()
    pulled_ids.update(r[0] for r in rows)
    # receipt 経由で繋がる他 tx も拡張
    if pulled_ids:
        placeholders = ",".join("?" * len(pulled_ids))
        for r in con.execute(
            f"SELECT DISTINCT rl2.transaction_id FROM receipt_links rl1 "
            f"JOIN receipt_links rl2 ON rl1.receipt_id = rl2.receipt_id "
            f"WHERE rl1.transaction_id IN ({placeholders})",
            list(pulled_ids),
        ).fetchall():
            pulled_ids.add(r[0])

    if not pulled_ids:
        return []
    rows = _items_query(con, year)
    stale = []
    for r in rows:
        if r["source_tx_id"] in pulled_ids:
            d = dict(r)
            d["stale_reason"] = "ui_pull_overlaps"
            stale.append(d)
    return stale


@app.get("/api/tax/items-stale")
async def api_tax_items_stale(year: str = ""):
    """旧 manual tax_expense_items のうち、 /ui Pull で同等以上の情報が得られる
    削除候補を返す。"""
    con = _tax_db()
    try:
        return _stale_manual_items(con, year)
    finally:
        con.close()


def _unused_invoice_vendors(con) -> list[dict]:
    """invoice_vendors のうち、 集計で使われていない vendor (= 不要 master) を返す。

    使用中判定: vendor の company_name または service_name が以下のいずれかに一致:
    - receipts.merchant
    - receipt_items.merchant
    - tax_expense_items.service_name / company_name

    invoice_number の一致は「使用中」 とみなさない。 /ui Pull 設計では集計が
    receipts.invoice_number / receipt_items.invoice_number で動くため、 vendor
    master に同じ T 番号があっても集計に貢献しない (= 古い名前の vendor entry が
    残っているケース、 例: invoice_vendors「草津市水道お客様センター / T78」 と
    receipts/receipt_items の正しい「草津市水道事業会計 / T78」 が並存)。
    """
    rows = con.execute("SELECT * FROM invoice_vendors").fetchall()
    out: list[dict] = []
    for v in rows:
        sn = (v["service_name"] or "").strip()
        cn = (v["company_name"] or "").strip()
        used = False
        if cn:
            if con.execute(
                "SELECT 1 FROM receipts WHERE merchant=? LIMIT 1", (cn,)
            ).fetchone():
                used = True
            if not used and con.execute(
                "SELECT 1 FROM receipt_items WHERE merchant=? LIMIT 1", (cn,)
            ).fetchone():
                used = True
            if not used and con.execute(
                "SELECT 1 FROM tax_expense_items WHERE company_name=? LIMIT 1", (cn,)
            ).fetchone():
                used = True
        if not used and sn:
            if con.execute(
                "SELECT 1 FROM tax_expense_items WHERE service_name=? LIMIT 1", (sn,)
            ).fetchone():
                used = True
        if not used:
            out.append(dict(v))
    return out


@app.get("/api/tax/vendors-unused")
async def api_tax_vendors_unused():
    """集計で使われていない invoice_vendors を返す。"""
    con = _tax_db()
    try:
        return _unused_invoice_vendors(con)
    finally:
        con.close()


@app.post("/api/tax/vendors/cleanup-unused")
async def api_tax_vendors_cleanup_unused():
    """未使用 vendor を一括削除。 ついでに stale manual (= /ui Pull で代替済の旧
    tax_expense_items) も削除して、 vendor 参照解放 → 削除を 1 操作で完結させる。

    戻り値: {"deleted_stale_items": N, "deleted_vendors": M}
    """
    con = _tax_db()
    try:
        # 1. 旧 manual エントリを先に削除 (= vendor 参照を解放)
        stale = _stale_manual_items(con, "")
        if stale:
            ids = [s["id"] for s in stale]
            placeholders = ",".join("?" * len(ids))
            con.execute(
                f"DELETE FROM tax_expense_items WHERE id IN ({placeholders})", ids,
            )
        # 2. 未使用 vendor を削除 (= 1 で参照が外れたものも含めて再判定)
        unused = _unused_invoice_vendors(con)
        if unused:
            ids2 = [v["id"] for v in unused]
            placeholders2 = ",".join("?" * len(ids2))
            con.execute(
                f"DELETE FROM invoice_vendors WHERE id IN ({placeholders2})", ids2,
            )
        con.commit()
        return {
            "deleted_stale_items": len(stale),
            "deleted_vendors": len(unused),
        }
    finally:
        con.close()


@app.post("/api/tax/items/cleanup-stale")
async def api_tax_items_cleanup_stale(year: str = ""):
    """stale な旧 manual tax_expense_items を一括削除。 戻り値: 削除件数。"""
    con = _tax_db()
    try:
        stale = _stale_manual_items(con, year)
        if not stale:
            return {"deleted": 0}
        ids = [s["id"] for s in stale]
        placeholders = ",".join("?" * len(ids))
        con.execute(
            f"DELETE FROM tax_expense_items WHERE id IN ({placeholders})", ids
        )
        con.commit()
        return {"deleted": len(ids)}
    finally:
        con.close()


@app.get("/api/tax/tx-suggestions")
async def api_tax_tx_suggestions(year: str):
    """指定年の取引一覧を返す（経費明細への追加候補として）。has_invoice を含む。"""
    inv_ids = _tax_invoice_ids()
    con = _tax_db()
    rows = con.execute("""
        SELECT id, date, bank, description, debit, category
        FROM transactions
        WHERE substr(date,1,4)=? AND debit>0
        ORDER BY date DESC
    """, (year,)).fetchall()
    con.close()
    result = []
    for r in rows:
        d = dict(r)
        bank = d["bank"]
        desc = d.get("description") or ""
        m = re.match(r'\[([^/\]]+)', desc)
        tid = m.group(1) if m else ""
        d["has_invoice"] = bool(tid and bank in inv_ids and tid in inv_ids[bank])
        result.append(d)
    return result


@app.get("/invoices", response_class=HTMLResponse)
async def invoices_page():
    return HTMLResponse(_build_tax_html())
