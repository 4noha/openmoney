"""
楽天市場 注文履歴スクレイパー。

楽天ID/パスワードでログイン（二段階: ID → 次へ → PW）。
注文履歴を取得して SQLite に保存。
セッションは .browser_data/rakuten に保持。

データ取得戦略（優先順）:
  1. Next.js __NEXT_DATA__ からのパース
  2. API レスポンスのインターセプト
  3. innerText 正規表現パース（フォールバック）
"""
import asyncio
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, BrowserContext, Page

load_dotenv()

HISTORY_URL = "https://order.my.rakuten.co.jp/"
ORDER_LIST_URL = "https://order.my.rakuten.co.jp/purchase-history/order-list"
_HISTORY_BASE = "https://order.my.rakuten.co.jp/purchase-history/"

DB_PATH = Path(__file__).parent.parent.parent / "transactions.db"
_BROWSER_DATA_DIR = Path(__file__).parent.parent.parent / ".browser_data" / "rakuten"
_INVOICE_DIR = Path(__file__).parent.parent.parent / "invoices" / "rakuten"
_DETAIL_BASE = "https://order.my.rakuten.co.jp/purchase-history/order-detail"


def _order_detail_url(order_num: str) -> str:
    shop_id = order_num.split("-")[0]
    return f"{_HISTORY_BASE}?order_number={order_num}&shop_id={shop_id}&act=detail_page_view"


def _receipt_url(order_num: str) -> str:
    shop_id = order_num.split("-")[0]
    return f"{_HISTORY_BASE}?order_number={order_num}&shop_id={shop_id}&act=receipt_page_view"


def _parse_payment_amount(text: str) -> int:
    """詳細ページから支払い金額を抽出する。"""
    m = re.search(r"支払い金額\s*\n?([\d,]+)円", text)
    if m:
        return int(m.group(1).replace(",", ""))
    return 0


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
    return "login.account.rakuten.com" in url or "login.rakuten.com" in url


# ─────────────────────────────────────────────
# ログイン
# ─────────────────────────────────────────────

async def login(page: Page) -> None:
    rakuten_id = os.environ.get("RAKUTEN_ID", "")
    rakuten_pw = os.environ.get("RAKUTEN_PW", "")
    if not rakuten_id or not rakuten_pw:
        raise RuntimeError("RAKUTEN_ID / RAKUTEN_PW が .env に設定されていません")

    await page.wait_for_selector("input#user_id", timeout=15_000)
    await page.fill("input#user_id", rakuten_id)
    await page.click("#cta001")

    await page.wait_for_selector("input[type='password']", timeout=15_000)
    await page.fill("input[type='password']", rakuten_pw)
    await page.click("#cta011")
    await asyncio.sleep(3)
    print("[rakuten] ログイン完了")


# ─────────────────────────────────────────────
# DB 保存
# ─────────────────────────────────────────────

def _save(txs: list[dict]) -> int:
    """
    注文番号を含む description をキーに upsert する。
    同じ注文番号で金額が変わった場合（旧パースのバグ修正等）は debit を更新する。
    """
    if not txs:
        return 0
    from src.db import connect as _connect_central
    con = _connect_central(DB_PATH)
    saved = 0
    updated = 0
    for t in txs:
        desc = t["description"]
        debit = t.get("debit", 0)
        # 注文番号ありの場合: 同 bank・description で金額が異なるレコードを更新
        if desc.startswith("[") and debit > 0:
            existing = con.execute(
                "SELECT id, debit FROM transactions WHERE bank=? AND date=? AND description=?",
                (t["bank"], t["date"], desc),
            ).fetchone()
            if existing:
                if existing[1] != debit:
                    # 金額が変わっていたら更新
                    con.execute(
                        "UPDATE transactions SET debit=?, fetched_at=? WHERE id=?",
                        (debit, datetime.now().isoformat(), existing[0]),
                    )
                    updated += 1
                continue  # 既存レコードあり（金額同じまたは更新済み）→ insert スキップ
        from src.server.categorize import normalize_desc
        cur = con.execute(
            "INSERT OR IGNORE INTO transactions (bank, date, description, description_normalized, debit, fetched_at) VALUES (?,?,?,?,?,?)",
            (t["bank"], t["date"], desc, normalize_desc(desc),
             debit, datetime.now().isoformat()),
        )
        saved += cur.rowcount
    con.commit()
    con.close()
    if updated:
        print(f"[rakuten] 金額更新: {updated} 件")
    return saved


