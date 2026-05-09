"""
CAMPFIRE 支援履歴スクレイパー。

メール/パスワードでログイン。支援したプロジェクト一覧を取得して SQLite に保存。
領収書 PDF を invoices/campfire/ に保存。
セッションは .browser_data/campfire に保持。
"""
import asyncio
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, BrowserContext, Page

load_dotenv()

LOGIN_URL   = "https://camp-fire.jp/login"
HISTORY_URL = "https://camp-fire.jp/mypage/backers"

DB_PATH = Path(__file__).parent.parent.parent / "transactions.db"
_BROWSER_DATA_DIR = Path(__file__).parent.parent.parent / ".browser_data" / "campfire"
_INVOICE_DIR = Path(__file__).parent.parent.parent / "invoices" / "campfire"


async def _launch_context(p, headless: bool) -> BrowserContext:
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
        locale="ja-JP",
        accept_downloads=True,
    )


def _needs_login(url: str) -> bool:
    return "camp-fire.jp/login" in url or "camp-fire.jp/sign" in url or "/login/" in url


# ─────────────────────────────────────────────
# ログイン
# ─────────────────────────────────────────────

async def login(page: Page) -> None:
    email = os.environ.get("CAMPFIRE_EMAIL", "")
    pw    = os.environ.get("CAMPFIRE_PW", "")
    if not email or not pw:
        raise RuntimeError("CAMPFIRE_EMAIL / CAMPFIRE_PW が .env に設定されていません")

    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await asyncio.sleep(3)

    # Step 1: メールアドレス入力
    await page.wait_for_selector("input[name='user[email]']", timeout=15_000)
    await page.fill("input[name='user[email]']", email)
    await page.click("input[name='commit'], input[type='submit'][name='commit']")

    # Step 2: パスワード入力（次へ後に表示される）
    await page.wait_for_selector("input[type='password']", timeout=15_000)
    await page.fill("input[type='password']", pw)
    await page.click("input[name='commit'], input[type='submit'][name='commit'], button[type='submit']:has-text('ログイン')")
    await page.wait_for_load_state("domcontentloaded")
    await asyncio.sleep(2)
    print("[campfire] ログイン完了")


# ─────────────────────────────────────────────
# DB 保存・マイグレーション
# ─────────────────────────────────────────────

def _save(txs: list[dict]) -> int:
    if not txs:
        return 0
    from src.db import connect as _connect_central
    con = _connect_central(DB_PATH)
    saved = 0
    for t in txs:
        # 旧形式（backer_id なし）のレコードがあれば更新する
        backer_id = t.get("backer_id", "")
        new_desc = t["description"]
        if backer_id:
            old_desc = re.sub(r"^\[\d+\] ", "", new_desc)
            # 旧形式レコードを新形式に更新
            con.execute(
                "UPDATE transactions SET description=? WHERE bank='CAMPFIRE' AND date=? AND description=? AND debit=?",
                (new_desc, t["date"], old_desc, t.get("debit", 0)),
            )
        from src.server.categorize import normalize_desc
        cur = con.execute(
            "INSERT OR IGNORE INTO transactions (bank, date, description, description_normalized, debit, fetched_at) VALUES (?,?,?,?,?,?)",
            (t["bank"], t["date"], new_desc, normalize_desc(new_desc),
             t.get("debit", 0), datetime.now().isoformat()),
        )
        saved += cur.rowcount
    con.commit()
    con.close()
    return saved


# ─────────────────────────────────────────────
# 領収書 PDF ダウンロード
# ─────────────────────────────────────────────

