"""
メルカリ取引履歴スクレイパー。

購入履歴・売上履歴を取得して SQLite に保存する。
初回・パスキー認証が必要な時は headed ブラウザを使う。
セッションは .browser_data/mercari に保持。
"""
import asyncio
import os
import re
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, BrowserContext, Page

load_dotenv()

LOGIN_URL    = "https://jp.mercari.com/login"
PURCHASES_URL = "https://jp.mercari.com/mypage/purchases"
SALES_URL    = "https://jp.mercari.com/mypage/listings?status=sold_out"
BASE_URL     = "https://jp.mercari.com"

DB_PATH = Path(__file__).parent.parent.parent / "transactions.db"
_BROWSER_DATA_DIR = Path(__file__).parent.parent.parent / ".browser_data" / "mercari"
_INVOICE_DIR = Path(__file__).parent.parent.parent / "invoices" / "mercari"


# ─────────────────────────────────────────────
# ブラウザコンテキスト
# ─────────────────────────────────────────────

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
    return "login.jp.mercari.com" in url or "/login" in url or "/signup" in url


# ─────────────────────────────────────────────
# ログイン（パスキー対応: メールアドレスを入力後、ユーザーがパスキー認証）
# ─────────────────────────────────────────────

async def login(page: Page) -> None:
    if not _needs_login(page.url):
        return

    email = os.environ.get("MERCARI_EMAIL", "")
    if not email:
        raise RuntimeError("MERCARI_EMAIL が .env に設定されていません")

    # メールアドレス入力
    try:
        await page.wait_for_selector(
            "input[data-testid='emailOrPhone'], input[name='emailOrPhone']",
            timeout=10_000,
        )
        await page.fill(
            "input[data-testid='emailOrPhone'], input[name='emailOrPhone']",
            email,
        )
        for btn in ["button[type='submit']", "button:has-text('次へ')"]:
            try:
                await page.click(btn, timeout=3_000)
                break
            except Exception:
                continue
        await page.wait_for_load_state("load")
        await asyncio.sleep(1)
    except Exception:
        pass

    # パスワード（パスキーでなければ入力）
    try:
        pw = os.environ.get("MERCARI_PW", "")
        if pw:
            await page.wait_for_selector("input[type='password']", timeout=5_000)
            await page.fill("input[type='password']", pw)
            for btn in ["button[type='submit']", "button:has-text('ログイン')"]:
                try:
                    await page.click(btn, timeout=3_000)
                    break
                except Exception:
                    continue
            await page.wait_for_load_state("load")
            await asyncio.sleep(1)
    except Exception:
        pass

    # パスキー / SMS 認証などは手動完了を待つ
    if _needs_login(page.url):
        print("Mercari: パスキー / SMS 認証をブラウザで完了してください（最大10分）...")
        _notify("Mercari 認証が必要です。ブラウザで完了してください。")
        try:
            await page.wait_for_function(
                "() => !window.location.hostname.includes('login.jp.mercari.com')",
                timeout=600_000,
            )
            await asyncio.sleep(2)
        except Exception:
            pass

    print(f"ログイン後URL: {page.url}")


# ─────────────────────────────────────────────
# 購入一覧: トランザクション ID・日付・タイトルを収集
# ─────────────────────────────────────────────

async def _collect_purchase_entries(page: Page) -> list[dict]:
    """購入一覧ページをスクロールして全エントリを収集する"""
    await page.goto(PURCHASES_URL, wait_until="load", timeout=60_000)
    await asyncio.sleep(2)

    entries: list[dict] = []
    seen: set[str] = set()

    while True:
        lis = await page.query_selector_all("li")
        for li in lis:
            a = await li.query_selector("a[href*='/transaction/'], a[href*='mercari-shops.com']")
            if not a:
                continue
            href = await a.get_attribute("href") or ""
            if not href or href in seen:
                continue
            seen.add(href)

            raw_text = (await li.inner_text()).strip()
            title, date_str = _parse_list_text(raw_text)
            entries.append({"href": href, "title": title, "date_str": date_str})

        # スクロールして追加ロード
        prev_h = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.5)
        new_h = await page.evaluate("document.body.scrollHeight")
        if new_h <= prev_h:
            break

    return entries


