"""
AliExpress 注文履歴スクレイパー。

メール / パスワードでログイン。注文履歴ページから各注文の合計額・
商品タイトル・注文 ID を取得して SQLite に保存。bank='AliExpress'。

PayPal 同様 anti-bot が厳しいので:
- Mac の実 Google Chrome バイナリを優先利用
- 永続セッション無し / セッション切れは headed フォールバック（最大10分待機）
- セッションは .browser_data/aliexpress に保存
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, BrowserContext, Page

load_dotenv()

# 日本ユーザは ja.aliexpress.com にリダイレクトされることが多い。
# ja → グローバルどちらでも到達可能なよう汎用 URL を使う。
LOGIN_URL    = "https://login.aliexpress.com/"
HISTORY_URL  = "https://www.aliexpress.com/p/order/index.html"

DB_PATH = Path(__file__).parent.parent.parent / "transactions.db"
_BROWSER_DATA_DIR = Path(__file__).parent.parent.parent / ".browser_data" / "aliexpress"
_INVOICE_DIR = Path(__file__).parent.parent.parent / "invoices" / "aliexpress"


async def _launch_context(p, headless: bool) -> BrowserContext:
    """実 Chrome バイナリ優先 + stealth 設定。"""
    chrome_bin = os.environ.get("CHROME_BIN", "")
    if not chrome_bin:
        for candidate in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
        ):
            if Path(candidate).exists():
                chrome_bin = candidate
                break

    launch_kwargs = dict(
        user_data_dir=str(_BROWSER_DATA_DIR),
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
        ignore_default_args=["--enable-automation"],
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
        locale="ja-JP",
        timezone_id="Asia/Tokyo",
    )
    if chrome_bin:
        launch_kwargs["executable_path"] = chrome_bin
        print(f"[aliexpress] Chrome バイナリ: {chrome_bin}")
    ctx = await p.chromium.launch_persistent_context(**launch_kwargs)
    await ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['ja-JP', 'ja', 'en-US', 'en'] });
        window.chrome = { runtime: {} };
    """)
    return ctx


def _on_orders_page(url: str) -> bool:
    return "/p/order/" in url or "/order/index" in url


def _needs_login(url: str) -> bool:
    return ("login.aliexpress.com" in url
            or "passport.aliexpress.com" in url
            or "/login" in url)


# ─────────────────────────────────────────────
# ログイン
# ─────────────────────────────────────────────

async def login(page: Page) -> None:
    """ID/PW を試みるが、キャプチャ等で阻まれた場合も例外にせず
    最終的に注文ページに到達したら成功と見なす（最大 10 分）。"""
    email = os.environ.get("ALIEXPRESS_EMAIL", "")
    pw    = os.environ.get("ALIEXPRESS_PW", "")
    if not email or not pw:
        raise RuntimeError("ALIEXPRESS_EMAIL / ALIEXPRESS_PW が .env に設定されていません")

    if "login.aliexpress.com" not in page.url and "passport." not in page.url:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
    await asyncio.sleep(2)

    # メールフィールド: id="fm-login-id" など揺れあり
    try:
        await page.wait_for_selector(
            "input[id*='login'], input[name='loginId'], input[name='account'], input[type='email']",
            timeout=8_000,
        )
        await page.fill(
            "input[id*='login'], input[name='loginId'], input[name='account'], input[type='email']",
            email,
        )
        await asyncio.sleep(1)
    except Exception:
        print("[aliexpress] メールフィールド未検出（手動でログインしてください）")

    # パスワードフィールド
    try:
        await page.wait_for_selector("input[type='password']", timeout=8_000)
        await page.fill("input[type='password']", pw)
        await asyncio.sleep(1)
        for btn in ("button[type='submit']", "button:has-text('ログイン')",
                    "button:has-text('Sign in')", "button[id*='submit']"):
            try:
                await page.click(btn, timeout=2_000)
                break
            except Exception:
                continue
    except Exception:
        print("[aliexpress] パスワードフィールド未検出（キャプチャ等の可能性）")

    print("[aliexpress] ブラウザで残りの認証を完了してください（最大10分待機）...")
    try:
        await page.wait_for_function(
            """() => {
                const url = location.href;
                return !url.includes('login.aliexpress.com')
                    && !url.includes('passport.aliexpress.com')
                    && !url.includes('/login');
            }""",
            timeout=600_000,
        )
        print(f"[aliexpress] ログイン完了: {page.url[:90]}")
    except Exception:
        print(f"[aliexpress] タイムアウト: {page.url[:90]}")


