"""vidIQ 領収書スクレイパー (= playwright login + 領収書ページ探索)。

VidIQ はサブスクサービスで、 Email + Password ログイン → アカウント / 課金管理
ページに領収書一覧がある。 ページ構造は変わりやすいので、 / /account /billing
/settings/billing 等を順に試して「Invoice」 「Receipt」 「領収書」 を含む
リンクを抽出 → 各リンクを PDF 化。

VidIQ は米国 (= San Francisco) 法人なので、 日本の適格事業者番号は発行されず
invoice_number=NULL (= 仕入税額控除対象外、 /invoices には出ない)。 VPASS の
「PAYPAL *VIDIQ」 「VIDIQ (SAN FRANCISCO)」 行への receipt link は付与する
(= 取得証跡として)。

env:
  VIDIQ_EMAIL  : ログイン Email
  VIDIQ_PW     : ログイン パスワード
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright, BrowserContext, Page

DB_PATH = Path(__file__).parent.parent.parent / "transactions.db"
INVOICE_DIR = Path(__file__).parent.parent.parent / "invoices" / "vidiq"
_BROWSER_DATA_DIR = Path(__file__).parent.parent.parent / ".pw_data" / "vidiq"

LOGIN_URL = "https://app.vidiq.com/auth/login"
_BANK = "vidIQ"
_MERCHANT = "vidIQ Inc."

# 領収書ページ候補 URL (= 探索順)
_BILLING_URLS = [
    "https://app.vidiq.com/account/billing",
    "https://app.vidiq.com/account/subscription",
    "https://app.vidiq.com/settings/billing",
    "https://app.vidiq.com/account",
    "https://app.vidiq.com/settings",
]


async def _launch_context(p, headless: bool) -> BrowserContext:
    _BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return await p.chromium.launch_persistent_context(
        user_data_dir=str(_BROWSER_DATA_DIR),
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
    )


def _is_logged_in(url: str) -> bool:
    return "auth/login" not in url and "vidiq.com" in url


async def login(page: Page) -> None:
    email = os.environ.get("VIDIQ_EMAIL", "")
    pw = os.environ.get("VIDIQ_PW", "")
    if not email or not pw:
        raise RuntimeError("VIDIQ_EMAIL / VIDIQ_PW が .env に設定されていません")

    await page.goto(LOGIN_URL, wait_until="load", timeout=60_000)
    await asyncio.sleep(2)

    # 既にログイン済 (永続 context) なら login URL から抜ける
    if _is_logged_in(page.url):
        print("[vidiq] 既存セッション再利用")
        return

    # email + password 入力 (= セレクタ候補を順に試す)
    for sel in ("input[name='email']", "input[type='email']", "input#email"):
        if await page.query_selector(sel):
            await page.fill(sel, email)
            break
    for sel in ("input[name='password']", "input[type='password']", "input#password"):
        if await page.query_selector(sel):
            await page.fill(sel, pw)
            break
    # submit
    for sel in ("button[type='submit']", "button:has-text('Log in')",
                "button:has-text('Sign in')", "button:has-text('Continue')"):
        btn = await page.query_selector(sel)
        if btn:
            await btn.click()
            break
    await page.wait_for_load_state("networkidle", timeout=30_000)
    await asyncio.sleep(2)
    print(f"[vidiq] ログイン後 URL: {page.url}")


async def _find_invoice_links(page: Page) -> list[dict]:
    """billing 系ページを順に goto して「Invoice」「Receipt」「領収書」 を含む
    リンク (= a 要素) を全部抽出。 重複排除して返す。
    """
    found: dict[str, dict] = {}  # href → {href, text, page}
    for url in _BILLING_URLS:
        try:
            await page.goto(url, wait_until="load", timeout=20_000)
            await asyncio.sleep(1.5)
        except Exception as e:
            print(f"[vidiq] {url} 失敗: {e}")
            continue
        if not _is_logged_in(page.url):
            print(f"[vidiq] {url}: ログインが切れた、 stop")
            break
        # ページ内のすべての a 要素を取得
        links = await page.evaluate("""
            () => Array.from(document.querySelectorAll('a, button')).map(el => ({
                tag: el.tagName,
                text: (el.textContent || '').trim().slice(0, 80),
                href: el.tagName === 'A' ? el.href : '',
            })).filter(x => x.text)
        """)
        for link in links:
            t = (link.get("text") or "").lower()
            if not any(kw in t for kw in ("invoice", "receipt", "領収書", "請求書", "billing history")):
                continue
            href = link.get("href") or ""
            key = href or link["text"]
            if key not in found:
                found[key] = {"href": href, "text": link["text"], "from_url": url}
    return list(found.values())


async def _save_invoice_pdfs(page: Page, links: list[dict]) -> list[Path]:
    """各 invoice リンクを開いて PDF 化。 保存パスを返す。

    href が空 (= JS ボタン) の場合は click 後の遷移先を試す。 失敗時 skip。
    """
    INVOICE_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for idx, link in enumerate(links, 1):
        href = link.get("href") or ""
        text = link.get("text") or "invoice"
        safe = re.sub(r"[^\w\-]+", "_", text)[:40]
        pdf_path = INVOICE_DIR / f"{idx:03d}_{safe}.pdf"
        if pdf_path.exists():
            saved.append(pdf_path)
            continue
        try:
            if href and href.startswith("http"):
                await page.goto(href, wait_until="load", timeout=20_000)
            else:
                # JS ボタン: text で query して click
                btn = await page.query_selector(f"a:has-text('{text[:20]}'), button:has-text('{text[:20]}')")
                if btn:
                    await btn.click()
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                else:
                    continue
            await asyncio.sleep(1.5)
            body = await page.evaluate("() => document.body.innerText")
            if len(body.strip()) < 80:
                continue
            await page.pdf(path=str(pdf_path), format="A4", print_background=True)
            if pdf_path.stat().st_size < 5000:
                pdf_path.unlink(missing_ok=True)
                continue
            saved.append(pdf_path)
        except Exception as e:
            print(f"  [{text[:30]}] PDF 化失敗: {e}")
    return saved


def _parse_invoice_pdf(pdf_path: Path) -> dict:
    """PDF テキストから date / amount / receipt_no を抽出 (= LLM 主導)。"""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        return {}
    if not text.strip():
        return {}
    from src.llm_extract import extract_json
    parsed = extract_json(
        "vidIQ の領収書 PDF テキストから JSON で抽出 (= USD subscription、 米国法人):\n"
        '{"date": "YYYY/MM/DD (= 領収日 / Date paid / Issued)", '
        '"amount": 整数 (= USD ならセント単位、 $9.90 → 990、 円なら円), '
        '"currency": "USD"|"JPY", '
        '"receipt_no": "Invoice / Receipt 番号" (任意)}\n\n'
        f"テキスト:\n{text[:3000]}\n\nJSON のみ。"
    )
    return parsed or {}


def _save_to_db(rows: list[dict]) -> int:
    if not rows:
        return 0
    from src.db import connect as _connect_central
    from src.server.categorize import normalize_desc
    con = _connect_central(DB_PATH)
    inserted = 0
    receipt_added = 0
    for r in rows:
        date_str = r.get("date")
        amount = r.get("amount")
        if not date_str or not amount:
            continue
        currency = r.get("currency", "USD")
        rno = r.get("receipt_no")
        ym = date_str[:7]
        if currency == "JPY":
            desc = f"vidIQ {ym}" + (f" #{rno}" if rno else "")
        else:
            desc = f"vidIQ ({currency}) {ym}" + (f" #{rno}" if rno else "")
        try:
            cur = con.execute(
                "INSERT OR IGNORE INTO transactions "
                "(bank, date, description, description_normalized, debit, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_BANK, date_str, desc, normalize_desc(desc),
                 int(amount), r["fetched_at"]),
            )
            inserted += cur.rowcount
            tx_row = con.execute(
                "SELECT id FROM transactions WHERE bank=? AND date=? AND description=?",
                (_BANK, date_str, desc),
            ).fetchone()
            if not tx_row:
                continue
            tx_id = tx_row[0]
            pdf_filename = r.get("pdf_filename")
            existing = con.execute(
                "SELECT id, filename FROM receipts WHERE merchant=? AND transaction_id=?",
                (_MERCHANT, tx_id),
            ).fetchone()
            if existing:
                rid, existing_fn = existing
                if pdf_filename and not existing_fn:
                    con.execute("UPDATE receipts SET filename=? WHERE id=?", (pdf_filename, rid))
                con.execute(
                    "INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id) VALUES (?, ?)",
                    (rid, tx_id),
                )
                continue
            cur = con.execute(
                "INSERT INTO receipts "
                "(filename, receipt_date, merchant, invoice_number, amount, transaction_id, saved_at) "
                "VALUES (?, ?, ?, NULL, ?, ?, ?)",
                (pdf_filename, date_str, _MERCHANT, int(amount), tx_id, r["fetched_at"]),
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


def _link_vpass_vidiq() -> int:
    """VPASS の「PAYPAL *VIDIQ」「VIDIQ」 行に vidIQ plugin の receipt を流用 link。"""
    from src.db import connect as _connect_central
    con = _connect_central(DB_PATH)
    before = con.execute("SELECT COUNT(*) FROM receipt_links").fetchone()[0]
    con.execute("""
        WITH match AS (
            SELECT v.id AS v_tx_id, r.id AS receipt_id
            FROM transactions v
            JOIN transactions x
              ON x.bank = 'vidIQ'
             AND x.debit = v.debit
             AND ABS(julianday(replace(v.date,'/','-')) - julianday(replace(x.date,'/','-'))) <= 14
            JOIN receipts r ON r.transaction_id = x.id AND r.merchant = 'vidIQ Inc.'
            WHERE v.bank = 'VPASS'
              AND (v.description LIKE '%VIDIQ%' OR v.description LIKE '%vidIQ%')
        )
        INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id)
        SELECT receipt_id, v_tx_id FROM match
    """)
    after = con.execute("SELECT COUNT(*) FROM receipt_links").fetchone()[0]
    con.commit()
    con.close()
    n = after - before
    if n > 0:
        print(f"  VPASS VIDIQ link: +{n} 件")
    return n


async def run(*, headless: bool = True, dry_run: bool = False) -> list[dict]:
    rows: list[dict] = []
    async with async_playwright() as p:
        context = await _launch_context(p, headless)
        page = await context.new_page()
        try:
            await login(page)
            if not _is_logged_in(page.url):
                print("[vidiq] ログイン失敗")
                return []
            links = await _find_invoice_links(page)
            print(f"[vidiq] invoice/receipt リンク: {len(links)} 件")
            for link in links[:5]:
                print(f"  - {link['text'][:60]} ({link.get('href','')[:60]})")
            if not links:
                return []
            pdf_paths = await _save_invoice_pdfs(page, links)
            print(f"[vidiq] PDF 保存: {len(pdf_paths)} 件")
            for pdf in pdf_paths:
                parsed = _parse_invoice_pdf(pdf)
                if parsed and parsed.get("date") and parsed.get("amount"):
                    rows.append({
                        **parsed,
                        "pdf_filename": pdf.relative_to(INVOICE_DIR.parent).as_posix(),
                        "fetched_at": datetime.now().isoformat(),
                    })
        finally:
            await context.close()

    if dry_run:
        print(f"[vidiq] dry_run: {len(rows)} 件")
        for r in rows:
            print(f"  {r}")
        return rows

    n = _save_to_db(rows)
    linked = _link_vpass_vidiq()
    shop_link = 0
    try:
        from src.matching import run_shop_matching
        matches = run_shop_matching()
        shop_link = sum(1 for m in matches if m.shop_bank == "vidIQ")
    except Exception as e:
        print(f"  shop_matching 失敗: {e}")
    try:
        from src.server import _invalidate_tx_cache
        _invalidate_tx_cache()
    except Exception:
        pass
    print(f"[vidiq] {len(rows)} 件取得 / DB +{n} / VPASS link +{linked} / shop_card {shop_link}")
    return rows


if __name__ == "__main__":
    asyncio.run(run(headless=False, dry_run=True))