# ─────────────────────────────────────────────
# Next.js __NEXT_DATA__ パーサー
# ─────────────────────────────────────────────

def _parse_next_data(data: dict) -> list[dict]:
    """
    Next.js __NEXT_DATA__ から注文リストを探して返す。
    Rakuten のデータ構造が変わった場合は空リストを返す。
    """
    txs = []

    def _walk(obj, depth=0):
        if depth > 10 or not obj:
            return
        if isinstance(obj, list):
            for item in obj:
                _walk(item, depth + 1)
        elif isinstance(obj, dict):
            # orderNumber / orderDate / totalPrice の3フィールドを持つ dict を注文とみなす
            if "orderNumber" in obj and "orderDate" in obj:
                _extract_order(obj)
            else:
                for v in obj.values():
                    _walk(v, depth + 1)

    def _extract_order(obj: dict):
        order_num = str(obj.get("orderNumber", "")).strip()
        date_raw = obj.get("orderDate", "")
        # YYYY-MM-DD や YYYY/MM/DD を YYYY/MM/DD に正規化
        date_m = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})", str(date_raw))
        if not date_m:
            return
        date_str = f"{date_m.group(1)}/{date_m.group(2)}/{date_m.group(3)}"

        # 金額: totalAmount / totalPrice / orderAmount / paymentAmount
        amount = 0
        for key in ("paymentAmount", "totalAmount", "totalPrice", "orderAmount",
                    "chargeAmount", "billingAmount"):
            v = obj.get(key)
            if v is not None:
                try:
                    amount = int(str(v).replace(",", "").replace("¥", "").strip())
                    if amount > 0:
                        break
                except ValueError:
                    pass

        # 商品名: items[0].itemName / productName / name
        desc_base = ""
        for key in ("itemName", "productName", "name", "goodsName"):
            v = obj.get(key)
            if v:
                desc_base = str(v)[:60]
                break
        if not desc_base:
            items = obj.get("items") or obj.get("orderItems") or obj.get("goods") or []
            if items and isinstance(items, list):
                first = items[0]
                if isinstance(first, dict):
                    for k in ("itemName", "productName", "name", "goodsName"):
                        if first.get(k):
                            desc_base = str(first[k])[:60]
                            break

        if not desc_base:
            desc_base = "楽天市場"
        if amount == 0:
            return  # 金額不明は除外

        desc = f"[{order_num}] {desc_base}" if order_num else desc_base
        txs.append({"bank": "楽天市場", "date": date_str, "description": desc,
                    "debit": amount, "order_num": order_num})

    _walk(data)
    return txs


# ─────────────────────────────────────────────
# innerText 正規表現パーサー（フォールバック）
# ─────────────────────────────────────────────

