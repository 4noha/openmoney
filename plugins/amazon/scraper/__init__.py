"""
Amazon.co.jp 注文履歴スクレイパー（パッケージ）。

初回・セッション切れ時は headed ブラウザを自動起動し、ユーザーが
メールアドレス・パスワード入力とブラウザ承認（2FA）を行う。
ログイン後のセッション Cookie は .browser_data/amazon に保持するため、
以降は headless で自動実行される。

サブモジュール:
    auth      : ログイン・OTP 処理
    orders    : 注文履歴取得・パース・DB 保存・注文詳細／商品リスト
    pay       : Amazon Pay 提携サイト利用履歴
    invoices  : 領収書 PDF ダウンロード
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, BrowserContext

load_dotenv()


# ─────────────────────────────────────────────
# 共通定数・パス
# ─────────────────────────────────────────────

ORDER_HISTORY_URL = "https://www.amazon.co.jp/your-orders/orders"
AMAZON_PAY_FILTER = "orderFilter=amazon-pay"
DB_PATH = Path(__file__).parent.parent.parent.parent / "transactions.db"
_BROWSER_DATA_DIR = Path(__file__).parent.parent.parent.parent / ".browser_data" / "amazon"
_INVOICE_DIR = Path(__file__).parent.parent.parent.parent / "invoices" / "amazon"

# 取得する年数（今年を含む過去N年）
HISTORY_YEARS = 3


# ─────────────────────────────────────────────
# 共有ヘルパー（サブモジュールから import される）
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


def _notify(message: str) -> None:
    """macOS デスクトップ通知。MF2_DESKTOP_NOTIFY=1 のみ発火（既定 OFF）。"""
    print(f"[notify:Amazon] {message}")
    if os.environ.get("MF2_DESKTOP_NOTIFY") == "1":
        script = f'display notification "{message}" with title "Amazon スクレイパー" sound name "Ping"'
        subprocess.run(["osascript", "-e", script], capture_output=True)


def _extract_order_id(description: str) -> str:
    """description から注文IDを抽出: '[D01-xxx] ...' → 'D01-xxx'。
    サブ行 '[D01-xxx/N] ...' / '[D01-xxx/送料] ...' は親 ID 'D01-xxx' を返す。
    """
    if description and description.startswith("["):
        end = description.find("]")
        if end > 0:
            inner = description[1:end]
            slash = inner.find("/")
            if slash >= 0:
                inner = inner[:slash]
            return inner
    return ""


def _db_connect() -> sqlite3.Connection:
    """スキーマは src.db に集約。"""
    from src.db import connect as _connect_central
    return _connect_central(DB_PATH)


# ─────────────────────────────────────────────
# サブモジュールの読み込み（後方互換のための再エクスポート）
# ─────────────────────────────────────────────

from .auth import (  # noqa: E402,F401
    _needs_login, _on_orders_page, _needs_otp, login,
)
from .orders import (  # noqa: E402,F401
    fetch_order_history, save_to_db, fetch_order_details, split_orders,
)
from .pay import (  # noqa: E402,F401
    fetch_amazon_pay_history, save_amazon_pay_to_db,
)
from .invoices import download_invoices  # noqa: E402,F401


# ─────────────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────────────

async def run(headless: bool = True, download_pdf: bool = True) -> list[dict]:
    _BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await _launch_context(p, headless)
        page = await context.new_page()

        await page.goto(ORDER_HISTORY_URL, wait_until="load", timeout=60_000)

        if _needs_login(page.url):
            if headless:
                # セッション切れ: headed ブラウザに切り替えてログイン
                await context.close()
                _notify("Amazon セッション切れ。ブラウザを開きます。")
                print("[Amazon] セッション切れ。headed ブラウザでログインします...")
                context = await _launch_context(p, headless=False)
                page = await context.new_page()
                await page.goto(ORDER_HISTORY_URL, wait_until="load", timeout=60_000)
            await login(page)

        transactions = await fetch_order_history(page)
        saved = save_to_db(transactions)
        print(f"DB保存: {saved} 件追加 → {DB_PATH}")

        print("Amazon Pay 提携サイト履歴 取得中...")
        amazon_pay_txs = await fetch_amazon_pay_history(page)
        amazon_pay_saved = save_amazon_pay_to_db(amazon_pay_txs)
        print(f"Amazon Pay DB保存: {amazon_pay_saved} 件追加")

        total = _db_connect().execute(
            "SELECT COUNT(*) FROM transactions WHERE bank='Amazon'"
        ).fetchone()[0]
        print(f"Amazon 累計: {total} 件")

        con = _db_connect()
        await fetch_order_details(page)

        # 品質チェック: 平均10件超はレコメンド混入の可能性（漫画一括購入は30件超も正常）
        stats = con.execute("""
            SELECT AVG(cnt), MAX(cnt) FROM
            (SELECT COUNT(*) as cnt FROM amazon_order_items GROUP BY order_id)
        """).fetchone()
        avg_cnt = stats[0] or 0
        max_cnt = stats[1] or 0
        print(f"[Amazon] 商品リスト品質: 平均{avg_cnt:.1f}件/注文, 最大{max_cnt}件/注文")
        if avg_cnt <= 10:
            split_orders(con)
        else:
            print(f"[Amazon] 警告: 商品リストに誤取得の疑い → split_orders をスキップ")
        con.close()

        if download_pdf:
            print("領収書PDF ダウンロード中...")
            pdf_saved = await download_invoices(page, transactions)
            print(f"領収書保存: {pdf_saved} 件 → {_INVOICE_DIR}")

        await context.close()
    from src.scrape_state import mark_scrape_done
    mark_scrape_done("amazon", count=len(transactions))
    return transactions


if __name__ == "__main__":
    asyncio.run(run(headless=False))
