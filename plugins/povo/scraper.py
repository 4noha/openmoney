"""povo (KDDI) 請求書 PDF を Gmail 経由で取得する plugin。

仕様:
1. Gmail (IMAP) で from=info@povo.jp + subject「請求書」 を含むメールを取得
2. 添付 PDF をパスワード (= 誕生日 YYYY-MM-DD、 env POVO_PDF_PASSWORD) で復号
3. PDF テキストを抽出し、 通話料分の請求金額 (= 「ご請求金額（税込）」) と
   適格事業者番号 (= T + 13 桁) を抽出
4. transactions に bank='povo' で INSERT、 メタ receipt (= filename NULL +
   invoice_number=T8010001213484 + merchant=KDDI Digital Life 株式会社) を
   作成して receipt_links で紐付け

スコープ:
- ✅ 通話料 / SMS / その他料金 (= 適格請求書あり、 KDDI Digital Life 発行)
- ❌ トッピング購入履歴 (= データ追加 / 使い放題等) は **非対応**。
  PDF 内に「トッピング（都度購入）の適格請求書の発行をご希望の場合、
  請求書発行申込フォームよりお手続きください」 と記載されている通り、
  申請フォームから個別申請しないと適格証明書が発行されない仕様のため。
  VPASS の「povoご利用料金」 行にはトッピング合計が含まれるため、 この plugin
  で取り込む通話料分とは金額が一致しない (= 部分インボイスとなる)。

env:
  GMAIL_USER          : Gmail アドレス (= Gmail plugin と共通)
  GMAIL_APP_PASSWORD  : Gmail アプリパスワード (= 同上)
  POVO_PDF_PASSWORD   : povo 請求書 PDF のパスワード (= 誕生日 YYYY-MM-DD 形式)
  ANTHROPIC_API_KEY   : LLM フォールバック用 (任意、 regex 失敗時のみ使用)
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
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "transactions.db"
RECEIPTS_DIR = Path(__file__).parent.parent.parent / "invoices" / "receipts"

_FROM = "info@povo.jp"
_SUBJECT_KW = "請求書"
_BANK = "povo"


def _extract_pdf_attachments(msg: email.message.Message) -> list[bytes]:
    """multipart メールから PDF 添付を抽出。"""
    attachments: list[bytes] = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        ctype = part.get_content_type()
        cdisp = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename() or ""
        if ctype == "application/pdf" or filename.lower().endswith(".pdf") or "attachment" in cdisp:
            payload = part.get_payload(decode=True)
            if payload and payload.startswith(b"%PDF"):
                attachments.append(payload)
    return attachments


def _read_pdf_text(pdf_bytes: bytes, password: str) -> str:
    """pdfplumber でパスワード付き PDF を開いてテキスト抽出。"""
    import pdfplumber
    text_parts: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes), password=password) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            text_parts.append(t)
    return "\n".join(text_parts)


def _parse_povo_text(text: str) -> dict | None:
    """povo 請求書テキストから date / amount / invoice_number / merchant を抽出。

    PDF 構造 (= 1 ページ目):
        ご利用番号  ご請求確定日  ご請求金額（税込）
        080-XXXX-XXXX  YYYY年MM月DD日  795 円
        ...
        合計  795 円
        税込
        本請求書は ... KDDI Digital Life 株式会社 (T8010001213484) が発行している...

    通話料分のみ抽出 (= トッピングは PDF 上「申請フォーム」 経由でしか適格請求書が
    出ないため非対応)。 失敗時のみ LLM フォールバック。
    """
    # 1) ご請求確定日 (例: 2026年04月01日) → 請求 transaction の date
    confirm_date = None
    m = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m:
        confirm_date = (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # 2) 通話料分の請求金額 (= 「合計 \d+ 円\n税込」 のパターンが最も確実)
    amount = None
    # パターン優先順:
    # a) 「合計\s+(\d+)\s*円\s*\n\s*税込」 (= Page 1 下部の本請求合計、 最も確実)
    # b) 「(\d+)\s*円\s*\n\s*お客様番号」 (= ヘッダ表行末尾の「795 円」、 次行が
    #    「お客様番号：」 で始まる構造を利用、 電話番号誤マッチを回避)
    for pat in (
        r"合計\s+([\d,]+)\s*円\s*\n\s*税込",
        r"([\d,]+)\s*円\s*\n\s*お客様番号",
    ):
        m2 = re.search(pat, text)
        if m2:
            try:
                amount = int(m2.group(1).replace(",", ""))
                break
            except ValueError:
                pass

    # 3) 適格事業者番号 (= T + 13 桁) と発行事業者名
    invoice_number = None
    merchant = "KDDI Digital Life 株式会社"  # default (= 現在の povo 発行体)
    m = re.search(r"T(\d{13})", text)
    if m:
        invoice_number = "T" + m.group(1)
    # 発行事業者名は PDF 中「..., XXX 株式会社 (T...) が発行している」 の
    # 直前句読点 (、 , 。) からマッチ末尾までを拾う
    m = re.search(r"[、,。]\s*([^、,。]+?)\s*\(T\d{13}\)\s*が発行", text)
    if m:
        merchant = m.group(1).strip()

    if confirm_date and amount is not None:
        y, mo, d = confirm_date
        return {
            "date": f"{y:04d}/{mo:02d}/{d:02d}",
            "amount": amount,
            "description": f"povo 通信料 ({y}年{mo}月分)",
            "invoice_number": invoice_number,
            "merchant": merchant,
        }

    # LLM フォールバック (= regex で取れない場合のみ。 ANTHROPIC_API_KEY
    # 未設定なら共通モジュールが None を返すので povo は regex オンリーで完結)
    from src.llm_extract import extract_json
    parsed = extract_json(
        "povo (KDDI) 請求書テキストから JSON で抽出 (= 通話料分のみ、 トッピング除く):\n"
        '{"date": "YYYY/MM/DD (ご請求確定日)", "amount": 整数円 (通話料合計、 税込), '
        '"description": "povo 通信料 (YYYY年M月分)", '
        '"invoice_number": "T + 13 桁 (適格事業者番号)", '
        '"merchant": "発行事業者名"}\n\n'
        f"テキスト:\n{text[:3000]}\n\nJSON のみ返してください。"
    )
    return parsed


def _save_to_db(transactions: list[dict]) -> int:
    """transactions に INSERT + 適格事業者番号があれば メタ receipt + receipt_links。

    既存の同 (bank, date, description) があれば INSERT OR IGNORE で skip。
    receipt は同 (invoice_number, receipt_date, transaction_id) で重複を避ける。
    """
    if not transactions:
        return 0
    from src.server.categorize import normalize_desc
    from src.db import connect as _connect_central
    con = _connect_central(DB_PATH)
    inserted = 0
    receipt_added = 0
    for tx in transactions:
        try:
            cur = con.execute(
                "INSERT OR IGNORE INTO transactions "
                "(bank, date, description, description_normalized, debit, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _BANK, tx["date"], tx["description"],
                    normalize_desc(tx["description"]),
                    int(tx.get("amount", 0)), tx["fetched_at"],
                ),
            )
            inserted += cur.rowcount
            # 既存も含めた transaction_id を取得
            tx_id_row = con.execute(
                "SELECT id FROM transactions WHERE bank=? AND date=? AND description=?",
                (_BANK, tx["date"], tx["description"]),
            ).fetchone()
            if not tx_id_row:
                continue
            tx_id = tx_id_row[0]

            inv = tx.get("invoice_number")
            if not inv:
                continue
            filename = tx.get("filename")
            # 既に同 invoice_number + 同 transaction_id の receipt があれば、
            # filename が NULL なら UPDATE で PDF を紐付ける、 なければ skip
            exists = con.execute(
                "SELECT id, filename FROM receipts WHERE invoice_number=? AND transaction_id=?",
                (inv, tx_id),
            ).fetchone()
            if exists:
                rid, existing_fn = exists
                if filename and not existing_fn:
                    con.execute(
                        "UPDATE receipts SET filename=? WHERE id=?",
                        (filename, rid),
                    )
                    receipt_added += 1
                # 既存 receipt が link されていない場合に備えて idempotent に追加
                con.execute(
                    "INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id) VALUES (?, ?)",
                    (rid, tx_id),
                )
                continue
            cur = con.execute(
                "INSERT INTO receipts "
                "(filename, receipt_date, merchant, invoice_number, amount, transaction_id, saved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    filename, tx["date"], tx.get("merchant"), inv,
                    int(tx.get("amount", 0)), tx_id, tx["fetched_at"],
                ),
            )
            rid = cur.lastrowid
            con.execute(
                "INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id) VALUES (?, ?)",
                (rid, tx_id),
            )
            receipt_added += 1
        except (ValueError, sqlite3.Error) as e:
            print(f"  DB保存スキップ: {tx} ({e})")
    con.commit()
    con.close()
    if receipt_added:
        print(f"  メタ receipt: +{receipt_added} 件 (= 適格事業者番号付与)")
    return inserted


def _lock_topping_as_personal() -> int:
    """POVO_LOCK_TOPPING_PERSONAL=1 のとき、 VPASS の「ｐｏｖｏご利用料金」 のうち
    povo PDF (= 適格請求書あり) と金額不一致 (= トッピング購入、 適格証明書発行
    フォーム未申請のため tax 控除対象外) を「今回は個人支出」 でロック。

    tx_meta に (key='lock_source', value='povo') を入れることで、
    /api/transactions/{id}/category 等の category 変更 API は 423 Locked で
    拒否する (= ハードロック、 自動分類も skip)。

    OFF (= POVO_LOCK_TOPPING_PERSONAL 未設定 / "0") の場合、 既存ロックを解除
    + category を空に戻す (= ユーザが再分類できる状態に)。
    """
    enabled = os.environ.get("POVO_LOCK_TOPPING_PERSONAL", "").strip() == "1"
    from src.db import connect as _connect_central
    con = _connect_central(DB_PATH)

    if not enabled:
        # OFF 時: 既存の povo ロック行を解除 (= category クリア + tx_meta 削除)
        unlocked = con.execute("""
            UPDATE transactions SET category=''
            WHERE id IN (
                SELECT transaction_id FROM tx_meta
                WHERE key='lock_source' AND value='povo'
            )
        """).rowcount
        con.execute(
            "DELETE FROM tx_meta WHERE key='lock_source' AND value='povo'"
        )
        con.commit()
        con.close()
        if unlocked > 0:
            print(f"  POVO_LOCK_TOPPING_PERSONAL=OFF: 既存ロック {unlocked} 件を解除")
        return 0

    # ON 時: VPASS ｐｏｖｏご利用料金 のうち、 povo PDF と金額一致しない (=
    # 同月内 povo bank に同金額 transaction が無い) 行を「今回は個人支出」 +
    # tx_meta 'lock_source'='povo' でロック
    rows = con.execute("""
        SELECT v.id, v.date, v.debit, v.category
        FROM transactions v
        WHERE v.bank='VPASS'
          AND v.description LIKE '%ｐｏｖｏ%'
          AND NOT EXISTS (
              SELECT 1 FROM transactions p
              WHERE p.bank='povo'
                AND substr(p.date, 1, 7) = substr(v.date, 1, 7)
                AND p.debit = v.debit
          )
    """).fetchall()
    locked = 0
    for tx_id, date, debit, current_cat in rows:
        # 既にロック済みなら skip (idempotent)
        existing = con.execute(
            "SELECT value FROM tx_meta WHERE transaction_id=? AND key='lock_source'",
            (tx_id,),
        ).fetchone()
        if existing and existing[0] == "povo":
            continue
        # 他 plugin のロックがあれば衝突回避 (= povo は手を出さない)
        if existing and existing[0] != "povo":
            print(f"  tx={tx_id} は別の lock_source={existing[0]!r} のため skip")
            continue
        con.execute(
            "UPDATE transactions SET category='今回は個人支出' WHERE id=?",
            (tx_id,),
        )
        con.execute(
            "INSERT OR REPLACE INTO tx_meta (transaction_id, key, value, source) "
            "VALUES (?, 'lock_source', 'povo', 'povo')",
            (tx_id,),
        )
        locked += 1
    con.commit()
    con.close()
    if locked > 0:
        print(f"  POVO_LOCK_TOPPING_PERSONAL=ON: トッピング {locked} 件を「今回は個人支出」 でロック")
    return locked


def _link_vpass_charges() -> int:
    """VPASS の「ｐｏｖｏご利用料金」 行のうち、 同月内 povo 通話料 transaction と
    金額完全一致するものに povo メタレシート (= T8010001213484) を流用 link。

    トッピング購入 (= 月額と異なる小額の VPASS 行) は適格証明書なしのため対象外。
    receipt_links は (receipt_id, transaction_id) UNIQUE なので idempotent。
    """
    from src.db import connect as _connect_central
    con = _connect_central(DB_PATH)
    before = con.execute("SELECT COUNT(*) FROM receipt_links").fetchone()[0]
    con.execute("""
        WITH match AS (
            SELECT v.id AS v_tx_id, r.id AS receipt_id
            FROM transactions v
            JOIN transactions p
              ON p.bank = 'povo'
             AND substr(p.date, 1, 7) = substr(v.date, 1, 7)
             AND p.debit = v.debit
            JOIN receipts r
              ON r.transaction_id = p.id
             AND r.invoice_number = 'T8010001213484'
            WHERE v.bank = 'VPASS'
              AND v.description LIKE '%ｐｏｖｏ%'
        )
        INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id)
        SELECT receipt_id, v_tx_id FROM match
    """)
    after = con.execute("SELECT COUNT(*) FROM receipt_links").fetchone()[0]
    n = after - before
    con.commit()
    con.close()
    if n > 0:
        print(f"  VPASS 突合: +{n} 件 (= 同月同金額の ｐｏｖｏご利用料金 にメタ receipt link)")
    return n


async def run(*, days: int = 90, dry_run: bool = False) -> list[dict]:
    """povo 請求書を Gmail から取得 → PDF 復号 + パース → DB 保存。

    days: 過去 N 日分 (default 90 = 約 3 ヶ月分)
    dry_run: True なら DB 保存なし、 パース結果のみ print
    """
    user = (os.environ.get("GMAIL_USER") or "").strip()
    # アプリパスワードは "xxxx xxxx xxxx xxxx" 形式でコピーされる場合があるので space 除去
    pw = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    pdf_pw = os.environ.get("POVO_PDF_PASSWORD")
    if not user or not pw:
        print("[povo] GMAIL_USER / GMAIL_APP_PASSWORD 未設定 (= gmail plugin の設定画面で入力)")
        return []
    if not pdf_pw:
        print("[povo] POVO_PDF_PASSWORD 未設定 (= povo plugin の設定画面で誕生日 YYYY-MM-DD を入力)")
        return []

    transactions: list[dict] = []
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")

    def _fetch_all():
        from email.header import decode_header

        def _decode(s: str) -> str:
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

        result: list[dict] = []
        with imaplib.IMAP4_SSL("imap.gmail.com") as M:
            M.login(user, pw)
            M.select("INBOX")
            # IMAP search は ASCII しか扱えないので FROM のみで絞り込み、
            # SUBJECT (= 日本語の「請求書」) は fetch 後に Python 側で filter
            typ, data = M.search(None, "SINCE", cutoff, "FROM", f'"{_FROM}"')
            if typ != "OK":
                return result
            msg_ids = data[0].split() if data and data[0] else []
            print(f"[povo] {_FROM} のメール (= subject filter 前): {len(msg_ids)} 件")
            for mid in msg_ids:
                typ, msg_data = M.fetch(mid, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                # subject に「請求書」 が含まれない通知メール (= 開通案内、 利用量警告等) は skip
                subject = _decode(msg.get("Subject") or "")
                if _SUBJECT_KW not in subject:
                    continue
                pdfs = _extract_pdf_attachments(msg)
                if not pdfs:
                    continue
                for pdf_bytes in pdfs:
                    try:
                        text = _read_pdf_text(pdf_bytes, pdf_pw)
                    except Exception as e:
                        print(f"  PDF 復号/抽出失敗: {e}")
                        continue
                    parsed = _parse_povo_text(text)
                    if not parsed:
                        print(f"  パース失敗 (text head: {text[:80]!r})")
                        continue
                    # PDF 自体を保存 (= 適格請求書の本物として参照可能にする)
                    # ファイル名は invoice_YYYY-MM.pdf。 パスワードは保ったまま保存
                    # するので、 後で開く時に POVO_PDF_PASSWORD と同じ誕生日が必要
                    pdf_filename = None
                    date_str = parsed.get("date") or ""
                    if date_str:
                        ym = date_str[:7].replace("/", "-")  # 2026/04/01 → 2026-04
                        pdf_filename = f"povo_{ym}.pdf"
                        RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
                        pdf_path = RECEIPTS_DIR / pdf_filename
                        if not pdf_path.exists():
                            pdf_path.write_bytes(pdf_bytes)
                    tx = {
                        "date": parsed.get("date"),
                        "description": parsed.get("description") or "povo 通信料",
                        "amount": int(parsed.get("amount") or 0),
                        "invoice_number": parsed.get("invoice_number"),
                        "merchant": parsed.get("merchant"),
                        "filename": pdf_filename,
                        "fetched_at": datetime.now().isoformat(),
                    }
                    if not tx["date"] or tx["amount"] <= 0:
                        continue
                    result.append(tx)
        return result

    transactions = await asyncio.get_event_loop().run_in_executor(None, _fetch_all)

    if dry_run:
        print(f"[povo] dry_run: {len(transactions)} 件 (DB 保存なし)")
        for tx in transactions:
            print(f"  {tx}")
        return transactions

    n = _save_to_db(transactions)
    linked = _link_vpass_charges()
    locked = _lock_topping_as_personal()
    # 個別 trigger (= /api/scrape/povo) の場合 daemon の最終 matching が走らない
    # ため、 povo ↔ VPASS shop_card link を plugin 内で再実行 (= idempotent)
    shop_link = 0
    try:
        from src.matching import run_shop_matching
        matches = run_shop_matching()
        shop_link = sum(1 for m in matches if m.shop_bank == "povo")
    except Exception as e:
        print(f"  shop_matching 失敗: {e}")
    # /ui の transaction 一覧キャッシュ (= 1 日 TTL) は受信後即時無効化しないと
    # 「DB 上は link 済みだが UI に出てこない」 状態になる。 plugin runner は
    # daemon と同プロセスなので関数を直接呼べる
    try:
        from src.server import _invalidate_tx_cache
        _invalidate_tx_cache()
    except Exception as e:
        print(f"  キャッシュ無効化失敗: {e}")
    print(
        f"[povo] {len(transactions)} 件取得 / DB +{n} 件追加 / "
        f"VPASS receipt link +{linked} 件 / shop_card link {shop_link} 件 / "
        f"トッピング lock {locked} 件"
    )
    return transactions


if __name__ == "__main__":
    asyncio.run(run(dry_run=True))
