import asyncio
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout

load_dotenv()

LOGIN_URL = "https://www.smbc-card.com/mem/index.jsp"
HISTORY_MONTHS = 13
DB_PATH = Path(__file__).parent.parent.parent / "transactions.db"


# ─────────────────────────────────────────────
# ログイン
# ─────────────────────────────────────────────

async def login(page: Page) -> None:
    if not os.environ.get("VPASS_ID"):
        raise RuntimeError("VPASS_ID が .env に設定されていません")
    if not os.environ.get("VPASS_PW"):
        raise RuntimeError("VPASS_PW が .env に設定されていません")

    await page.goto(LOGIN_URL, wait_until="load", timeout=60_000)

    # フォームがJS描画されるまで待機
    try:
        await page.wait_for_selector("input#id_input, input[name='userid']", timeout=15_000)
    except Exception:
        await page.screenshot(path="/tmp/vpass_login_debug.png")
        raise RuntimeError("ログインフォームが表示されませんでした（/tmp/vpass_login_debug.png 参照）")

    # Akamai のボット検知チャレンジ（_abck Cookie）が完了するまで待機
    for _ in range(20):
        cookies = await page.context.cookies()
        if any(c["name"] == "_abck" for c in cookies):
            break
        await asyncio.sleep(0.5)
    else:
        # Cookie が取れなくても続行（初回は設定に時間がかかることがある）
        await asyncio.sleep(2)

    # ログインID（確認済みセレクタ優先）
    for sel in [
        "input#id_input",
        "input[name='userid']",
        "input[name='inputLoginId']",
        "input[name='loginId']",
    ]:
        try:
            await page.fill(sel, os.environ["VPASS_ID"], timeout=3_000)
            print(f"  ログインID入力: {sel}")
            break
        except Exception:
            continue
    else:
        await page.screenshot(path="/tmp/vpass_login_debug.png")
        raise RuntimeError("ログインID入力欄が見つかりません（/tmp/vpass_login_debug.png 参照）")

    await asyncio.sleep(1)

    # パスワード（確認済みセレクタ優先）
    for sel in [
        "input#pw_input",
        "input[name='password']",
        "input[type='password']",
    ]:
        try:
            await page.fill(sel, os.environ["VPASS_PW"], timeout=3_000)
            print(f"  パスワード入力: {sel}")
            break
        except Exception:
            continue
    else:
        await page.screenshot(path="/tmp/vpass_login_debug.png")
        raise RuntimeError("パスワード入力欄が見つかりません")

    await asyncio.sleep(1)

    # ログインボタンをクリックして送信
    await page.click("input.btnNormal[type='submit']")
    print("  ログインボタンクリック")

    # ログイン後のリダイレクト完了を待つ
    # agree/v1 → memx/mypage の全リダイレクトチェーンが完了するまで待機
    try:
        await page.wait_for_url("**/memx/**", timeout=30_000)
    except Exception:
        # memx に到達しなかった場合は現在の状態を確認
        pass
    print(f"ログイン後URL: {page.url}")

    content = await page.content()

    # ログイン失敗チェック（ログインページに戻った場合も含む）
    if "index.jsp" in page.url and "mem/index" in page.url:
        await page.screenshot(path="/tmp/vpass_login_failed.png")
        raise RuntimeError("VPASSログインに失敗しました。VPASS_ID/VPASS_PW を確認してください。")
    if any(kw in content for kw in ["ログインできません", "IDまたはパスワードが", "認証に失敗", "ログインに失敗"]):
        await page.screenshot(path="/tmp/vpass_login_failed.png")
        raise RuntimeError("VPASSログインに失敗しました。VPASS_ID/VPASS_PW を確認してください。")

    # OTPチェック
    if any(kw in content for kw in ["ワンタイムパスワード", "認証番号", "確認コード", "セキュリティコード", "二段階認証"]):
        await _handle_otp(page)


async def _handle_otp(page: Page) -> None:
    print("=" * 50)
    print("VPASS 二段階認証が必要です（SMS/メールの認証コード）")
    print("=" * 50)
    _notify("VPASSの認証コードを入力してください")

    otp = await _wait_for_otp()

    # OTP入力欄
    for sel in [
        "input[name='oneTimePassword']",
        "input[name='otpCode']",
        "input[name='authCode']",
        "input[id*='otp' i]",
        "input[id*='authCode' i]",
        "input[type='tel']",
        "input[type='number']",
        "input[maxlength='6']",
        "input[maxlength='8']",
    ]:
        try:
            await page.fill(sel, otp, timeout=3_000)
            print(f"  OTP入力: {sel}")
            break
        except Exception:
            continue
    else:
        await page.screenshot(path="/tmp/vpass_otp_debug.png")
        raise RuntimeError("OTP入力欄が見つかりません（/tmp/vpass_otp_debug.png 参照）")

    # 送信
    for sel in [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('認証')",
        "button:has-text('確認')",
        "button:has-text('送信')",
        "button:has-text('次へ')",
    ]:
        try:
            await page.click(sel, timeout=3_000)
            break
        except Exception:
            continue

    await page.wait_for_load_state("load")
    print(f"OTP認証後URL: {page.url}")


