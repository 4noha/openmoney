"""Amazon 領収書 PDF ダウンロード + 販売者発行の適格請求書取得。"""
from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page

from . import _INVOICE_DIR, _db_connect
from .auth import _needs_login


def _invoice_path(tx: dict) -> Path:
    order_id = ""
    desc = tx.get("description", "")
    if desc.startswith("["):
        end = desc.find("]")
        if end > 0:
            order_id = desc[1:end]
    date_str = tx["date"].replace("/", "-")
    year = date_str[:4]
    filename = f"{date_str}_{order_id or 'unknown'}.pdf"
    return _INVOICE_DIR / year / filename


def _pending_invoice_orders() -> list[tuple[str, str, bool, bool]]:
    """Amazon 注文 (order_id, date_str, need_summary_pdf, need_qualifying_pdf) を返す。

    - need_summary_pdf : <date>_<oid>.pdf 未保存
    - need_qualifying_pdf : <date>_<oid>_invoice*.pdf が 1 件もない (= 適格請求書未取得)

    どちらかが True の注文を返す (= 両方完了済の注文は skip)。
    """
    con = _db_connect()
    rows = con.execute(
        "SELECT description, date FROM transactions "
        "WHERE bank='Amazon' AND description LIKE '[%]%' AND description NOT LIKE '[%/%]%' "
        "ORDER BY date DESC"
    ).fetchall()
    con.close()
    pending: list[tuple[str, str, bool, bool]] = []
    for r in rows:
        desc = r["description"]
        end = desc.find("]")
        if end <= 1:
            continue
        order_id = desc[1:end]
        if "/" in order_id:
            continue
        date_str = r["date"].replace("/", "-")
        year_dir = _INVOICE_DIR / date_str[:4]
        summary_pdf = year_dir / f"{date_str}_{order_id}.pdf"
        invoice_pdfs = list(year_dir.glob(f"{date_str}_{order_id}_invoice*.pdf"))
        need_s = not summary_pdf.exists()
        need_q = len(invoice_pdfs) == 0
        if need_s or need_q:
            pending.append((order_id, date_str, need_s, need_q))
    return pending


async def download_invoices(page: Page, transactions: list[dict]) -> int:
    """未ダウンロードの領収書PDFを保存する。DB 全件を対象にする。保存件数を返す。

    各注文に対して:
    1. 既存 print.html (= Amazon 購入明細) PDF 保存 → <date>_<oid>.pdf
    2. 注文詳細ページから「販売者の発行する適格請求書」 リンクを抽出 → 各
       リンクを別 PDF で保存 → <date>_<oid>_invoice<seq>.pdf
    3. 適格請求書 PDF から T 番号 + 発行事業者名を抽出 → receipts INSERT
    """
    pending = _pending_invoice_orders()
    n_summary = sum(1 for _, _, ns, _ in pending if ns)
    n_qualifying = sum(1 for _, _, _, nq in pending if nq)
    print(f"[Amazon] 処理対象: {len(pending)} 件 (購入明細 {n_summary} / 適格請求書 {n_qualifying})")
    saved = 0
    for order_id, date_str, need_summary, need_qualifying in pending:
        year = date_str[:4]
        pdf_path = _INVOICE_DIR / year / f"{date_str}_{order_id}.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)

        # ①既存の Amazon 購入明細 PDF (= 後方互換)。 既に保存済なら skip
        if need_summary:
            urls = [
                f"https://www.amazon.co.jp/gp/css/summary/print.html?ie=UTF8&orderID={order_id}",
                f"https://www.amazon.co.jp/gp/your-account/order-details?orderID={order_id}",
            ]
            ok = False
            for url in urls:
                try:
                    await page.goto(url, wait_until="load", timeout=30_000)
                    await asyncio.sleep(0.5)
                    if _needs_login(page.url):
                        print(f"  [Amazon] セッション切れ — PDF ダウンロード中断")
                        return saved
                    body = await page.evaluate("() => document.body.innerText")
                    if len(body.strip()) < 100:
                        continue
                    await page.pdf(path=str(pdf_path), format="A4", print_background=True)
                    if pdf_path.stat().st_size < 5000:
                        pdf_path.unlink(missing_ok=True)
                        continue
                    saved += 1
                    ok = True
                    break
                except Exception as e:
                    print(f"  [{order_id}] {url[-30:]} 失敗: {e}")
            if not ok:
                print(f"  [{order_id}] 購入明細 取得不可")

        # ②販売者発行の適格請求書 (= ストアごとの T 番号入り)
        if need_qualifying:
            try:
                await _download_qualifying_invoices(page, order_id, date_str)
            except Exception as e:
                print(f"  [{order_id}] 適格請求書取得失敗: {e}")

        await asyncio.sleep(0.8)

    return saved