def _parse_orders(text: str) -> list[dict]:
    txs: list[dict] = []
    # 注文ヘッダーパターン（注文番号も同時にキャプチャ）
    header_pattern = r"注文日[：:]\s*\n(\d{4}/\d{2}/\d{2})\([^)]+\)\n注文番号[：:]\s*\n([^\n]+)"
    matches = list(re.finditer(header_pattern, text))
    if not matches:
        # 旧形式フォールバック
        header_pattern2 = r"注文日[：:]\s*\n(\d{4}/\d{2}/\d{2})\(\S\)\n注文番号[：:]"
        matches = list(re.finditer(header_pattern2, text))

    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[start:end]

        date_str = m.group(1)

        # 注文番号: グループ2があればそれを使う、なければ別途検索
        if m.lastindex and m.lastindex >= 2:
            order_num = m.group(2).strip()
        else:
            order_num_m = re.search(r"注文番号[：:]\s*\n?([^\n]+)", chunk)
            order_num = order_num_m.group(1).strip() if order_num_m else ""

        # 商品名: 「その他」後 or 「注文番号」後の最初の説明テキスト
        item_m = re.search(r"その他\n(.+?)(?:\n[\d,]+円|\n(?:送料|合計|商品代金|ご請求))", chunk, re.DOTALL)
        if item_m:
            desc_base = item_m.group(1).strip().splitlines()[0][:60]
        else:
            # 注文番号行の後にある最初の非数字・非記号のテキストを拾う
            lines = chunk.split('\n')
            desc_base = "楽天市場"
            skip = 4  # 最初の数行（注文日・日付・注文番号・番号）はスキップ
            for line in lines[skip:]:
                line = line.strip()
                if line and len(line) > 4 and not re.match(r'^[\d,円\s※（）()]+$', line):
                    desc_base = line[:60]
                    break

        # 金額: ご請求金額 > 合計 > お支払い > チャンク末尾の最後の金額
        amount = 0
        for pat in [
            r"ご請求金額[^\d\n]*\n?([\d,]+)円",
            r"お支払い金額[^\d\n]*\n?([\d,]+)円",
            r"合計[^\d\n]*\n?([\d,]+)円",
            r"ご注文合計[^\d\n]*\n?([\d,]+)円",
        ]:
            am = re.search(pat, chunk)
            if am:
                amount = int(am.group(1).replace(",", ""))
                break
        if amount == 0:
            # フォールバック: チャンク後半の最後の金額
            all_amounts = re.findall(r"([\d,]+)円", chunk[len(chunk)//2:])
            if all_amounts:
                amount = int(all_amounts[-1].replace(",", ""))
        if amount == 0:
            continue

        desc = f"[{order_num}] {desc_base}" if order_num else desc_base
        txs.append({"bank": "楽天市場", "date": date_str, "description": desc,
                    "debit": amount, "order_num": order_num})
    return txs


# ─────────────────────────────────────────────
# ページから注文データを取得
# ─────────────────────────────────────────────

async def _extract_page_orders(page: Page) -> list[dict]:
    """
    1. __NEXT_DATA__ を試みる
    2. 失敗したら innerText パースにフォールバック
    """
    try:
        raw = await page.evaluate("() => { const el = document.getElementById('__NEXT_DATA__'); return el ? el.textContent : null; }")
        if raw:
            data = json.loads(raw)
            txs = _parse_next_data(data)
            if txs:
                print(f"[rakuten] __NEXT_DATA__ から {len(txs)} 件取得")
                return txs
    except Exception as e:
        print(f"[rakuten] __NEXT_DATA__ 解析失敗: {e}")

    text = await page.evaluate("() => document.body.innerText")
    txs = _parse_orders(text)
    print(f"[rakuten] テキストパース: {len(txs)} 件")
    return txs


# ─────────────────────────────────────────────
# 注文履歴取得
# ─────────────────────────────────────────────

async def _fetch_orders(page: Page) -> list[dict]:
    txs: list[dict] = []
    # API インターセプト（Next.js が呼ぶバックエンド API）
    api_orders: list[dict] = []

    async def on_response(resp):
        ct = resp.headers.get("content-type", "")
        if "json" not in ct:
            return
        url = resp.url
        if not any(k in url for k in ["order", "purchase", "history"]):
            return
        try:
            data = await resp.json()
            parsed = _parse_next_data(data) if isinstance(data, dict) else []
            if parsed:
                api_orders.extend(parsed)
                print(f"[rakuten] API {url}: {len(parsed)} 件")
        except Exception:
            pass

    page.on("response", on_response)

    await page.goto(HISTORY_URL, wait_until="domcontentloaded")
    await asyncio.sleep(3)

    if _needs_login(page.url):
        await login(page)

    if _needs_login(page.url):
        print("[rakuten] ログイン失敗")
        return txs

    if ORDER_LIST_URL not in page.url:
        await page.goto(ORDER_LIST_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)

    try:
        await page.wait_for_selector("select", timeout=10_000)
    except Exception:
        print("[rakuten] 注文履歴ページが見つかりません")
        await page.screenshot(path="/tmp/rakuten_debug.png")
        return txs

    year_select = await page.query_selector("select[name='year']") or await page.query_selector("select")
    options = await year_select.query_selector_all("option") if year_select else []
    years = [v for opt in options if (v := await opt.get_attribute("value")) and v.isdigit()]

    for year in years:
        try:
            await page.select_option("select[name='year']", year)
        except Exception:
            await page.select_option("select", year)
        await asyncio.sleep(3)

        year_txs = await _extract_page_orders(page)
        txs.extend(year_txs)
        print(f"[rakuten] {year}年: {len(year_txs)} 件")

    # API インターセプトで追加取得があればマージ
    if api_orders:
        existing_nums = {t.get("order_num") for t in txs if t.get("order_num")}
        new_from_api = [o for o in api_orders if o.get("order_num") not in existing_nums]
        if new_from_api:
            txs.extend(new_from_api)
            print(f"[rakuten] API インターセプトで追加 {len(new_from_api)} 件")

    page.remove_listener("response", on_response)
    return txs


# ─────────────────────────────────────────────
# PDF 領収書保存
# ─────────────────────────────────────────────

def _has_order_content(text: str, order_num: str) -> bool:
    """ページが有効な注文内容を含んでいるか確認する。"""
    return order_num in text and "支払い金額" in text


async def _fetch_order_page(page: Page, order_num: str) -> str:
    """
    注文詳細ページを取得する。
    receipt_page_view を試みて有効な注文内容がなければ detail_page_view にフォールバック。
    """
    url = _receipt_url(order_num)
    await page.goto(url, wait_until="networkidle", timeout=30_000)
    try:
        await page.wait_for_selector("text=支払い金額", timeout=8_000)
    except Exception:
        pass
    await asyncio.sleep(1)
    text = await page.evaluate("() => document.body.innerText")

    if not _has_order_content(text, order_num):
        detail_url = _order_detail_url(order_num)
        await page.goto(detail_url, wait_until="networkidle", timeout=30_000)
        try:
            await page.wait_for_selector("text=支払い金額", timeout=8_000)
        except Exception:
            pass
        await asyncio.sleep(1)
        text = await page.evaluate("() => document.body.innerText")
        print(f"  [rakuten] {order_num}: receipt_page_view に注文内容なし → 詳細ページで代替")

    return text


async def _save_pdfs(page: Page, txs: list[dict]) -> None:
    """注文詳細ページから PDF を保存し、支払い金額を DB に反映する（既存ファイルはスキップ）。"""
    # screen メディアで PDF 生成するとスクリーンショットと同じレイアウトになる
    await page.emulate_media(media="screen")
    from src.db import connect as _connect_central
    con = _connect_central(DB_PATH)
    for tx in txs:
        order_num = tx.get("order_num", "")
        if not order_num:
            continue
        date_part = tx["date"].replace("/", "-")
        year_dir = _INVOICE_DIR / tx["date"][:4]
        pdf_path = year_dir / f"{date_part}_{order_num}.pdf"
        if pdf_path.exists():
            continue

        try:
            if _needs_login(page.url):
                print(f"  [rakuten] {order_num}: セッション切れ")
                con.close()
                return

            text = await _fetch_order_page(page, order_num)

            if _needs_login(page.url):
                print(f"  [rakuten] {order_num}: セッション切れ")
                con.close()
                return

            # 支払い金額で DB を更新
            payment = _parse_payment_amount(text)
            if payment and payment != tx.get("debit", 0):
                con.execute(
                    "UPDATE transactions SET debit=?, fetched_at=? "
                    "WHERE bank='楽天市場' AND description LIKE ?",
                    (payment, datetime.now().isoformat(), f"[{order_num}]%"),
                )
                con.commit()
                print(f"  [rakuten] {order_num}: 金額修正 {tx.get('debit')} → {payment}")
                tx["debit"] = payment  # _save() が後で上書きしないよう同期

            year_dir.mkdir(parents=True, exist_ok=True)
            await page.pdf(
                path=str(pdf_path),
                format="A4",
                print_background=True,
                margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"},
            )
            print(f"  [rakuten] PDF保存: {pdf_path.name} (支払い金額 ¥{payment:,})")
            await asyncio.sleep(1)
        except Exception as e:
            print(f"  [rakuten] {order_num} PDF失敗: {e}")
    con.close()


# ─────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────

async def run(headless: bool = True) -> list[dict]:
    _BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        ctx = await _launch_context(p, headless)
        page = await ctx.new_page()
        txs = await _fetch_orders(page)
        if txs:
            await _save_pdfs(page, txs)
        await ctx.close()
    saved = _save(txs)
    print(f"[rakuten] {saved}/{len(txs)} 件保存")
    from src.scrape_state import mark_scrape_done
    mark_scrape_done("rakuten", count=len(txs), extra={"saved": saved})
    return txs


if __name__ == "__main__":
    asyncio.run(run(headless=False))
