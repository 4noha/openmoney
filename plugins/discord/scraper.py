"""Discord 領収書メールを Gmail 経由で取得する plugin。

Discord は PayPal 経由で課金 → VPASS に「PAYPAL *DISCORD (4029357733 )」 表記で
請求が立つ。 一方 Discord 自身も noreply@discord.com から「Discord のお支払いに
成功しました」 件名の領収書メールを送る (= USD / 日本語混在)。 メール本文 (HTML)
を LLM パースして金額・日付・領収書番号を抽出。

Discord は米国法人なので invoice_number=NULL (= 日本の T 番号なし)。 仕入税額
控除対象外。

env:
  GMAIL_USER          : Gmail アドレス (= gmail plugin と共通)
  GMAIL_APP_PASSWORD  : Gmail アプリパスワード
  ANTHROPIC_API_KEY   : LLM 抽出用
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

_FROM = "noreply@discord.com"
_SUBJECT_KW = "お支払い"  # 日本語 → Python 側 filter
_BANK = "Discord"
_MERCHANT = "Discord Inc."


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


def _parse_receipt(subject: str, body: str) -> dict | None:
    from src.llm_extract import extract_json
    return extract_json(
        "Discord 領収書メールから JSON で抽出 (= PayPal 経由決済、 米国 Discord Inc.):\n"
        '{"date": "YYYY/MM/DD (= 領収日)", '
        '"amount": 整数 (= 円なら円、 USD/EUR ならセント単位、 $9.99 → 999、 ¥1,200 → 1200), '
        '"currency": "USD"|"JPY"|"EUR", '
        '"receipt_no": "領収書番号 / 取引 ID" (任意、 無ければ null), '
        '"plan": "Nitro / Nitro Basic 等の プラン名" (任意)}\n\n'
        f"件名: {subject}\n\n本文:\n{body[:3000]}\n\nJSON のみ返してください。"
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
        plan = r.get("plan") or "Discord"
        rno = r.get("receipt_no")
        date_str = r["date"]
        ym = date_str[:7]
        if currency == "JPY":
            desc = f"Discord {plan} {ym}" + (f" #{rno}" if rno else "")
        else:
            desc = f"Discord {plan} ({currency}) {ym}" + (f" #{rno}" if rno else "")
        try:
            cur = con.execute(
                "INSERT OR IGNORE INTO transactions "
                "(bank, date, description, description_normalized, debit, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_BANK, date_str, desc, normalize_desc(desc),
                 int(r["amount"]), r["fetched_at"]),
            )
            inserted += cur.rowcount
            tx_row = con.execute(
                "SELECT id FROM transactions WHERE bank=? AND date=? AND description=?",
                (_BANK, date_str, desc),
            ).fetchone()
            if not tx_row:
                continue
            tx_id = tx_row[0]
            existing = con.execute(
                "SELECT id FROM receipts WHERE merchant=? AND transaction_id=?",
                (_MERCHANT, tx_id),
            ).fetchone()
            if existing:
                con.execute(
                    "INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id) VALUES (?, ?)",
                    (existing[0], tx_id),
                )
                continue
            # Discord は invoice_number=NULL (= 米国法人、 日本の T 番号制度対象外)
            cur = con.execute(
                "INSERT INTO receipts "
                "(filename, receipt_date, merchant, invoice_number, amount, transaction_id, saved_at) "
                "VALUES (NULL, ?, ?, NULL, ?, ?, ?)",
                (date_str, _MERCHANT, int(r["amount"]), tx_id, r["fetched_at"]),
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


def _link_vpass_paypal_discord() -> int:
    """VPASS の "PAYPAL *DISCORD" 行に Discord plugin の receipt を流用 link
    (= 米国法人なので T 番号なしだが、 メタ情報として「Discord 課金」 と分かるように)。
    """
    from src.db import connect as _connect_central
    con = _connect_central(DB_PATH)
    before = con.execute("SELECT COUNT(*) FROM receipt_links").fetchone()[0]
    con.execute("""
        WITH match AS (
            SELECT v.id AS v_tx_id, r.id AS receipt_id
            FROM transactions v
            JOIN transactions d
              ON d.bank = 'Discord'
             AND d.debit = v.debit
             AND ABS(julianday(replace(v.date,'/','-')) - julianday(replace(d.date,'/','-'))) <= 7
            JOIN receipts r ON r.transaction_id = d.id AND r.merchant = 'Discord Inc.'
            WHERE v.bank = 'VPASS' AND v.description LIKE '%DISCORD%'
        )
        INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id)
        SELECT receipt_id, v_tx_id FROM match
    """)
    after = con.execute("SELECT COUNT(*) FROM receipt_links").fetchone()[0]
    con.commit()
    con.close()
    n = after - before
    if n > 0:
        print(f"  VPASS DISCORD link: +{n} 件")
    return n


async def run(*, days: int = 365, dry_run: bool = False) -> list[dict]:
    user = (os.environ.get("GMAIL_USER") or "").strip()
    pw = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    if not user or not pw:
        print("[discord] GMAIL_USER / GMAIL_APP_PASSWORD 未設定")
        return []

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")

    def _fetch_all() -> list[dict]:
        # Phase 1: IMAP で raw 全部取得 → 即切断
        raw_msgs: list[bytes] = []
        with imaplib.IMAP4_SSL("imap.gmail.com") as M:
            M.login(user, pw)
            M.select("INBOX")
            typ, data = M.search(None, "SINCE", cutoff, "FROM", f'"{_FROM}"')
            if typ != "OK":
                return []
            msg_ids = data[0].split() if data and data[0] else []
            print(f"[discord] {_FROM}: {len(msg_ids)} 件 (subject filter 前)")
            for mid in msg_ids:
                typ, msg_data = M.fetch(mid, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw_msgs.append(msg_data[0][1])

        # Phase 2: LLM パース
        result: list[dict] = []
        for raw in raw_msgs:
            msg = email.message_from_bytes(raw)
            subject = _decode_h(msg.get("Subject") or "")
            if _SUBJECT_KW not in subject:
                continue
            body = _extract_text_body(msg)
            parsed = _parse_receipt(subject, body)
            if not parsed or not parsed.get("date") or not parsed.get("amount"):
                print(f"  パース失敗: {subject!r}")
                continue
            result.append({
                **parsed,
                "fetched_at": datetime.now().isoformat(),
            })
        return result

    rows = await asyncio.get_event_loop().run_in_executor(None, _fetch_all)

    if dry_run:
        print(f"[discord] dry_run: {len(rows)} 件")
        for r in rows:
            print(f"  {r}")
        return rows

    n = _save_to_db(rows)
    linked = _link_vpass_paypal_discord()
    shop_link = 0
    try:
        from src.matching import run_shop_matching
        matches = run_shop_matching()
        shop_link = sum(1 for m in matches if m.shop_bank == "Discord")
    except Exception as e:
        print(f"  shop_matching 失敗: {e}")
    try:
        from src.server import _invalidate_tx_cache
        _invalidate_tx_cache()
    except Exception:
        pass
    print(f"[discord] {len(rows)} 件取得 / DB +{n} 件追加 / VPASS link +{linked} / shop_card link {shop_link}")
    return rows


if __name__ == "__main__":
    asyncio.run(run(dry_run=True))