def _parse_list_text(raw: str) -> tuple[str, str]:
    """一覧テキストからタイトルと日付文字列を取り出す"""
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    date_str = ""
    title = lines[0] if lines else ""
    for line in lines:
        m = re.search(r"(\d{4}/\d{2}/\d{2})", line)
        if m:
            date_str = m.group(1)
            break
    return title, date_str


def _extract_price_from_text(text: str) -> int | None:
    """テキストから最初に見つかる ¥ 金額を返す（¥と数字が改行で分かれていても対応）"""
    m = re.search(r"[¥￥]\s*([\d,]+)", text)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


# ─────────────────────────────────────────────
# 取引詳細: 価格を取得
# ─────────────────────────────────────────────

async def _fetch_transaction_price(page: Page, href: str) -> int | None:
    """取引詳細ページを開き支払い金額を返す"""
    url = href if href.startswith("http") else BASE_URL + href
    is_shops = "mercari-shops.com" in url
    try:
        await page.goto(url, wait_until="load", timeout=30_000)
        try:
            if is_shops:
                await page.wait_for_selector(
                    "[class*='price'], [class*='total'], [class*='amount'], dl, table",
                    timeout=8_000,
                )
            else:
                await page.wait_for_selector(
                    "[data-testid='transaction:information-for-buyer'], [data-testid='item-price']",
                    timeout=8_000,
                )
        except Exception:
            await asyncio.sleep(2)
    except Exception:
        return None

    # 通常メルカリ: data-testid="transaction:information-for-buyer"
    if not is_shops:
        el = await page.query_selector("[data-testid='transaction:information-for-buyer']")
        if el:
            text = await el.inner_text()
            m = re.search(r"商品代金\s*[¥￥]([\d,]+)", text)
            if m:
                return int(m.group(1).replace(",", ""))
            m = re.search(r"[¥￥]([\d,]+)", text)
            if m:
                return int(m.group(1).replace(",", ""))
        return None

    # メルカリShops: ページ全体のテキストから注文金額を探す
    try:
        body_text = await page.inner_text("body")
    except Exception:
        return None
    for pattern in [
        r"(?:合計|注文金額|お支払い金額|請求金額|商品金額|小計)\s*[¥￥]([\d,]+)",
        r"[¥￥]\s*([\d,]+)",
    ]:
        m = re.search(pattern, body_text)
        if m:
            amount = int(m.group(1).replace(",", ""))
            if amount > 0:
                return amount
    return None


# ─────────────────────────────────────────────
# 売上一覧
# ─────────────────────────────────────────────

async def _collect_sale_entries(page: Page) -> list[dict]:
    """売上済み一覧を収集する"""
    await page.goto(SALES_URL, wait_until="load", timeout=60_000)
    await asyncio.sleep(2)

    entries: list[dict] = []
    seen: set[str] = set()

    while True:
        # 売上は /transaction/ または /item/ リンク
        lis = await page.query_selector_all("li")
        for li in lis:
            a = await li.query_selector("a[href*='/transaction/'], a[href*='/item/']")
            if not a:
                continue
            href = await a.get_attribute("href") or ""
            if not href or href in seen:
                continue
            seen.add(href)

            raw_text = (await li.inner_text()).strip()
            title, date_str = _parse_list_text(raw_text)
            # /transaction/ は成約確定リンクなのでリスト価格を信頼できる。
            # /item/ は下書き・出品中も混入するため価格を取らず詳細ページで確認する。
            price = _extract_price_from_text(raw_text) if "/transaction/" in href else None
            entries.append({"href": href, "title": title, "date_str": date_str, "price": price})

        prev_h = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.5)
        new_h = await page.evaluate("document.body.scrollHeight")
        if new_h <= prev_h:
            break

    return entries


async def _fetch_sale_price(page: Page, href: str) -> int | None:
    """売上取引詳細から売上金額（手数料控除後）を返す"""
    # 売上一覧は /item/mXXX リンクだが、取引情報は /transaction/mXXX にある
    if "/item/" in href:
        href = href.replace("/item/", "/transaction/")
    url = href if href.startswith("http") else BASE_URL + href
    try:
        await page.goto(url, wait_until="load", timeout=30_000)
        try:
            await page.wait_for_selector(
                "[data-testid='transaction:information-for-seller'], [data-testid='transaction:information-for-buyer']",
                timeout=8_000,
            )
        except Exception:
            await asyncio.sleep(2)
    except Exception:
        return None

    page_text = await page.evaluate("() => document.body.innerText")
    if "下書きがまだ残っています" in page_text:
        return None

    # 売上側の情報ブロック
    for testid in ["transaction:information-for-seller", "transaction:information-for-buyer"]:
        el = await page.query_selector(f"[data-testid='{testid}']")
        if el:
            text = await el.inner_text()
            # 「販売利益\n¥1,799」など — 完了済みのみ取得（フォールバック不可）
            m = re.search(r"販売利益\s*[¥￥]([\d,]+)", text)
            if m:
                return int(m.group(1).replace(",", ""))

    return None


