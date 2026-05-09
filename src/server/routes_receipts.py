"""レシート・領収書 PDF / 画像配信のルート群。"""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel
from fastapi.responses import FileResponse

from src.server import (
    app,
    _tx_db,
    _AMAZON_INVOICE_DIR,
    _MERCARI_INVOICE_DIR,
    _RAKUTEN_INVOICE_DIR,
    _CAMPFIRE_INVOICE_DIR,
    _ALIEXPRESS_INVOICE_DIR,
    _RECEIPTS_DIR,
)


class InvoiceMetaBody(BaseModel):
    merchant: str = ""
    invoice_number: str = ""
    receipt_date: str = ""
    amount: int | None = None
    # 同 (bank, description_normalized) を持つ他の経費 tx にも自動で紐付けるか。
    # 適格事業者登録は 1 vendor → 多 tx の 1:N データなので、 都度入力ではなく
    # 1 度登録すれば過去・未来の全該当取引に自動適用される設計。
    apply_to_all_same_desc: bool = True
    # description_normalized 違いだが同事業者の取引 (= 「ラクテンペイ*ジヨイフルホンダ」
    # と「ジョイフル本田 荒川沖店/iD」 のように bank 経由で表記が変わる) を含めて
    # 1:N 適用する場合のキーワード。 description_normalized LIKE '%keyword%' で
    # マッチする全経費 tx に link を張る (= bank 横断 / 表記揺れ対応)。
    apply_to_keyword: str = ""


def normalize_invoice_number(s: str) -> str | None:
    """適格請求書登録番号を「T + 13 桁数字」 形式に正規化する。

    入力許容:
    - 'T1234567890123' (正規)
    - 't1234567890123' (小文字 T)
    - '1234567890123' (T 省略 → 自動補完)
    - 'T-1234-5678-9012-3' (ハイフン区切り → 除去)
    - 全角文字含む (= NFKC 正規化)

    戻り値:
    - 正規化済み文字列 ("T1234567890123") = 正しい
    - "" = 入力が空 (= 後日追加用、 OK)
    - None = 不正な形式 (= 桁数違い / 数字以外混入 等)
    """
    if not s:
        return ""
    import unicodedata
    s = unicodedata.normalize("NFKC", s).strip()
    s = s.replace("-", "").replace(" ", "").upper()
    # 数字のみで 13 桁なら T を自動補完
    if s.isdigit() and len(s) == 13:
        return "T" + s
    # T + 13 桁数字
    if s.startswith("T") and len(s) == 14 and s[1:].isdigit():
        return s
    return None


@app.get("/api/receipts")
async def api_receipts():
    """全レシート一覧（紐付きtransaction情報含む）"""
    con = _tx_db()
    rows = con.execute(
        "SELECT id, filename, receipt_date, merchant, amount, saved_at FROM receipts ORDER BY saved_at DESC"
    ).fetchall()
    result = []
    for r in rows:
        rec = dict(r)
        linked = con.execute(
            """SELECT t.id, t.date, t.bank, t.description, t.debit
               FROM receipt_links rl
               JOIN transactions t ON t.id = rl.transaction_id
               WHERE rl.receipt_id = ?
               ORDER BY t.date DESC""",
            (r["id"],),
        ).fetchall()
        rec["linked_transactions"] = [dict(row) for row in linked]
        result.append(rec)
    con.close()
    return result


@app.get("/api/transactions/{tx_id}/receipts")
async def api_tx_receipts(tx_id: int):
    """指定transactionに紐付くレシート一覧 + 内訳 (receipt_items)"""
    con = _tx_db()
    rows = con.execute(
        """SELECT r.id, r.filename, r.receipt_date, r.merchant, r.invoice_number,
                  r.amount, r.saved_at
           FROM receipt_links rl
           JOIN receipts r ON r.id = rl.receipt_id
           WHERE rl.transaction_id = ?
           ORDER BY r.saved_at DESC""",
        (tx_id,),
    ).fetchall()
    out = []
    for r in rows:
        rec = dict(r)
        items = con.execute(
            """SELECT id, label, merchant, invoice_number, amount, sort_order
               FROM receipt_items WHERE receipt_id=? ORDER BY sort_order""",
            (r["id"],),
        ).fetchall()
        rec["items"] = [dict(it) for it in items]
        out.append(rec)
    con.close()
    return out