async def _download_invoice(page: Page, backer_id: str, date_str: str) -> bool:
    """
    /mypage/backers/{backer_id} の詳細ページを PDF 化（支援金額証憑）し、
    さらにシステム利用料の公式領収書も保存する。
    いずれか新規保存があれば True を返す。
    """
    _INVOICE_DIR.mkdir(parents=True, exist_ok=True)
    date_part = date_str.replace("/", "-")
    detail_pdf_path = _INVOICE_DIR / f"{date_part}_{backer_id}_detail.pdf"
    fee_pdf_path    = _INVOICE_DIR / f"{date_part}_{backer_id}_fee.pdf"
    saved_any = False

    detail_url = f"https://camp-fire.jp/mypage/backers/{backer_id}"
    await page.goto(detail_url, wait_until="domcontentloaded")
    await asyncio.sleep(1)

    # ① 詳細ページをそのまま PDF 化（支援金額の証憑）
    if not detail_pdf_path.exists():
        try:
            await page.pdf(path=str(detail_pdf_path), format="A4", print_background=True)
            print(f"  [campfire] 支援詳細PDF保存: {detail_pdf_path.name}")
            saved_any = True
        except Exception as e:
            print(f"  [campfire] 支援詳細PDF失敗 backer={backer_id}: {e}")

    # ② システム利用料の公式領収書（/mypage/invoices/{id}）を取得
    if not fee_pdf_path.exists():
        invoice_link = await page.eval_on_selector_all(
            "a[href*='/mypage/invoices/']",
            "els => els.map(e => e.href)"
        )
        if not invoice_link:
            return saved_any
        m = re.search(r"/mypage/invoices/(\d+)", invoice_link[0])
        if not m:
            return saved_any
        invoice_id = m.group(1)

        # ブラウザコンテキストのセッション（Cookie）を使って直接 GET
        try:
            download_url = f"https://camp-fire.jp/mypage/invoices/{invoice_id}"
            response = await page.context.request.get(download_url)
            if not response.ok:
                print(f"  [campfire] システム利用料領収書 HTTP {response.status} backer={backer_id}")
                return saved_any
            body = await response.body()
            if len(body) >= 100:
                with open(fee_pdf_path, "wb") as f:
                    f.write(body)
                print(f"  [campfire] システム利用料領収書保存: {fee_pdf_path.name}")
                saved_any = True
        except Exception as e:
            print(f"  [campfire] システム利用料領収書DL失敗 backer={backer_id}: {e}")

    return saved_any


# ─────────────────────────────────────────────
# 支援履歴取得
# ─────────────────────────────────────────────

async def _fetch_history(page: Page) -> list[dict]:
    txs: list[dict] = []
    await page.goto(HISTORY_URL, wait_until="domcontentloaded")
    await asyncio.sleep(3)

    if _needs_login(page.url):
        await login(page)
        await page.goto(HISTORY_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)

    try:
        await page.wait_for_selector(".box", timeout=15_000)
    except Exception:
        print("[campfire] 支援履歴要素が見つかりません")
        await page.screenshot(path="/tmp/campfire_debug.png")
        return txs

    items = await page.query_selector_all(".box")
    for item in items:
        text = (await item.inner_text()).strip()
        if not text:
            continue
        date_m = re.search(r"支援日[：:]\s*(\d{4})/(\d{2})/(\d{2})", text)
        if not date_m:
            continue
        date_str = f"{date_m.group(1)}/{date_m.group(2)}/{date_m.group(3)}"
        amount_m = re.search(r"支援金額[：:]\s*([\d,]+)円", text)
        if not amount_m:
            continue
        amount = int(amount_m.group(1).replace(",", ""))
        desc_base = text.splitlines()[0].strip()[:60]

        # backer_id を取得
        inner_links = await item.eval_on_selector_all("a", "els => els.map(e => e.href)")
        backer_link = next(
            (l for l in inner_links if "/mypage/backers/" in l and l.count("/") > 4), None
        )
        backer_id = backer_link.split("/mypage/backers/")[-1].rstrip("/") if backer_link else ""

        # description に backer_id を含める
        desc = f"[{backer_id}] {desc_base}" if backer_id else desc_base
        txs.append({
            "bank": "CAMPFIRE",
            "date": date_str,
            "description": desc,
            "debit": amount,
            "backer_id": backer_id,
        })

    # 領収書 PDF をダウンロード（ページ遷移を伴うので履歴取得後に実行）
    for tx in txs:
        if tx.get("backer_id"):
            await _download_invoice(page, tx["backer_id"], tx["date"])

    return txs


# ─────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────

async def run(headless: bool = True) -> list[dict]:
    _BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        ctx = await _launch_context(p, headless)
        page = await ctx.new_page()
        txs = await _fetch_history(page)
        await ctx.close()
    saved = _save(txs)
    print(f"[campfire] {saved}/{len(txs)} 件保存")
    from src.scrape_state import mark_scrape_done
    mark_scrape_done("campfire", count=len(txs), extra={"saved": saved})
    return txs


if __name__ == "__main__":
    asyncio.run(run(headless=False))
