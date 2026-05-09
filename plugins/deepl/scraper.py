"""DeepL SE 領収書メールを Gmail 経由で取得する plugin。

DeepL Pro / Subscription の領収書が no-reply@deepl.com から届く (= subject に
「領収書」 を含む)。 PDF 添付があれば保存し、 本文 (HTML / text) から金額・日付を
抽出。 通貨は JPY 想定 (= 日本ユーザは円請求) だが LLM フォールバックで EUR / USD
にも対応。

env:
  GMAIL_USER          : Gmail アドレス (= gmail plugin と共通)
  GMAIL_APP_PASSWORD  : Gmail アプリパスワード (= 同上)
  ANTHROPIC_API_KEY   : LLM フォールバック用 (任意)
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import os
import re
import sqlite3
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "transactions.db"
RECEIPTS_DIR = Path(__file__).parent.parent.parent / "invoices" / "receipts"

_FROM = "no-reply@deepl.com"
_SUBJECT_KW = "領収書"  # 日本語 = IMAP search では使えないので Python 側で filter
_BANK = "DeepL"
_MERCHANT = "DeepL SE"


def _decode_h(s: str) -> str:
    if not s:
        return ""
    parts = decode_header(s)
    out: list[str] = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(enc or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _extract_text_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset, errors="replace")
                except (LookupError, TypeError):
                    return payload.decode("utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    text = payload.decode(charset, errors="replace")
                except (LookupError, TypeError):
                    text = payload.decode("utf-8", errors="replace")
                return re.sub(r"<[^>]+>", " ", text)
    else:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except (LookupError, TypeError):
            return payload.decode("utf-8", errors="replace")
    return ""


def _extract_pdf_attachments(msg: email.message.Message) -> list[bytes]:
    attachments: list[bytes] = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        ctype = part.get_content_type()
        cdisp = (part.get("Content-Disposition") or "").lower()
        filename = (part.get_filename() or "").lower()
        if ctype == "application/pdf" or filename.endswith(".pdf") or "attachment" in cdisp:
            payload = part.get_payload(decode=True)
            if payload and payload.startswith(b"%PDF"):
                attachments.append(payload)
    return attachments


def _parse_receipt(subject: str, body: str) -> dict | None:
    """DeepL レシートメールから date / amount / receipt_no / currency / invoice_number を抽出。

    LLM 主導 (= フォーマットが多言語 / 通貨 / 表記ゆれが多いため)。 ANTHROPIC_API_KEY が
    なければ regex で最低限を試行 (= フォールバック)。
    """
    from src.llm_extract import extract_json
    parsed = extract_json(
        "DeepL の領収書メールから JSON で抽出:\n"
        '{"date": "YYYY/MM/DD (= 領収日 / Date)", '
        '"amount": 整数 (= 円なら円、 USD/EUR ならセント単位、 例: ¥3,000 → 3000、 $20.50 → 2050), '
        '"currency": "JPY"|"USD"|"EUR" (= 通貨)、 '
        '"receipt_no": "領収書番号 / Invoice number" (任意、 無ければ null), '
        '"invoice_number": "T + 13 桁 (適格事業者番号、 日本国外サービスは通常 null)"}\n\n'
        f"件名: {subject}\n\n本文:\n{body[:3000]}\n\nJSON のみ返してください。"
    )
    if parsed and parsed.get("date") and parsed.get("amount"):
        return parsed

    # regex フォールバック (= LLM 不可時)
    # 「合計: 3,000 円」 「Total: ¥3,000」 「Total: $20.00」 等
    amount = None
    currency = "JPY"
    for pat, cur in (
        (r"合計[^¥\d]*¥?\s*([\d,]+)\s*円", "JPY"),
        (r"Total[^¥\d]*¥\s*([\d,]+)", "JPY"),
        (r"\$\s*([\d,]+\.\d{2})", "USD"),
        (r"€\s*([\d,]+\.\d{2})", "EUR"),
    ):
        m = re.search(pat, body)
        if m:
            try:
                v = float(m.group(1).replace(",", ""))
                amount = int(round(v * 100)) if cur != "JPY" else int(v)
                currency = cur
                break
            except ValueError:
                pass
    # 日付: YYYY-MM-DD or YYYY/MM/DD or 日本語年月日
    date_str = None
    m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?", body)
    if m:
        date_str = f"{int(m.group(1)):04d}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"
    if amount and date_str:
        return {"date": date_str, "amount": amount, "currency": currency,
                "receipt_no": None, "invoice_number": None}
    return None


def _save_to_db(rows: list[dict]) -> int:
    if not rows:
        return 0
    from src.db import connect as _connect_central
    from src.server.categorize import normalize_desc
    con = _connect_central(DB_PATH)
    inserted = 0
    receipt_added = 0
    for r in rows:
        currency = r.get("currency", "JPY")
        receipt_no = r.get("receipt_no")
        if currency == "JPY":
            desc = f"DeepL Pro {r['date'][:7].replace('/', '/')}" + (f" #{receipt_no}" if receipt_no else "")
        else:
            desc = f"DeepL Pro ({currency}) {r['date'][:7]}" + (f" #{receipt_no}" if receipt_no else "")
        try:
            cur = con.execute(
                "INSERT OR IGNORE INTO transactions "
                "(bank, date, description, description_normalized, debit, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _BANK, r["date"], desc, normalize_desc(desc),
                    int(r["amount"]), r["fetched_at"],
                ),
            )
            inserted += cur.rowcount
            tx_row = con.execute(
                "SELECT id FROM transactions WHERE bank=? AND date=? AND description=?",
                (_BANK, r["date"], desc),
            ).fetchone()
            if not tx_row:
                continue
            tx_id = tx_row[0]

            pdf_filename = r.get("pdf_filename")
            inv = r.get("invoice_number") or None
            existing = con.execute(
                "SELECT id, filename FROM receipts WHERE merchant=? AND transaction_id=?",
                (_MERCHANT, tx_id),
            ).fetchone()
            if existing:
                rid, existing_fn = existing
                if pdf_filename and not existing_fn:
                    con.execute("UPDATE receipts SET filename=? WHERE id=?", (pdf_filename, rid))
                    receipt_added += 1
                if inv:
                    con.execute("UPDATE receipts SET invoice_number=? WHERE id=? AND (invoice_number IS NULL OR invoice_number='')",
                                (inv, rid))
                con.execute(
                    "INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id) VALUES (?, ?)",
                    (rid, tx_id),
                )
                continue
            cur = con.execute(
                "INSERT INTO receipts "
                "(filename, receipt_date, merchant, invoice_number, amount, transaction_id, saved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pdf_filename, r["date"], _MERCHANT, inv, int(r["amount"]), tx_id, r["fetched_at"]),
            )
            rid = cur.lastrowid
            con.execute(
                "INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id) VALUES (?, ?)",
                (rid, tx_id),
            )
            receipt_added += 1
        except (ValueError, sqlite3.Error) as e:
            print(f"  DB保存スキップ: {r} ({e})")
    con.commit()
    con.close()
    if receipt_added:
        print(f"  receipt: +{receipt_added} 件")
    return inserted


async def run(*, days: int = 365, dry_run: bool = False) -> list[dict]:
    user = (os.environ.get("GMAIL_USER") or "").strip()
    pw = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    if not user or not pw:
        print("[deepl] GMAIL_USER / GMAIL_APP_PASSWORD 未設定 (= gmail plugin で設定)")
        return []

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    rows: list[dict] = []

    def _fetch_all() -> list[dict]:
        result: list[dict] = []
        with imaplib.IMAP4_SSL("imap.gmail.com") as M:
            M.login(user, pw)
            M.select("INBOX")
            # FROM だけ IMAP search、 subject「領収書」 は日本語なので Python 側で filter
            typ, data = M.search(None, "SINCE", cutoff, "FROM", f'"{_FROM}"')
            if typ != "OK":
                return result
            msg_ids = data[0].split() if data and data[0] else []
            print(f"[deepl] {_FROM}: {len(msg_ids)} 件 (subject filter 前)")
            for mid in msg_ids:
                typ, msg_data = M.fetch(mid, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                subject = _decode_h(msg.get("Subject") or "")
                # 「領収書」 (= 日本語) または英語の "Receipt" を含むものだけを対象に
                if _SUBJECT_KW not in subject and "Receipt" not in subject and "receipt" not in subject:
                    continue
                body = _extract_text_body(msg)
                parsed = _parse_receipt(subject, body)
                if not parsed:
                    print(f"  パース失敗: {subject!r}")
                    continue

                pdf_filename = None
                pdfs = _extract_pdf_attachments(msg)
                if pdfs:
                    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
                    ym = (parsed.get("date") or "")[:7].replace("/", "-")
                    rno = parsed.get("receipt_no") or ym
                    safe = re.sub(r"[^\w\-]", "_", str(rno))[:30]
                    pdf_filename = f"deepl_{safe}.pdf"
                    pdf_path = RECEIPTS_DIR / pdf_filename
                    if not pdf_path.exists():
                        pdf_path.write_bytes(pdfs[0])

                result.append({
                    **parsed,
                    "pdf_filename": pdf_filename,
                    "fetched_at": datetime.now().isoformat(),
                })
        return result

    rows = await asyncio.get_event_loop().run_in_executor(None, _fetch_all)

    if dry_run:
        print(f"[deepl] dry_run: {len(rows)} 件")
        for r in rows:
            print(f"  {r}")
        return rows

    n = _save_to_db(rows)
    # 個別 trigger でも shop_card link を再生成 (= idempotent)
    shop_link = 0
    try:
        from src.matching import run_shop_matching
        matches = run_shop_matching()
        shop_link = sum(1 for m in matches if m.shop_bank == "DeepL")
    except Exception as e:
        print(f"  shop_matching 失敗: {e}")
    # VPASS の "DEEPL*" 行に DeepL plugin の receipt を流用 link
    receipt_link_added = _link_vpass_deepl()
    try:
        from src.server import _invalidate_tx_cache
        _invalidate_tx_cache()
    except Exception:
        pass
    print(
        f"[deepl] {len(rows)} 件取得 / DB +{n} 件追加 / "
        f"shop_card link {shop_link} 件 / VPASS receipt link +{receipt_link_added} 件"
    )
    return rows


def _link_vpass_deepl() -> int:
    """VPASS の "DEEPL*" 行のうち、 同金額の DeepL plugin tx と日付近接 (≤ 5日)
    で一致する行に DeepL plugin の receipt を流用 link。
    """
    from src.db import connect as _connect_central
    con = _connect_central(DB_PATH)
    before = con.execute("SELECT COUNT(*) FROM receipt_links").fetchone()[0]
    con.execute("""
        WITH match AS (
            SELECT v.id AS v_tx_id, r.id AS receipt_id
            FROM transactions v
            JOIN transactions d
              ON d.bank = 'DeepL'
             AND d.debit = v.debit
             AND ABS(julianday(replace(v.date,'/','-')) - julianday(replace(d.date,'/','-'))) <= 5
            JOIN receipts r
              ON r.transaction_id = d.id
             AND r.merchant = 'DeepL SE'
            WHERE v.bank = 'VPASS'
              AND v.description LIKE '%DEEPL%'
        )
        INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id)
        SELECT receipt_id, v_tx_id FROM match
    """)
    after = con.execute("SELECT COUNT(*) FROM receipt_links").fetchone()[0]
    con.commit()
    con.close()
    n = after - before
    if n > 0:
        print(f"  VPASS DEEPL に receipt link: +{n} 件")
    return n


if __name__ == "__main__":
    asyncio.run(run(dry_run=True))
