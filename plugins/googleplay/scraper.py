"""Google Play ご注文明細メールを Gmail 経由で取得する plugin。

googleplay-noreply@google.com から「Google Play のご注文明細（ご注文: YYYY/MM/DD）」
件名で 1 注文 = 1 メール送信される。 中身は購入アプリ / アプリ内課金 / サブスクなど。

メール本文に T 番号 + 発行事業者名が含まれる:
- アプリ提供元 (= WOAN TECHNOLOGY LIMITED 等) は海外法人で T 番号なし
- Google Play 自身 = Google Asia Pacific Pte. Ltd. (シンガポール) で T 番号なし
- Google G.K. (= 日本法人) 発行のものは T 番号あり

LLM 主導で抽出 (= 多様なフォーマット)。 取れた T 番号があれば receipts に保存し
/invoices に出る、 取れなければ receipt のみ作成 (= 仕入税額控除対象外)。

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

_FROM = "googleplay-noreply@google.com"
_SUBJECT_KW = "ご注文明細"  # 日本語 → Python 側 filter
_BANK = "Google Play"


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
        "Google Play ご注文明細メールから JSON で抽出 (= 1 注文 = 1 メール、 アプリ / "
        "アプリ内課金 / サブスク 等):\n"
        '{"date": "YYYY/MM/DD (= 注文日)", '
        '"amount": 整数 (= 円なら円、 USD ならセント単位), '
        '"currency": "JPY"|"USD", '
        '"order_id": "GPA.XXXX-XXXX-XXXX-XXXXX (Google Play 注文 ID)" (任意), '
        '"app_name": "アプリ / 商品名 (= 例: SwitchBot)", '
        '"developer": "アプリ提供元 (= 例: WOAN TECHNOLOGY LIMITED)", '
        '"merchant": "発行事業者名 (= 適格請求書発行者、 通常は Google G.K. or アプリ提供元)", '
        '"invoice_number": "T + 13 桁 (適格事業者番号、 日本法人発行のみ存在)"}\n\n'
        f"件名: {subject}\n\n本文:\n{body[:3500]}\n\nJSON のみ返してください。"
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
        currency = r.get("currency", "JPY")
        app_name = r.get("app_name") or "Google Play"
        order_id = r.get("order_id")
        date_str = r["date"]
        if currency == "JPY":
            desc = f"Google Play {app_name}" + (f" #{order_id}" if order_id else f" {date_str}")
        else:
            desc = f"Google Play {app_name} ({currency})" + (f" #{order_id}" if order_id else f" {date_str}")
        merchant = (r.get("merchant") or r.get("developer") or "Google Asia Pacific Pte. Ltd.")
        inv = (r.get("invoice_number") or "").strip() or None
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
                "SELECT id, invoice_number FROM receipts WHERE merchant=? AND transaction_id=?",
                (merchant, tx_id),
            ).fetchone()
            if existing:
                rid, existing_inv = existing
                if inv and not existing_inv:
                    con.execute(
                        "UPDATE receipts SET invoice_number=? WHERE id=?", (inv, rid),
                    )
                con.execute(
                    "INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id) VALUES (?, ?)",
                    (rid, tx_id),
                )
                continue
            cur = con.execute(
                "INSERT INTO receipts "
                "(filename, receipt_date, merchant, invoice_number, amount, transaction_id, saved_at) "
                "VALUES (NULL, ?, ?, ?, ?, ?, ?)",
                (date_str, merchant, inv, int(r["amount"]), tx_id, r["fetched_at"]),
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


def _link_vpass_google() -> int:
    """VPASS の "GOOGLE *<アプリ名>" 行に Google Play plugin の receipt を流用 link。

    Google Play 課金の VPASS 表記例:
    - GOOGLE SWITCHBOT
    - GOOGLE *<アプリ名>
    """
    from src.db import connect as _connect_central
    con = _connect_central(DB_PATH)
    before = con.execute("SELECT COUNT(*) FROM receipt_links").fetchone()[0]
    con.execute("""
        WITH match AS (
            SELECT v.id AS v_tx_id, r.id AS receipt_id
            FROM transactions v
            JOIN transactions g
              ON g.bank = 'Google Play'
             AND g.debit = v.debit
             AND ABS(julianday(replace(v.date,'/','-')) - julianday(replace(g.date,'/','-'))) <= 14
            JOIN receipts r ON r.transaction_id = g.id
            WHERE v.bank = 'VPASS' AND v.description LIKE '%GOOGLE%'
        )
        INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id)
        SELECT receipt_id, v_tx_id FROM match
    """)
    after = con.execute("SELECT COUNT(*) FROM receipt_links").fetchone()[0]
    con.commit()
    con.close()
    n = after - before
    if n > 0:
        print(f"  VPASS GOOGLE link: +{n} 件")
    return n


async def run(*, days: int = 365, dry_run: bool = False) -> list[dict]:
    user = (os.environ.get("GMAIL_USER") or "").strip()
    pw = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    if not user or not pw:
        print("[googleplay] GMAIL_USER / GMAIL_APP_PASSWORD 未設定")
        return []

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")

    def _fetch_all() -> list[dict]:
        raw_msgs: list[bytes] = []
        with imaplib.IMAP4_SSL("imap.gmail.com") as M:
            M.login(user, pw)
            M.select("INBOX")
            typ, data = M.search(None, "SINCE", cutoff, "FROM", f'"{_FROM}"')
            if typ != "OK":
                return []
            msg_ids = data[0].split() if data and data[0] else []
            print(f"[googleplay] {_FROM}: {len(msg_ids)} 件")
            for mid in msg_ids:
                typ, msg_data = M.fetch(mid, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw_msgs.append(msg_data[0][1])

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
            result.append({**parsed, "fetched_at": datetime.now().isoformat()})
        return result

    rows = await asyncio.get_event_loop().run_in_executor(None, _fetch_all)

    if dry_run:
        print(f"[googleplay] dry_run: {len(rows)} 件")
        for r in rows:
            print(f"  {r}")
        return rows

    n = _save_to_db(rows)
    linked = _link_vpass_google()
    shop_link = 0
    try:
        from src.matching import run_shop_matching
        matches = run_shop_matching()
        shop_link = sum(1 for m in matches if m.shop_bank == "Google Play")
    except Exception as e:
        print(f"  shop_matching 失敗: {e}")
    try:
        from src.server import _invalidate_tx_cache
        _invalidate_tx_cache()
    except Exception:
        pass
    print(f"[googleplay] {len(rows)} 件取得 / DB +{n} 件追加 / VPASS link +{linked} / shop_card link {shop_link}")
    return rows


if __name__ == "__main__":
    asyncio.run(run(dry_run=True))