# ─────────────────────────────────────────────
# 注文履歴取得
# ─────────────────────────────────────────────

_MONTH_MAP = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
    "January": 1, "February": 2, "March": 3, "April": 4,
    "June": 6, "July": 7, "August": 8, "September": 9,
    "October": 10, "November": 11, "December": 12,
}


def _parse_aliexpress_date(text: str) -> str | None:
    """AliExpress の日付表記を YYYY/MM/DD に正規化。
    対応: 'Feb 6, 2026' / 'February 6, 2026' / '2026年2月6日' / '2026/02/06'
    """
    # 英語月名 + 日 + 年
    m = re.search(r"\b([A-Z][a-z]{2,8})\s+(\d{1,2}),\s*(\d{4})", text)
    if m and m.group(1) in _MONTH_MAP:
        mo, d, y = _MONTH_MAP[m.group(1)], int(m.group(2)), int(m.group(3))
        return f"{y}/{mo:02d}/{d:02d}"
    # 日本語年月日
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if m:
        return f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"
    # スラッシュ・ハイフン
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if m:
        return f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"
    return None


async def fetch_orders(page: Page) -> list[dict]:
    r"""注文履歴ページから各注文を取得。複数ページを次へボタンで巡回。

    DOM:
        .order-item                       ← 1 注文 1 つ
          .order-item-header-status-text  ← Completed / Processing
          /Order date: Feb 6, 2026/       ← 注文日
          /Order ID: \d+/                 ← 注文 ID
          .order-item-store-name a span   ← ストア名
          .order-item-content-info-name   ← 商品タイトル（あれば）
          /Total:[¥￥]?[\d,]+円?/         ← 合計
    """
    await page.goto(HISTORY_URL, wait_until="domcontentloaded", timeout=60_000)
    await asyncio.sleep(5)

    all_orders: list[dict] = []
    seen_ids: set[str] = set()

    # AliExpress は「.order-more」(View orders) クリックで次バッチを読み込む。
    # クリック → 待機 → 件数増えなくなるまでループ（最大 200 回 ≒ 2,000 件想定）
    last_count = 0
    stable_rounds = 0
    for round_idx in range(200):
        cur = await page.evaluate("() => document.querySelectorAll('.order-item').length")
        if cur == last_count:
            stable_rounds += 1
            if stable_rounds >= 3:
                break  # 3 回連続で増えず → 末尾到達
        else:
            stable_rounds = 0
            last_count = cur
        # ページ末尾にスクロールしてから「View orders」ボタンを探す
        await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.5)
        clicked = False
        for sel in (".order-more", "[class*='order-more']",
                    "button:has-text('View orders')", "button:has-text('もっと見る')"):
            try:
                if await page.locator(sel).count():
                    await page.click(sel, timeout=2_000)
                    clicked = True
                    await asyncio.sleep(2.5)
                    break
            except Exception:
                continue
        if not clicked and cur == last_count:
            # ボタンも無いなら末尾
            stable_rounds += 2
        if round_idx % 5 == 0:
            print(f"[aliexpress] load round {round_idx}: {cur} cards")

    # 単一ページ（全件読込済み）から抽出
    for page_idx in range(1):
        # トップレベルの .order-item のみを抽出（クラス完全一致）
        cards = await page.evaluate("""
            () => {
                // .order-item-header 等の派生クラスは除外、純粋に "order-item" のみ
                const all = document.querySelectorAll('.order-item');
                const out = [];
                all.forEach(c => {
                    if (c.className.split(/\\s+/).includes('order-item')) {
                        out.push({
                            text: c.innerText || '',
                            // ストア名と商品タイトルだけは個別 selector で取る
                            store: (c.querySelector('.order-item-store-name a span')?.innerText || '').trim(),
                            title: (c.querySelector('.order-item-content-info-name, [class*="content-info-name"], [class*="product-name"]')?.innerText || '').trim(),
                            total: (() => {
                                const el = c.querySelector('[class*="total"], [class*="Total"]');
                                return (el?.innerText || '').trim();
                            })(),
                        });
                    }
                });
                return out;
            }
        """) or []

        new_count = 0
        for card in cards:
            text = card.get("text", "")
            id_m = re.search(r"Order ID[:：]?\s*(\d{10,})", text) or re.search(r"(\d{16,})", text)
            if not id_m:
                continue
            oid = id_m.group(1)
            if oid in seen_ids:
                continue

            # 日付: "Order date: Feb 6, 2026" を優先
            date_str = None
            date_m = re.search(r"Order date[:：]?\s*([^\n]+)", text)
            if date_m:
                date_str = _parse_aliexpress_date(date_m.group(1))
            if not date_str:
                date_str = _parse_aliexpress_date(text)
            if not date_str:
                date_str = datetime.now().strftime("%Y/%m/%d")

            # 合計: "Total:10,451円" 形式を最優先
            amt_m = re.search(r"Total[:：]?\s*[¥￥]?\s*([\d,]+)\s*円?", text)
            if not amt_m:
                # フォールバック: 商品単価行 (例: "10,451円   x1") の最大金額
                yen_amounts = [int(m.replace(",", "")) for m in re.findall(r"([\d,]+)\s*円", text)]
                if not yen_amounts:
                    continue
                amt = max(yen_amounts)
            else:
                amt = int(amt_m.group(1).replace(",", ""))

            seen_ids.add(oid)
            new_count += 1

            # タイトル: 個別 selector → なければストア名 → 最後のフォールバック
            title = card.get("title") or card.get("store") or "AliExpress注文"
            desc = f"[{oid}] {title}"[:250]
            all_orders.append({
                "bank": "AliExpress",
                "date": date_str,
                "description": desc,
                "debit": amt,
                "credit": 0,
            })

        print(f"[aliexpress] cards={len(cards)} extracted={new_count}")

    print(f"[aliexpress] 取得: {len(all_orders)} 件")
    return all_orders


