"""Amazon Pay 提携サイト利用履歴の取得・パース・保存。"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime

from playwright.async_api import Page

from . import HISTORY_YEARS, _db_connect


AMAZON_PAY_ORDERS_URL = "https://pay.amazon.co.jp/jr/your-account/orders"


async def fetch_amazon_pay_history(page: Page) -> list[dict]:
    """Amazon Pay 外部加盟店利用履歴を取得する（pay.amazon.co.jp）。

    ページは 2年ごとの期間フィルタ形式。HISTORY_YEARS 分をカバーする期間を選択して取得する。
    """
    await page.goto(AMAZON_PAY_ORDERS_URL, wait_until="load", timeout=60_000)
    await asyncio.sleep(2)

    all_txs: list[dict] = []
    today = datetime.now()
    cutoff_year = today.year - HISTORY_YEARS + 1

    period_links = await page.query_selector_all("a")
    period_hrefs: list[str] = []
    for link in period_links:
        text = (await link.inner_text()).strip()
        href = await link.get_attribute("href") or ""
        if re.search(r"\d{4}年\d+月.+\d{4}年\d+月", text) and href:
            years_in_text = [int(m) for m in re.findall(r"\d{4}", text)]
            if years_in_text and max(years_in_text) >= cutoff_year:
                full_href = href if href.startswith("http") else f"https://pay.amazon.co.jp{href}"
                if full_href not in period_hrefs:
                    period_hrefs.append(full_href)

    if not period_hrefs:
        period_hrefs = [AMAZON_PAY_ORDERS_URL]

    seen_keys: set[tuple] = set()
    for href in period_hrefs:
        if href != page.url:
            await page.goto(href, wait_until="load", timeout=60_000)
            await asyncio.sleep(2)
        txs = await _parse_amazon_pay_page(page, cutoff_year)
        for tx in txs:
            key = (tx["date"], tx["debit"], tx["description"])
            if key not in seen_keys:
                seen_keys.add(key)
                all_txs.append(tx)

    print(f"  Amazon Pay 合計: {len(all_txs)} 件")
    return all_txs


async def _parse_amazon_pay_page(page: Page, cutoff_year: int = 2000) -> list[dict]:
    """pay.amazon.co.jp の利用履歴ページをパースする。

    テーブル行テキスト形式: 日付\\t金額\\t販売事業者\\t...
    例: '2026/04/07\\t￥9,460\\tアミナコレクションオンラインショップ\\t\\t詳細とサポート'
    """
    transactions: list[dict] = []
    fetched_at = datetime.now().isoformat()

    text = await page.evaluate("() => document.body.innerText")
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) < 3:
            continue
        date_str, amount_str, merchant = parts[0], parts[1], parts[2]

        if not re.match(r"^\d{4}/\d{2}/\d{2}$", date_str):
            continue
        year = int(date_str[:4])
        if year < cutoff_year:
            continue

        amount_clean = amount_str.replace(",", "").replace("¥", "").replace("￥", "").strip()
        if not amount_clean.isdigit():
            continue

        if not merchant:
            merchant = "Amazon Pay 外部加盟店"

        transactions.append({
            "date": date_str,
            "debit": int(amount_clean),
            "credit": 0,
            "description": merchant[:80],
            "fetched_at": fetched_at,
        })

    return transactions


def save_amazon_pay_to_db(transactions: list[dict]) -> int:
    con = _db_connect()
    before = con.execute("SELECT COUNT(*) FROM transactions WHERE bank='AmazonPay'").fetchone()[0]
    for tx in transactions:
        try:
            con.execute(
                "INSERT OR IGNORE INTO transactions "
                "(bank, date, description, debit, credit, balance, fetched_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("AmazonPay", tx["date"], tx["description"],
                 tx["debit"], tx["credit"], 0, tx["fetched_at"]),
            )
        except Exception as e:
            print(f"  [warn] AmazonPay save: {e}")
    con.commit()
    after = con.execute("SELECT COUNT(*) FROM transactions WHERE bank='AmazonPay'").fetchone()[0]
    con.close()
    return after - before
