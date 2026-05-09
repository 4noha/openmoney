"""Gmail 経由で明細メールを取得して transactions に保存する plugin scraper。

IMAP (= Google アプリパスワード) でメール本文を取得 → Claude API で構造化抽出
(= 日付 / 金額 / 店名) → DB INSERT という流れ。 各送信者ごとの parser_hint は
config/gmail_sources.toml(.local.toml) で指定。

env:
  GMAIL_USER          : Gmail メールアドレス
  GMAIL_APP_PASSWORD  : アプリパスワード (= Google アカウントで発行、 16 桁)
  ANTHROPIC_API_KEY   : Claude API key (= 既存)
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


def _decode_header(s: str) -> str:
    if not s:
        return ""
    parts = decode_header(s)
    out = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(enc or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _extract_body(msg: email.message.Message) -> str:
    """multipart 対応で text/plain 本文を抽出。 無ければ text/html を strip。"""
    if msg.is_multipart():
        # text/plain 優先
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset, errors="replace")
                except (LookupError, TypeError):
                    return payload.decode("utf-8", errors="replace")
        # fallback: text/html
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


def _parse_with_claude(body: str, source: dict) -> dict | None:
    """LLM で構造化抽出。 {date, amount, description, merchant} を返す。

    モデル / API key 周りは src.llm_extract に集約。 ANTHROPIC_API_KEY 未設定時は
    None が返り、 呼び出し側で skip される。
    """
    from src.llm_extract import extract_json
    prompt = f"""以下のメール本文から取引情報を JSON で抽出してください。

source: {source.get('name', '?')}
hint: {source.get('parser_hint', '')}

JSON フォーマット:
{{
  "date": "YYYY/MM/DD",     // 利用日 / 注文日 (不明なら null)
  "amount": 1234,           // 金額 (税込、 整数円)
  "description": "店名・利用内容 (80 字以内)",
  "merchant": "店名 (任意)"
}}

メール本文:
{body[:4000]}

JSON のみ返してください。 余分なテキスト不要。
"""
    return extract_json(prompt)


def _save_to_db(transactions: list[dict]) -> int:
    """transactions に INSERT。 description_normalized も同時保存 (= 既存 scraper パターン)。"""
    if not transactions:
        return 0
    from src.server.categorize import normalize_desc
    from src.db import connect as _connect_central
    con = _connect_central(DB_PATH)
    inserted = 0
    for tx in transactions:
        try:
            cur = con.execute(
                "INSERT OR IGNORE INTO transactions "
                "(bank, date, description, description_normalized, debit, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    tx["bank"],
                    tx["date"],
                    tx["description"],
                    normalize_desc(tx["description"]),
                    int(tx.get("amount", 0)),
                    tx["fetched_at"],
                ),
            )
            inserted += cur.rowcount
        except (ValueError, sqlite3.Error) as e:
            print(f"  DB保存スキップ: {tx} ({e})")
    con.commit()
    con.close()
    return inserted


async def run(*, days: int = 30, dry_run: bool = False) -> list[dict]:
    """Gmail から明細メールを取得して DB 保存。

    days: 過去 N 日分を取得 (default 30)
    dry_run: True なら DB 保存せずパース結果のみ print
    """
    user = (os.environ.get("GMAIL_USER") or "").strip()
    # Google のアプリパスワード表示は "xxxx xxxx xxxx xxxx" のように space 入り
    # でコピーされやすい。 IMAP には space 抜きで渡す
    pw = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    if not user or not pw:
        print("[gmail] GMAIL_USER / GMAIL_APP_PASSWORD 未設定")
        return []

    from src.personal.gmail_sources import load_gmail_sources
    sources = load_gmail_sources()
    if not sources:
        print("[gmail] config/gmail_sources.toml(.local.toml) に source 未定義")
        return []

    transactions: list[dict] = []
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    # IMAP は同期ライブラリなので executor で実行
    def _fetch_all():
        result: list[dict] = []
        with imaplib.IMAP4_SSL("imap.gmail.com") as M:
            M.login(user, pw)
            M.select("INBOX")
            for src in sources:
                from_addr = (src.get("from") or "").strip()
                subj = (src.get("subject") or "").strip()
                # IMAP search は ASCII しか扱えないので FROM のみで絞る。
                # SUBJECT に日本語が含まれる場合は fetch 後に Python 側で filter
                subj_is_ascii = subj.isascii()
                criteria = ["SINCE", cutoff, "FROM", f'"{from_addr}"']
                if subj and subj_is_ascii:
                    criteria += ["SUBJECT", f'"{subj}"']
                typ, data = M.search(None, *criteria)
                if typ != "OK":
                    continue
                msg_ids = data[0].split() if data and data[0] else []
                print(f"[gmail] {src['name']} ({from_addr}): {len(msg_ids)} 件")
                for mid in msg_ids:
                    typ, msg_data = M.fetch(mid, "(RFC822)")
                    if typ != "OK" or not msg_data or not msg_data[0]:
                        continue
                    raw = msg_data[0][1]
                    msg = email.message_from_bytes(raw)
                    # 日本語 subject は Python 側で filter
                    if subj and not subj_is_ascii:
                        actual = _decode_header(msg.get("Subject") or "")
                        if subj not in actual:
                            continue
                    body = _extract_body(msg)
                    if not body:
                        continue
                    parsed = _parse_with_claude(body, src)
                    if not parsed or not parsed.get("amount"):
                        continue
                    tx = {
                        "bank": f"Gmail:{src['name']}",
                        "date": parsed.get("date") or "",
                        "description": (parsed.get("description") or "")[:200],
                        "amount": int(parsed.get("amount") or 0),
                        "fetched_at": datetime.now().isoformat(),
                    }
                    if not tx["date"] or tx["amount"] <= 0:
                        continue
                    result.append(tx)
        return result

    transactions = await asyncio.get_event_loop().run_in_executor(None, _fetch_all)

    if dry_run:
        print(f"[gmail] dry_run: {len(transactions)} 件 (DB 保存なし)")
        for tx in transactions[:5]:
            print(f"  {tx}")
        return transactions

    n = _save_to_db(transactions)
    print(f"[gmail] {len(transactions)} 件取得 / DB +{n} 件追加")
    return transactions


if __name__ == "__main__":
    asyncio.run(run(dry_run=True))