async def _wait_for_otp() -> str:
    # インタラクティブモード: stdin から入力
    try:
        if sys.stdin.isatty():
            code = input("認証コードを入力してください: ").strip()
            if code:
                return code
    except EOFError:
        pass

    # デーモンモード: サーバー経由（/vpass-otp エンドポイント）
    print("サーバー経由でOTPを待機中（最大5分）... Android から /vpass-otp へ POST してください")
    try:
        from src.server import _get, _set
    except ImportError:
        raise RuntimeError("サーバーモジュールが読み込めません。OTPを手動で入力できません。")

    deadline = asyncio.get_event_loop().time() + 300
    while asyncio.get_event_loop().time() < deadline:
        otp = _get("vpass_otp")
        if otp:
            _set("vpass_otp", "")
            print("  OTP受信（サーバー経由）")
            return otp
        await asyncio.sleep(5)

    raise RuntimeError("OTP入力タイムアウト（5分）")


def _notify(message: str) -> None:
    """macOS デスクトップ通知。MF2_DESKTOP_NOTIFY=1 のみ発火（既定 OFF）。"""
    print(f"[notify:VPASS] {message}")
    if os.environ.get("MF2_DESKTOP_NOTIFY") == "1":
        script = f'display notification "{message}" with title "VPASS認証" sound name "Ping"'
        subprocess.run(["osascript", "-e", script], capture_output=True)


# ─────────────────────────────────────────────
# 明細取得
# ─────────────────────────────────────────────

MEISAI_URL = "https://www.smbc-card.com/memx/web_meisai/top/index.html"


async def _navigate_to_meisai(page: Page) -> None:
    await page.goto(MEISAI_URL, wait_until="load", timeout=60_000)
    await page.wait_for_timeout(3_000)
    print(f"  利用明細ページ: {page.url}")
    await page.screenshot(path="/tmp/vpass_meisai_top.png")


async def _get_available_months(page: Page) -> list[str]:
    """月選択の option 値一覧を返す（なければ空リスト）"""
    for sel in ["select[name*='month']", "select[id*='month']", "select[name*='Month']", "select"]:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            options = await loc.evaluate(
                "el => Array.from(el.options).map(o => o.value).filter(v => v)"
            )
            if options:
                return options
        except Exception:
            continue
    return []


async def _select_month(page: Page, value: str) -> None:
    """月セレクトボックスで指定月を選択し「照会」ボタンを押してデータを更新する"""
    # 月セレクト: Dojo widget の select を探す（name が長い固定値なので "select" で代用）
    for sel in ["select[name*='vp-view']", "select"]:
        try:
            await page.select_option(sel, value, timeout=3_000)
            break
        except Exception:
            continue

    # 「照会」ボタン（input type=submit）をクリック
    # ※ input[type='submit'] だとページ先頭の「検索」ボタンにマッチするため class で絞る
    for btn_sel in [
        "input.inquiry_btn:not([disabled])",
        "input#U051112-btn01",
        "input[value='照会'][type='submit']",
    ]:
        try:
            await page.click(btn_sel, timeout=3_000)
            break
        except Exception:
            continue

    # フォーム送信後のページ更新（AJAX or full reload）を待つ
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        await asyncio.sleep(3)


async def _parse_table(page: Page) -> list[dict]:
    """現在ページのテーブルを解析して取引リストを返す。

    VPASS のテーブル列構造:
      cells[0]: 空 td
      cells[1]: ご利用日 (YY/MM/DD)
      cells[2]: ご利用店名
      cells[3]: ご利用金額 (span 内)
      cells[4]+: 支払区分・回数・お支払金額 など
    """
    transactions: list[dict] = []

    rows = await page.query_selector_all("table tr")
    for row in rows:
        cells = await row.query_selector_all("td")
        if len(cells) < 4:
            continue

        texts = [(await c.inner_text()).strip().replace("\xa0", "") for c in cells]

        # cells[1] が日付
        date_raw = texts[1]
        if not date_raw or not any(c.isdigit() for c in date_raw):
            continue

        normalized_date = _normalize_date(date_raw)
        if not normalized_date:
            continue

        description = _normalize_description(texts[2]) if len(texts) > 2 else ""

        # cells[3] が利用金額（カンマ区切り）
        amount_raw = texts[3].replace(",", "").replace("円", "").replace("￥", "").strip()
        if not amount_raw or not amount_raw.lstrip("-").isdigit():
            continue

        amount = int(amount_raw)
        debit = max(amount, 0)
        credit = max(-amount, 0)

        transactions.append({
            "date": normalized_date,
            "debit": debit,
            "credit": credit,
            "description": description,
            "fetched_at": datetime.now().isoformat(),
        })

    return transactions