# ─────────────────────────────────────────────
# DB 保存
# ─────────────────────────────────────────────

def _save(orders: list[dict]) -> int:
    if not orders:
        return 0
    from src.db import connect as _connect_central
    con = _connect_central(DB_PATH)
    saved = 0
    from src.server.categorize import normalize_desc
    for o in orders:
        cur = con.execute(
            "INSERT OR IGNORE INTO transactions "
            "(bank, date, description, description_normalized, debit, credit, fetched_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (o["bank"], o["date"], o["description"],
             normalize_desc(o["description"]),
             o.get("debit", 0), o.get("credit", 0),
             datetime.now().isoformat()),
        )
        saved += cur.rowcount
    con.commit()
    con.close()
    return saved


# ─────────────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────────────

async def download_invoices(page: Page, orders: list[dict]) -> int:
    """各注文の詳細ページを PDF 化して invoices/aliexpress/ に保存。
    ファイル名は {YYYY-MM-DD}_{order_id}.pdf。既存はスキップ（idempotent）。
    """
    _INVOICE_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0
    for o in orders:
        m = re.match(r"\[(\d+)\]", o["description"])
        if not m:
            continue
        oid = m.group(1)
        date_part = o["date"].replace("/", "-")
        pdf_path = _INVOICE_DIR / f"{date_part}_{oid}.pdf"
        if pdf_path.exists():
            continue
        url = f"https://www.aliexpress.com/p/order/detail.html?orderId={oid}"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            await asyncio.sleep(2)
            await page.pdf(path=str(pdf_path), format="A4", print_background=True)
            print(f"  [aliexpress] PDF保存: {pdf_path.name}")
            saved += 1
        except Exception as e:
            print(f"  [aliexpress] PDF失敗 {oid}: {e}")
        await asyncio.sleep(1)
    print(f"[aliexpress] PDF合計: {saved} 件保存")
    return saved


async def run(headless: bool = True, download_pdf: bool = True) -> list[dict]:
    _BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    has_session = any(_BROWSER_DATA_DIR.glob("Default/Cookies"))
    use_headless = headless and has_session

    async with async_playwright() as p:
        ctx = await _launch_context(p, use_headless)
        page = await ctx.new_page()

        await page.goto(HISTORY_URL, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(3)

        if _needs_login(page.url):
            if use_headless:
                await ctx.close()
                print("[aliexpress] セッション切れ。headed ブラウザでログインします...")
                ctx = await _launch_context(p, headless=False)
                page = await ctx.new_page()
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
                await asyncio.sleep(3)
            await login(page)

        orders = await fetch_orders(page)
        if download_pdf and orders:
            await download_invoices(page, orders)
        await ctx.close()

    saved = _save(orders)
    print(f"[aliexpress] DB保存: {saved} 件追加")
    return orders


if __name__ == "__main__":
    import sys
    asyncio.run(run(headless="--headless" in sys.argv))
