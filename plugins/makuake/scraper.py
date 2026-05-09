"""
Makuake 応援購入履歴スクレイパー。

メール/パスワードでログイン。応援購入したプロジェクト一覧を取得して SQLite に保存。
セッションは .browser_data/makuake に保持。
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

LOGIN_URL   = "https://auth.makuake.com/login"
HISTORY_URL = "https://www.makuake.com/my/project/favorite/"

DB_PATH = Path(__file__).parent.parent.parent / "transactions.db"
_BROWSER_DATA_DIR = Path(__file__).parent.parent.parent / ".browser_data" / "makuake"


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
    )


def _needs_login(url: str) -> bool:
    return "auth.makuake.com" in url


# ─────────────────────────────────────────────
# ログイン
# ─────────────────────────────────────────────

async def login(page: Page) -> None:
    email = os.environ.get("MAKUAKE_EMAIL", "")
    pw    = os.environ.get("MAKUAKE_PW", "")
    if not email or not pw:
        raise RuntimeError("MAKUAKE_EMAIL / MAKUAKE_PW が .env に設定されていません")

    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await asyncio.sleep(3)

    await page.wait_for_selector("input#email", timeout=15_000)
    await page.fill("input#email", email)
    await page.fill("input#password", pw)
    await page.click("button[type='submit'], button:has-text('ログイン')")
    await asyncio.sleep(3)
    print("[makuake] ログイン完了")


# ─────────────────────────────────────────────
# DB 保存
# ─────────────────────────────────────────────

def _save(txs: list[dict]) -> int:
    if not txs:
        return 0
    from src.db import connect as _connect_central
    con = _connect_central(DB_PATH)
    saved = 0
    from src.server.categorize import normalize_desc
    for t in txs:
        cur = con.execute(
            "INSERT OR IGNORE INTO transactions (bank, date, description, description_normalized, debit, fetched_at) VALUES (?,?,?,?,?,?)",
            (t["bank"], t["date"], t["description"],
             normalize_desc(t["description"]),
             t.get("debit", 0), datetime.now().isoformat()),
        )
        saved += cur.rowcount
    con.commit()
    con.close()
    return saved


# ─────────────────────────────────────────────
# 応援購入履歴取得
# ─────────────────────────────────────────────

async def _fetch_history(page: Page) -> list[dict]:
    txs: list[dict] = []
    await page.goto(HISTORY_URL, wait_until="domcontentloaded")
    await asyncio.sleep(3)

    if _needs_login(page.url):
        await login(page)
        await page.goto(HISTORY_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)

    # 「すべて見る」ボタンを全て押して詳細を展開
    try:
        btns = await page.query_selector_all("text=すべて見る")
        for btn in btns:
            await btn.click()
            await asyncio.sleep(0.5)
    except Exception:
        pass

    # 各プロジェクトカード（article.investment-item）を処理
    articles = await page.query_selector_all("article")
    if not articles:
        print("[makuake] 応援購入履歴要素が見つかりません")
        await page.screenshot(path="/tmp/makuake_debug.png")
        return txs

    for article in articles:
        text = (await article.inner_text()).strip()
        if not text:
            continue
        date_m = re.search(r"応援購入日\n(\d{4}/\d{2}/\d{2})", text)
        if not date_m:
            continue
        date_str = date_m.group(1)
        amount_m = re.search(r"支払い総額\(税込\)\t([\d,]+)円", text)
        if not amount_m:
            amount_m = re.search(r"支払い総額\(税込\)\s*([\d,]+)円", text)
        if not amount_m:
            continue
        amount = int(amount_m.group(1).replace(",", ""))
        desc = text.splitlines()[0].strip()[:60]
        txs.append({"bank": "Makuake", "date": date_str, "description": desc, "debit": amount})

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
    print(f"[makuake] {saved}/{len(txs)} 件保存")
    from src.scrape_state import mark_scrape_done
    mark_scrape_done("makuake", count=len(txs), extra={"saved": saved})
    return txs


if __name__ == "__main__":
    asyncio.run(run(headless=False))