def _normalize_date(raw: str) -> str | None:
    """各種日付フォーマットを YYYY/MM/DD に正規化"""
    raw = raw.strip().replace("年", "/").replace("月", "/").replace("日", "")
    for fmt in ["%Y/%m/%d", "%y/%m/%d", "%m/%d", "%Y-%m-%d"]:
        try:
            parsed = datetime.strptime(raw, fmt)
            if parsed.year == 1900:
                parsed = parsed.replace(year=datetime.now().year)
            return parsed.strftime("%Y/%m/%d")
        except ValueError:
            continue
    return None


async def fetch_all_history(page: Page) -> list[dict]:
    """
    DB 状態に応じて差分のみ取得する。
    - 初回または過去データ不足 → HISTORY_MONTHS 月分取得
    - データ充足 → 直近 2 ヶ月のみ増分取得
    """
    await _navigate_to_meisai(page)
    await page.screenshot(path="/tmp/vpass_meisai_top.png")

    newest_in_db = _get_newest_vpass_date()
    oldest_in_db = _get_oldest_vpass_date()
    today = date.today()
    history_start = today - relativedelta(months=HISTORY_MONTHS)

    incremental = (
        oldest_in_db is not None
        and oldest_in_db <= history_start + timedelta(days=7)
    )

    # 月選択方式を試みる
    months = await _get_available_months(page)

    if months:
        return await _fetch_by_month_select(page, months, incremental, newest_in_db)
    else:
        return await _fetch_by_month_navigation(page, incremental, newest_in_db)


async def _fetch_by_month_select(
    page: Page,
    months: list[str],
    incremental: bool,
    newest_in_db: date | None,
) -> list[dict]:
    target_count = 2 if incremental else HISTORY_MONTHS
    target_months = months[:target_count]

    all_txs: list[dict] = []
    for month_val in target_months:
        print(f"  月選択: {month_val} ...", end=" ", flush=True)
        await _select_month(page, month_val)
        month_txs = await _download_csv_for_month(page)
        print(f"{len(month_txs)} 件")
        all_txs.extend(month_txs)

    return all_txs


async def _download_csv_for_month(page: Page) -> list[dict]:
    """現在表示中の月の CSV をダウンロードしてパース。失敗時は HTML パースにフォールバック。"""
    try:
        async with page.expect_download(timeout=30_000) as dl_info:
            await page.click("a:has-text('CSV形式で保存する')", timeout=5_000)
        download = await dl_info.value
        src = await download.path()
        dest = Path("/tmp/vpass_dl.csv")
        shutil.copy(src, dest)
        return _parse_csv_file(dest)
    except Exception as e:
        print(f"(CSV失敗:{e.__class__.__name__}) ", end="", flush=True)
        return await _fetch_all_pages(page)


def _normalize_description(s: str) -> str:
    """description を DB 保存用に正規化する（全角/半角ハイフン統一など）"""
    return s.replace("　", " ").replace("−", "－").strip()


def _parse_csv_file(path: Path) -> list[dict]:
    """VPASS CSV（Shift-JIS）をパースして取引リストを返す。

    列構造: 日付, 利用店名, 利用金額, 支払区分, 今回回数, お支払い金額, 追加情報
    ヘッダー行（名前,カード番号,カード名）や合計行（最初のフィールドが空）はスキップ。
    """
    transactions = []
    fetched_at = datetime.now().isoformat()
    with open(path, encoding="shift_jis", errors="replace") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line:
                continue
            cols = line.split(",")
            # 日付列が YYYY/MM/DD 形式でなければスキップ
            if not re.match(r"^\d{4}/\d{2}/\d{2}$", cols[0]):
                continue
            if len(cols) < 3:
                continue

            date_str = cols[0]
            description = _normalize_description(cols[1])
            amount_raw = cols[2].replace(",", "").strip()
            if not amount_raw or not amount_raw.lstrip("-").isdigit():
                continue

            amount = int(amount_raw)
            transactions.append({
                "date": date_str,
                "debit": max(amount, 0),
                "credit": max(-amount, 0),
                "description": description,
                "fetched_at": fetched_at,
            })
    return transactions