# ─────────────────────────────────────────────
# PDF 保存
# ─────────────────────────────────────────────

def _pdf_path(category: str, tx_id: str, date_str: str) -> Path:
    year = date_str[:4] if date_str else datetime.now().strftime("%Y")
    date_part = date_str.replace("/", "-") if date_str else datetime.now().strftime("%Y-%m-%d")
    return _INVOICE_DIR / category / year / f"{date_part}_{tx_id}.pdf"


async def _save_pdf(page: Page, href: str, category: str, tx_id: str, date_str: str) -> bool:
    path = _pdf_path(category, tx_id, date_str)
    if path.exists():
        return False
    url = href if href.startswith("http") else BASE_URL + href
    try:
        await page.goto(url, wait_until="load", timeout=30_000)
        await asyncio.sleep(2)
        page_text = await page.evaluate("() => document.body.innerText")
        if "下書きがまだ残っています" in page_text:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.pdf(path=str(path), format="A4", print_background=True)
        return True
    except Exception as e:
        print(f"  PDF保存スキップ [{tx_id}]: {e}")
        return False


# ─────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────

def _db_connect() -> sqlite3.Connection:
    from src.db import connect as _connect_central
    return _connect_central(DB_PATH)


def _already_saved(con: sqlite3.Connection, bank: str, description: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM transactions WHERE bank=? AND description LIKE ?",
        (bank, f"%{description[:30]}%"),
    ).fetchone()
    return row is not None


def _save_tx(con: sqlite3.Connection, bank: str, tx: dict) -> bool:
    from src.server.categorize import normalize_desc
    try:
        con.execute(
            "INSERT OR IGNORE INTO transactions "
            "(bank, date, description, description_normalized, debit, credit, balance, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (bank, tx["date"], tx["description"],
             normalize_desc(tx["description"]),
             int(tx.get("debit") or 0), int(tx.get("credit") or 0),
             0, tx["fetched_at"]),
        )
        return con.total_changes > 0
    except Exception as e:
        print(f"  DB保存スキップ: {e}")
        return False


def _notify(message: str) -> None:
    """macOS デスクトップ通知。MF2_DESKTOP_NOTIFY=1 のみ発火（既定 OFF）。"""
    print(f"[notify:Mercari] {message}")
    if os.environ.get("MF2_DESKTOP_NOTIFY") == "1":
        script = f'display notification "{message}" with title "Mercari スクレイパー" sound name "Ping"'
        subprocess.run(["osascript", "-e", script], capture_output=True)


# ─────────────────────────────────────────────
# メイン取得ロジック
# ─────────────────────────────────────────────