async def _download_qualifying_invoices(page: Page, order_id: str, date_str: str) -> None:
    """注文詳細ページの「販売者の発行する適格請求書」 全リンクを保存 + receipt 作成。

    1 注文に複数商品 (= 別ストア出品) がある場合、 リンクが複数あるので seq 番号
    付きで保存。 PDF パースで T 番号取れた分だけ /invoices に出る。
    """
    detail_url = f"https://www.amazon.co.jp/gp/your-account/order-details?orderID={order_id}"
    await page.goto(detail_url, wait_until="load", timeout=30_000)
    await asyncio.sleep(0.5)
    if _needs_login(page.url):
        return

    # 「インボイス」 / 「適格請求書」 を含むリンクを抽出
    hrefs = await page.evaluate("""
        () => Array.from(document.querySelectorAll('a')).filter(a => {
            const t = (a.textContent || '').trim();
            return t.includes('インボイス') || t.includes('適格請求書')
                || t.includes('販売者') && t.includes('請求書');
        }).map(a => a.href).filter(h => h && h.startsWith('http'))
    """)
    # 重複排除しつつ順序維持
    seen: set[str] = set()
    hrefs = [h for h in hrefs if not (h in seen or seen.add(h))]
    if not hrefs:
        return

    year = date_str[:4]
    year_dir = _INVOICE_DIR / year
    for seq, href in enumerate(hrefs, 1):
        invoice_pdf = year_dir / f"{date_str}_{order_id}_invoice{seq}.pdf"
        if invoice_pdf.exists():
            continue
        try:
            await page.goto(href, wait_until="load", timeout=30_000)
            await asyncio.sleep(0.5)
            if _needs_login(page.url):
                return
            body = await page.evaluate("() => document.body.innerText")
            if len(body.strip()) < 80:
                continue
            await page.pdf(path=str(invoice_pdf), format="A4", print_background=True)
            if invoice_pdf.stat().st_size < 5000:
                invoice_pdf.unlink(missing_ok=True)
                continue
            _save_invoice_receipt(order_id, date_str, invoice_pdf)
        except Exception as e:
            print(f"  [{order_id}] invoice{seq} 失敗: {e}")


def _parse_invoice_pdf(pdf_path: Path) -> tuple[str | None, str | None]:
    """適格請求書 PDF からストアの T 番号 + 発行事業者名を抽出。"""
    try:
        import pdfplumber
        text_parts: list[str] = []
        with pdfplumber.open(pdf_path) as pdf:
            for p in pdf.pages:
                text_parts.append(p.extract_text() or "")
        text = "\n".join(text_parts)
    except Exception:
        return None, None

    m = re.search(r"T\d{13}", text)
    if not m:
        return None, None
    invoice_no = m.group(0)
    merchant = None
    for pat in (
        r"([^\s\n]+(?:株式会社|有限会社|合同会社|合資会社|合名会社))\s*[\n\s]*登録番号\s*T\d{13}",
        r"事業者名[\s:：]*([^\n]+?)\s*T\d{13}",
        r"発行者[\s:：]*([^\n]+?)\s*T\d{13}",
        r"販売者[\s:：]*([^\n]+?)\s*[\n\s]+.*?T\d{13}",
        r"([^、,。\n]{3,40}?)\s*\(T\d{13}\)",
    ):
        mm = re.search(pat, text)
        if mm:
            merchant = mm.group(1).strip()
            break
    return invoice_no, merchant


def _save_invoice_receipt(order_id: str, order_date: str, pdf_path: Path) -> None:
    """PDF をパースして receipts に保存 + 既存 Amazon 注文 tx に link。"""
    invoice_no, merchant = _parse_invoice_pdf(pdf_path)
    if not invoice_no:
        return  # T 番号なしは receipt 化しない (= 適格請求書 URL から取れたが Amazon 不発行)
    if not merchant:
        merchant = f"Amazon 出品者 ({order_id})"
    rel_filename = pdf_path.relative_to(_INVOICE_DIR.parent).as_posix()
    con = _db_connect()
    # Amazon の 親注文 [oid] + 分割サブ行 [oid/N] 全てに link
    tx_rows = con.execute(
        "SELECT id FROM transactions "
        "WHERE bank='Amazon' AND description LIKE ?",
        (f"[{order_id}]%",),
    ).fetchall()
    if not tx_rows:
        con.close()
        return
    parent_id = tx_rows[0][0]

    existing = con.execute(
        "SELECT id FROM receipts WHERE filename=?", (rel_filename,),
    ).fetchone()
    if existing:
        rid = existing[0]
        con.execute(
            "UPDATE receipts SET invoice_number=?, merchant=? WHERE id=?",
            (invoice_no, merchant, rid),
        )
    else:
        # 金額は親 transaction の debit を使う (= サブ行は internal split)
        amt_row = con.execute(
            "SELECT debit FROM transactions WHERE id=?", (parent_id,),
        ).fetchone()
        amt = amt_row[0] if amt_row else 0
        cur = con.execute(
            "INSERT INTO receipts "
            "(filename, receipt_date, merchant, invoice_number, amount, transaction_id, saved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rel_filename, order_date.replace("-", "/"), merchant, invoice_no, amt,
             parent_id, datetime.now().isoformat()),
        )
        rid = cur.lastrowid
    for row in tx_rows:
        con.execute(
            "INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id) VALUES (?, ?)",
            (rid, row[0]),
        )
    con.commit()
    con.close()
    print(f"  [{order_id}] 適格請求書: {merchant} {invoice_no}")