@app.post("/api/transactions/{tx_id}/invoice-meta")
async def api_attach_invoice_meta(tx_id: int, body: InvoiceMetaBody):
    """明細に「レシートの代わりのデータ (= 事業者名 + インボイス番号)」 を付ける。

    receipts に filename=NULL のメタのみエントリを作って receipt_links で紐付ける。
    インボイス管理 (/tax) は receipts.invoice_number を Pull するだけで自動反映する
    という疎結合設計の入口。 PDF を撮らない / 撮れない経費 (= サブスク決済 等) でも
    適格請求書情報を残せるようにする。

    apply_to_all_same_desc=True (default) なら、 同 (bank, description_normalized)
    を持つ他の経費 tx にも自動で receipt_links を張る (1:N 適用)。 適格事業者登録は
    1 vendor → 多 tx のデータなので、 1 度登録すれば過去 20 件 + 将来取得分にも
    効くべきという設計要件に従う。
    """
    con = _tx_db()
    t = con.execute(
        "SELECT id, date, bank, description_normalized FROM transactions WHERE id=?",
        (tx_id,),
    ).fetchone()
    if not t:
        con.close()
        raise HTTPException(status_code=404, detail="transaction not found")
    receipt_date = body.receipt_date.strip() or (t["date"] or "")
    merchant = body.merchant.strip()
    invoice_number = normalize_invoice_number(body.invoice_number)
    if invoice_number is None:
        con.close()
        raise HTTPException(
            status_code=400,
            detail="invoice_number の形式が不正 (= T + 13 桁数字、 もしくは 13 桁数字)",
        )
    # duplicate 防止: 同 source_tx_id 群 + 同 merchant の既存メタがあれば update。
    # 既存に invoice_number あり、 新入力が空 → 既存維持。 逆なら invoice_number を補強。
    existing_id: int | None = None
    if merchant:
        ex = con.execute(
            "SELECT r.id, r.invoice_number FROM receipts r "
            "JOIN receipt_links rl ON rl.receipt_id = r.id "
            "WHERE rl.transaction_id = ? "
            "  AND (r.filename IS NULL OR r.filename = '') "
            "  AND r.merchant = ? LIMIT 1",
            (tx_id, merchant),
        ).fetchone()
        if ex:
            existing_id = ex["id"]
            ex_inv = (ex["invoice_number"] or "").strip()
            new_inv = invoice_number or ex_inv
            con.execute(
                "UPDATE receipts SET invoice_number=?, receipt_date=COALESCE(NULLIF(?, ''), receipt_date) WHERE id=?",
                (new_inv, receipt_date, existing_id),
            )
    if existing_id is not None:
        receipt_id = existing_id
    else:
        cur = con.execute(
            "INSERT INTO receipts (filename, receipt_date, merchant, invoice_number, "
            " amount, raw_json) VALUES (NULL, ?, ?, ?, ?, ?)",
            (
                receipt_date,
                merchant,
                invoice_number,
                body.amount,
                '{"source":"manual_invoice_meta"}',
            ),
        )
        receipt_id = cur.lastrowid
    # 1:N 適用: 同 (bank, description_normalized) の全経費 tx に link
    target_ids: list[int] = [tx_id]
    if body.apply_to_all_same_desc and t["description_normalized"]:
        same = con.execute(
            "SELECT id FROM transactions "
            "WHERE bank=? AND description_normalized=? "
            "  AND category IN ('経費', '今回は経費') AND id != ?",
            (t["bank"], t["description_normalized"], tx_id),
        ).fetchall()
        target_ids.extend(r[0] for r in same)
    # キーワード適用 (= bank 横断 / 表記揺れ対応): description_normalized に
    # 指定キーワードを含む全経費 tx に link を張る。
    kw = (body.apply_to_keyword or "").strip()
    if kw:
        kw_rows = con.execute(
            "SELECT id FROM transactions "
            "WHERE description_normalized LIKE ? "
            "  AND category IN ('経費', '今回は経費')",
            (f"%{kw}%",),
        ).fetchall()
        for r in kw_rows:
            if r[0] not in target_ids:
                target_ids.append(r[0])
    for tid in target_ids:
        con.execute(
            "INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id) VALUES (?, ?)",
            (receipt_id, tid),
        )
    con.commit()
    con.close()
    # cache invalidate (= no_receipt タグが消えるので)
    from src.server import _invalidate_tx_cache
    _invalidate_tx_cache()
    return {"status": "ok", "receipt_id": receipt_id, "linked_count": len(target_ids)}