async def _fetch_all_pages(page: Page) -> list[dict]:
    """HTML テーブルの全ページを取得する（CSV 失敗時のフォールバック）"""
    all_txs = await _parse_table(page)

    while True:
        next_btn = page.locator("span.return_back_meisai:not(.disabled)").first
        if await next_btn.count() == 0 or not await next_btn.is_visible():
            break
        text = (await next_btn.inner_text()).strip()
        if "次へ" not in text:
            break
        await next_btn.click()
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            await asyncio.sleep(2)
        page_txs = await _parse_table(page)
        if not page_txs:
            break
        all_txs.extend(page_txs)

    return all_txs


async def _fetch_by_month_navigation(
    page: Page,
    incremental: bool,
    newest_in_db: date | None,
) -> list[dict]:
    """前月ボタンを繰り返し押して月ごとに取得"""
    target_count = 2 if incremental else HISTORY_MONTHS
    all_txs: list[dict] = []

    for i in range(target_count):
        print(f"  月 {i + 1}/{target_count} 取得 ...", end=" ", flush=True)
        txs = await _parse_table(page)
        print(f"{len(txs)} 件")
        all_txs.extend(txs)

        if i < target_count - 1:
            moved = await _go_to_prev_month(page)
            if not moved:
                print("  前月ボタンが見つかりません。取得を終了します。")
                break

    return all_txs


async def _go_to_prev_month(page: Page) -> bool:
    for sel in [
        "a:has-text('前月')",
        "button:has-text('前月')",
        "a:has-text('先月')",
        "a:has-text('＜')",
        "a:has-text('<')",
        "button:has-text('＜')",
        "[class*='prev']",
        "[class*='before']",
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click()
                await page.wait_for_load_state("load")
                return True
        except Exception:
            continue
    return False


# ─────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────

def _db_connect() -> sqlite3.Connection:
    from src.db import connect as _connect_central
    return _connect_central(DB_PATH)


def _get_oldest_vpass_date() -> date | None:
    try:
        con = _db_connect()
        row = con.execute("SELECT MIN(date) FROM transactions WHERE bank='VPASS'").fetchone()
        con.close()
        if row and row[0]:
            return datetime.strptime(row[0], "%Y/%m/%d").date()
    except Exception:
        pass
    return None


def _get_newest_vpass_date() -> date | None:
    try:
        con = _db_connect()
        row = con.execute("SELECT MAX(date) FROM transactions WHERE bank='VPASS'").fetchone()
        con.close()
        if row and row[0]:
            return datetime.strptime(row[0], "%Y/%m/%d").date()
    except Exception:
        pass
    return None


def save_to_db(transactions: list[dict]) -> int:
    from src.server.categorize import normalize_desc
    con = _db_connect()
    before = con.execute("SELECT COUNT(*) FROM transactions WHERE bank='VPASS'").fetchone()[0]
    for tx in transactions:
        try:
            con.execute(
                "INSERT OR IGNORE INTO transactions "
                "(bank, date, description, description_normalized, debit, credit, balance, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    "VPASS", tx["date"], tx["description"],
                    normalize_desc(tx["description"]),
                    int(tx["debit"] or 0), int(tx["credit"] or 0),
                    0, tx["fetched_at"],
                ),
            )
        except (ValueError, sqlite3.Error) as e:
            print(f"  DB保存スキップ: {tx} ({e})")
    con.commit()
    after = con.execute("SELECT COUNT(*) FROM transactions WHERE bank='VPASS'").fetchone()[0]
    con.close()
    return after - before


# ─────────────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────────────

# Akamai のセッション Cookie を保持するための永続コンテキストディレクトリ
_BROWSER_DATA_DIR = Path(__file__).parent.parent.parent / ".browser_data" / "vpass"


async def run(headless: bool = False) -> list[dict]:
    _BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        # launch_persistent_context で Akamai Cookie（_abck 等）を毎回再利用する
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(_BROWSER_DATA_DIR),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        await login(page)
        transactions = await fetch_all_history(page)

        new_count = save_to_db(transactions)
        print(f"DB保存: {new_count} 件追加 → {DB_PATH}")

        total = _db_connect().execute(
            "SELECT COUNT(*) FROM transactions WHERE bank='VPASS'"
        ).fetchone()[0]
        print(f"VPASS 累計: {total} 件")

        await context.close()
    from src.scrape_state import mark_scrape_done
    mark_scrape_done("vpass", count=len(transactions),
                     extra={"saved": new_count})
    return transactions


if __name__ == "__main__":
    asyncio.run(run(headless=False))
