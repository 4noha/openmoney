"""Wonder Studio (= Wonder Dynamics) の領収書メールを Gmail 経由で取得する plugin。

Wonder Dynamics は Autodesk に買収されたため、 メール送信元 / フォーマットが
時期によって 2 系統ある:

1. 旧: Stripe 経由 (= acct_1LxarbINaKzhufDf@stripe.com)
   subject: "Your receipt from Wonder Dynamics #2000-XXXX"
   通貨: USD、 PDF 添付あり

2. 新: オートデスク株式会社 (= ar.estore.noreply@autodesk.com)
   subject: 「【オートデスク株式会社】請求書/納品書/領収書」
   通貨: JPY 想定 (= 日本法人請求)、 PDF 添付想定 + 適格事業者番号あり想定

両方とも bank='Wonder Studio' に統合して保存する (= 同サービスの継続として扱う)。
パースは LLM 主導 (= 通貨 / フォーマット差を吸収)。

env:
  GMAIL_USER          : Gmail アドレス (= gmail plugin と共通)
  GMAIL_APP_PASSWORD  : Gmail アプリパスワード (= 同上)
  ANTHROPIC_API_KEY   : LLM フォールバック用 (任意、 regex 失敗時のみ)
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import io
import os
import re
import sqlite3
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "transactions.db"
RECEIPTS_DIR = Path(__file__).parent.parent.parent / "invoices" / "receipts"

_BANK = "Wonder Studio"

# 取得対象メールの送信元 + 件名キーワード。 旧 Stripe + 新 Autodesk の両対応。
# subject_kw が ASCII なら IMAP search に直接渡せる、 日本語は Python 側で filter。
_SOURCES = [
    {
        "from": "invoice+statements+acct_1LxarbINaKzhufDf@stripe.com",
        "subject_kw": "Wonder Dynamics",
        "merchant_default": "Wonder Dynamics, Inc.",
        "currency_default": "USD",
    },
    {
        "from": "ar.estore.noreply@autodesk.com",
        "subject_kw": "領収書",  # 「【オートデスク株式会社】請求書/納品書/領収書」
        "merchant_default": "オートデスク株式会社",
        "currency_default": "JPY",
    },
]


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
    """text/plain or text/html を抜き出す。 HTML はタグ除去。"""
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


def _read_pdf_text(pdf_bytes: bytes) -> str:
    """pdfplumber でパスワード無し PDF を開いてテキスト抽出。 失敗なら空文字。"""
    try:
        import pdfplumber
        import io
        text_parts: list[str] = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                text_parts.append(t)
        return "\n".join(text_parts)
    except Exception as e:
        print(f"  PDF テキスト抽出失敗: {e}")
        return ""


def _parse_receipt(subject: str, body: str, source: dict, pdf_text: str = "") -> dict | None:
    """件名 + 本文 (+ PDF テキスト) から date / amount / currency / receipt_no /
    invoice_number / merchant を抽出。

    旧 Stripe (USD): regex で抽出可能 (= "Amount paid $50.00" / "Date paid Jun 5, 2025"
    / 件名 #2000-XXXX)。
    新 Autodesk (JPY): メール本文が text/html のみで情報少ないので PDF テキストを
    LLM に渡してパース (= 適格事業者番号も PDF 内記載想定)。
    """
    currency_default = source.get("currency_default", "USD")

    if currency_default == "USD":
        # 旧 Stripe フォーマット regex 試行
        receipt_no = None
        m = re.search(r"#(\d{4}-\d{4})", subject)
        if m:
            receipt_no = m.group(1)
        amount_cents = None
        for pat in (
            r"Amount paid[^$]*?\$\s*([\d,]+\.\d{2})",
            r"Total[^$]*?\$\s*([\d,]+\.\d{2})",
            r"\$\s*([\d,]+\.\d{2})\s*USD",
            r"\$\s*([\d,]+\.\d{2})",
        ):
            m = re.search(pat, body)
            if m:
                try:
                    amount_cents = int(round(float(m.group(1).replace(",", "")) * 100))
                    break
                except ValueError:
                    pass
        date_str = None
        for pat in (
            r"Date paid\s+(\w+\s+\d{1,2},\s*\d{4})",
            r"Paid on\s+(\w+\s+\d{1,2},\s*\d{4})",
            r"(\w+\s+\d{1,2},\s*\d{4})",
        ):
            m = re.search(pat, body)
            if m:
                try:
                    d = datetime.strptime(m.group(1), "%B %d, %Y")
                    date_str = d.strftime("%Y/%m/%d")
                    break
                except ValueError:
                    continue
        if receipt_no and amount_cents and date_str:
            return {
                "date": date_str,
                "amount": amount_cents,
                "currency": "USD",
                "receipt_no": receipt_no,
                "invoice_number": None,
                "merchant": source.get("merchant_default"),
            }

    # LLM フォールバック (= 旧 Stripe regex 失敗 + 新 Autodesk 全件)
    from src.llm_extract import extract_json
    combined = f"件名: {subject}\n\n本文:\n{body[:1500]}"
    if pdf_text:
        combined += f"\n\nPDF 内容:\n{pdf_text[:3500]}"
    return extract_json(
        f"Wonder Studio (Wonder Dynamics / オートデスク株式会社) の領収書から "
        "JSON で抽出。 旧 Stripe (USD) と 新 Autodesk (JPY、 PDF 添付に詳細) の両方ありうる:\n"
        '{"date": "YYYY/MM/DD (= 領収日)", '
        '"amount": 整数 (= 円なら円、 USD ならセント単位、 $99.99 → 9999、 ¥10,800 → 10800), '
        '"currency": "USD"|"JPY", '
        '"receipt_no": "領収書番号 / 注文番号" (任意), '
        '"invoice_number": "T + 13 桁 (適格事業者番号、 オートデスク株式会社のみ存在)", '
        '"merchant": "発行事業者名 (例: Wonder Dynamics, Inc. / オートデスク株式会社)"}\n\n'
        f"{combined}\n\nJSON のみ返してください。"
    )


def _save_to_db(rows: list[dict]) -> int:
    if not rows:
        return 0
    from src.db import connect as _connect_central
    from src.server.categorize import normalize_desc
    con = _connect_central(DB_PATH)
    inserted = 0
    receipt_added = 0
    for r in rows:
        currency = r.get("currency", "USD")
        receipt_no = r.get("receipt_no")
        merchant = r.get("merchant") or "Wonder Dynamics, Inc."
        if currency == "JPY":
            desc = f"Wonder Studio {r['date'][:7]}" + (f" #{receipt_no}" if receipt_no else "")
        else:
            desc = f"Wonder Studio ({currency}) {r['date'][:7]}" + (f" #{receipt_no}" if receipt_no else "")
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
            inv = r.get("invoice_number") or None  # 米国請求は常に NULL
            existing = con.execute(
                "SELECT id, filename FROM receipts WHERE merchant=? AND transaction_id=?",
                (merchant, tx_id),
            ).fetchone()
            if existing:
                rid, existing_fn = existing
                if pdf_filename and not existing_fn:
                    con.execute("UPDATE receipts SET filename=? WHERE id=?", (pdf_filename, rid))
                    receipt_added += 1
                if inv:
                    con.execute(
                        "UPDATE receipts SET invoice_number=? WHERE id=? "
                        "AND (invoice_number IS NULL OR invoice_number='')",
                        (inv, rid),
                    )
                con.execute(
                    "INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id) VALUES (?, ?)",
                    (rid, tx_id),
                )
                continue
            cur = con.execute(
                "INSERT INTO receipts "
                "(filename, receipt_date, merchant, invoice_number, amount, transaction_id, saved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pdf_filename, r["date"], merchant, inv, int(r["amount"]), tx_id, r["fetched_at"]),
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
        print("[wonder_studio] GMAIL_USER / GMAIL_APP_PASSWORD 未設定 (= gmail plugin で設定)")
        return []

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    rows: list[dict] = []

    def _fetch_all() -> list[dict]:
        # フェーズ 1: IMAP で全メールの raw bytes だけ取得して即切断
        # (= LLM 中に IMAP を保持すると Gmail が「Too many simultaneous connections」 で切る)
        raw_msgs: list[tuple[dict, bytes]] = []
        with imaplib.IMAP4_SSL("imap.gmail.com") as M:
            M.login(user, pw)
            M.select("INBOX")
            for src in _SOURCES:
                from_addr = src["from"]
                subj_kw = src["subject_kw"]
                subj_is_ascii = subj_kw.isascii()
                criteria = ["SINCE", cutoff, "FROM", f'"{from_addr}"']
                if subj_is_ascii:
                    criteria += ["SUBJECT", f'"{subj_kw}"']
                typ, data = M.search(None, *criteria)
                if typ != "OK":
                    continue
                msg_ids = data[0].split() if data and data[0] else []
                print(f"[wonder_studio] {from_addr}: {len(msg_ids)} 件 (subject filter 前)")
                for mid in msg_ids:
                    typ, msg_data = M.fetch(mid, "(RFC822)")
                    if typ != "OK" or not msg_data or not msg_data[0]:
                        continue
                    raw_msgs.append((src, msg_data[0][1]))
        # ここで IMAP 切断済 → LLM がいくら時間かかっても影響なし

        # フェーズ 2: パース + PDF 保存
        result: list[dict] = []
        for src, raw in raw_msgs:
            msg = email.message_from_bytes(raw)
            subject = _decode_h(msg.get("Subject") or "")
            subj_kw = src["subject_kw"]
            if not subj_kw.isascii() and subj_kw not in subject:
                continue
            body = _extract_text_body(msg)
            pdfs = _extract_pdf_attachments(msg)
            pdf_text = _read_pdf_text(pdfs[0]) if pdfs else ""
            parsed = _parse_receipt(subject, body, src, pdf_text=pdf_text)
            if not parsed or not parsed.get("date") or not parsed.get("amount"):
                print(f"  パース失敗: {subject!r}")
                continue

            pdf_filename = None
            if pdfs:
                RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
                date_str = parsed.get("date") or ""
                rno = parsed.get("receipt_no") or date_str[:7].replace("/", "-")
                safe = re.sub(r"[^\w\-]", "_", str(rno))[:40]
                pdf_filename = f"wonder_studio_{safe}.pdf"
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
        print(f"[wonder_studio] dry_run: {len(rows)} 件")
        for r in rows:
            print(f"  {r}")
        return rows

    n = _save_to_db(rows)
    # 個別 trigger でも shop_card link を再生成 (= idempotent)
    shop_link = 0
    try:
        from src.matching import run_shop_matching
        matches = run_shop_matching()
        shop_link = sum(1 for m in matches if m.shop_bank == "Wonder Studio")
    except Exception as e:
        print(f"  shop_matching 失敗: {e}")
    # VPASS オートデスク行に Wonder Studio plugin の receipt を流用 link
    # (= 適格事業者番号 T6010001074615 を VPASS 側でも見えるように)
    receipt_link_added = _link_vpass_autodesk()
    # /ui キャッシュ即時無効化 (= povo と同じパターン)
    try:
        from src.server import _invalidate_tx_cache
        _invalidate_tx_cache()
    except Exception:
        pass
    print(
        f"[wonder_studio] {len(rows)} 件取得 / DB +{n} 件追加 / "
        f"shop_card link {shop_link} 件 / VPASS receipt link +{receipt_link_added} 件"
    )
    return rows


def _link_vpass_autodesk() -> int:
    """VPASS の「オートデスク」 行のうち、 同金額の Wonder Studio (Autodesk) tx と
    一致する行に Wonder Studio plugin の receipt を流用 link。

    旧 Stripe (USD) の merchant=Wonder Dynamics, Inc. は対象外 (= 円金額が
    一致しないため自然に弾かれる + invoice_number=NULL なので link しても税控除
    対象にならない)。 新 Autodesk (JPY) のみ対象。
    """
    from src.db import connect as _connect_central
    con = _connect_central(DB_PATH)
    before = con.execute("SELECT COUNT(*) FROM receipt_links").fetchone()[0]
    con.execute("""
        WITH match AS (
            SELECT v.id AS v_tx_id, r.id AS receipt_id
            FROM transactions v
            JOIN transactions w
              ON w.bank = 'Wonder Studio'
             AND w.debit = v.debit
             AND ABS(julianday(replace(v.date,'/','-')) - julianday(replace(w.date,'/','-'))) <= 15
            JOIN receipts r
              ON r.transaction_id = w.id
             AND r.merchant = 'オートデスク株式会社'
            WHERE v.bank = 'VPASS'
              AND v.description LIKE '%オートデスク%'
        )
        INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id)
        SELECT receipt_id, v_tx_id FROM match
    """)
    after = con.execute("SELECT COUNT(*) FROM receipt_links").fetchone()[0]
    con.commit()
    con.close()
    n = after - before
    if n > 0:
        print(f"  VPASS オートデスク に receipt link: +{n} 件")
    return n


if __name__ == "__main__":
    asyncio.run(run(dry_run=True))