async def fetch_all(page: Page, download_pdf: bool = True) -> tuple[list[dict], list[dict]]:
    """購入・売上を全件取得して返す"""
    con = _db_connect()
    fetched_at = datetime.now().isoformat()

    # ── 購入履歴 ──
    print("[Mercari] 購入一覧を収集中...")
    purchase_entries = await _collect_purchase_entries(page)
    print(f"  購入エントリ: {len(purchase_entries)} 件")

    purchases: list[dict] = []
    pdf_count = 0
    for i, entry in enumerate(purchase_entries):
        href = entry["href"]
        tx_id = href.split("/")[-1]
        is_new = not _already_saved(con, "Mercari", f"[{tx_id}]")

        if is_new:
            price = await _fetch_transaction_price(page, href)
            if price is None:
                continue

            date = _normalize_date(entry["date_str"]) or datetime.now().strftime("%Y/%m/%d")
            desc = f"[{tx_id}] {entry['title'][:180]}"
            tx = {"date": date, "debit": price, "credit": 0, "description": desc, "fetched_at": fetched_at}
            if _save_tx(con, "Mercari", tx):
                purchases.append(tx)
                if (i + 1) % 10 == 0:
                    con.commit()
                    print(f"  購入: {i+1}/{len(purchase_entries)} 処理済...")

        if download_pdf:
            date_str = _normalize_date(entry["date_str"]) or datetime.now().strftime("%Y/%m/%d")
            saved = await _save_pdf(page, href, "purchases", tx_id, date_str)
            if saved:
                pdf_count += 1
                print(f"  PDF保存: {tx_id}")

    con.commit()
    if download_pdf:
        print(f"  購入PDF: {pdf_count} 件保存")

    # ── 売上履歴 ──
    print("[Mercari] 売上一覧を収集中...")
    sale_entries = await _collect_sale_entries(page)
    print(f"  売上エントリ: {len(sale_entries)} 件")

    sales: list[dict] = []
    pdf_count = 0
    for i, entry in enumerate(sale_entries):
        href = entry["href"]
        tx_id = href.split("/")[-1]
        is_new = not _already_saved(con, "Mercari売上", f"[{tx_id}]")

        if is_new:
            # リスト表示から価格取得できればページ遷移不要
            price = entry.get("price") or await _fetch_sale_price(page, href)
            if price is None:
                continue

            date = _normalize_date(entry["date_str"]) or datetime.now().strftime("%Y/%m/%d")
            desc = f"[{tx_id}] {entry['title'][:180]}"
            tx = {"date": date, "debit": 0, "credit": price, "description": desc, "fetched_at": fetched_at}
            if _save_tx(con, "Mercari売上", tx):
                sales.append(tx)
                if (i + 1) % 10 == 0:
                    con.commit()
                    print(f"  売上: {i+1}/{len(sale_entries)} 処理済...")

        if download_pdf:
            # 売上は /item/ → /transaction/ に変換してPDF保存
            tx_href = href.replace("/item/", "/transaction/")
            date_str = _normalize_date(entry["date_str"]) or datetime.now().strftime("%Y/%m/%d")
            saved = await _save_pdf(page, tx_href, "sales", tx_id, date_str)
            if saved:
                pdf_count += 1
                print(f"  PDF保存: {tx_id}")

    con.commit()
    con.close()
    if download_pdf:
        print(f"  売上PDF: {pdf_count} 件保存")
    return purchases, sales


# ─────────────────────────────────────────────
# 日付正規化
# ─────────────────────────────────────────────

def _normalize_date(raw: str) -> str | None:
    if not raw:
        return None
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", raw)
    if m:
        return f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"
    raw2 = raw.replace("年", "/").replace("月", "/").replace("日", "").strip()
    for fmt in ["%Y/%m/%d", "%y/%m/%d"]:
        try:
            return datetime.strptime(raw2, fmt).strftime("%Y/%m/%d")
        except ValueError:
            continue
    return None


# ─────────────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────────────

async def run(headless: bool = True, download_pdf: bool = True) -> tuple[list[dict], list[dict]]:
    _BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await _launch_context(p, headless)
        page = await context.new_page()

        await page.goto(PURCHASES_URL, wait_until="load", timeout=60_000)

        if _needs_login(page.url):
            if headless:
                await context.close()
                _notify("Mercari セッション切れ。ブラウザを開きます。")
                print("[Mercari] セッション切れ。headed ブラウザでログインします...")
                context = await _launch_context(p, headless=False)
                page = await context.new_page()
                await page.goto(PURCHASES_URL, wait_until="load", timeout=60_000)
            await login(page)

        purchases, sales = await fetch_all(page, download_pdf=download_pdf)

        total_p = _db_connect().execute(
            "SELECT COUNT(*) FROM transactions WHERE bank='Mercari'"
        ).fetchone()[0]
        total_s = _db_connect().execute(
            "SELECT COUNT(*) FROM transactions WHERE bank='Mercari売上'"
        ).fetchone()[0]
        print(f"Mercari 累計: 購入 {total_p} 件 / 売上 {total_s} 件")

        await context.close()
    from src.scrape_state import mark_scrape_done
    mark_scrape_done("mercari", count=len(purchases) + len(sales),
                     extra={"purchases": len(purchases), "sales": len(sales)})
    return purchases, sales


if __name__ == "__main__":
    asyncio.run(run(headless=False))