@app.post("/api/receipts/{receipt_id}/link/{tx_id}")
async def api_link_receipt(receipt_id: int, tx_id: int):
    """レシートとtransactionをリンク"""
    con = _tx_db()
    r = con.execute("SELECT id FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if not r:
        con.close()
        raise HTTPException(status_code=404, detail="receipt not found")
    t = con.execute("SELECT id FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not t:
        con.close()
        raise HTTPException(status_code=404, detail="transaction not found")
    con.execute(
        "INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id) VALUES (?, ?)",
        (receipt_id, tx_id),
    )
    con.commit()
    con.close()
    return {"status": "ok"}


@app.delete("/api/receipts/{receipt_id}/link/{tx_id}")
async def api_unlink_receipt(receipt_id: int, tx_id: int):
    """リンク解除"""
    con = _tx_db()
    con.execute(
        "DELETE FROM receipt_links WHERE receipt_id=? AND transaction_id=?",
        (receipt_id, tx_id),
    )
    con.commit()
    con.close()
    return {"status": "ok"}


@app.get("/api/mercari-invoice/{tx_id}")
async def api_mercari_invoice(tx_id: str):
    """Mercari 取引 PDF をダウンロード用に返す。"""
    if not _MERCARI_INVOICE_DIR.exists():
        raise HTTPException(status_code=404, detail="invoice dir not found")
    for pdf in _MERCARI_INVOICE_DIR.rglob(f"*_{tx_id}.pdf"):
        return FileResponse(str(pdf), media_type="application/pdf",
                            headers={"Content-Disposition": "inline"})
    raise HTTPException(status_code=404, detail="invoice not found")


@app.get("/api/rakuten-invoice/{order_num}")
async def api_rakuten_invoice(order_num: str):
    for pdf in _RAKUTEN_INVOICE_DIR.rglob(f"*_{order_num}.pdf"):
        return FileResponse(str(pdf), media_type="application/pdf",
                            headers={"Content-Disposition": "inline"})
    raise HTTPException(status_code=404, detail="invoice not found")


@app.get("/api/aliexpress-invoice/{order_id}")
async def api_aliexpress_invoice(order_id: str):
    for pdf in _ALIEXPRESS_INVOICE_DIR.rglob(f"*_{order_id}.pdf"):
        return FileResponse(str(pdf), media_type="application/pdf",
                            headers={"Content-Disposition": "inline"})
    raise HTTPException(status_code=404, detail="invoice not found")


@app.get("/api/campfire-invoice/{backer_id}")
async def api_campfire_invoice(backer_id: str, kind: str = "detail"):
    """kind=detail → 支援詳細ページPDF（商品代の証憑）/ kind=fee → システム利用料公式領収書"""
    suffix = "_detail.pdf" if kind == "detail" else "_fee.pdf"
    for pdf in _CAMPFIRE_INVOICE_DIR.rglob(f"*_{backer_id}{suffix}"):
        return FileResponse(str(pdf), media_type="application/pdf",
                            headers={"Content-Disposition": "inline"})
    for pdf in _CAMPFIRE_INVOICE_DIR.rglob(f"*_{backer_id}.pdf"):
        return FileResponse(str(pdf), media_type="application/pdf",
                            headers={"Content-Disposition": "inline"})
    raise HTTPException(status_code=404, detail="invoice not found")




@app.get("/api/amazon-invoice/{order_id}")
async def api_amazon_invoice(order_id: str):
    if not _AMAZON_INVOICE_DIR.exists():
        raise HTTPException(status_code=404, detail="invoice dir not found")
    for pdf in _AMAZON_INVOICE_DIR.rglob(f"*_{order_id}.pdf"):
        return FileResponse(str(pdf), media_type="application/pdf",
                            headers={"Content-Disposition": "inline"})
    raise HTTPException(status_code=404, detail="invoice not found")


@app.get("/api/receipts/{receipt_id}/image")
async def api_receipt_image(receipt_id: int):
    """レシート画像ファイルを返す"""
    con = _tx_db()
    row = con.execute("SELECT filename FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(status_code=404, detail="receipt not found")
    path = _RECEIPTS_DIR / row["filename"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="image file not found")
    return FileResponse(str(path))
