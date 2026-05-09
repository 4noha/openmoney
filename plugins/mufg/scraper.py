import asyncio
import os
import sqlite3
import subprocess
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout

load_dotenv()

LOGIN_URL = "https://directg.s.bk.mufg.jp/APL/LGP_P_01/PU/LG_0001/LG_0001_PC01"
ANSHIN_PASS_BUTTON = "承認完了したのでログイン"
HISTORY_MONTHS = 25
DB_PATH = Path(__file__).parent.parent.parent / "transactions.db"


# ─────────────────────────────────────────────
# ログイン
# ─────────────────────────────────────────────

async def login(page: Page) -> None:
    await page.goto(LOGIN_URL, wait_until="networkidle")
    await page.fill("#tx-contract-number", os.environ["MUFG_ID"])
    await page.fill("#tx-ib-password", os.environ["MUFG_PW"])

    # マスターパスワード未復号で暗号化値が入った場合、ユーザがブラウザで
    # 手動修正してログインできるよう 5 分待機 (一般パスワードマネージャ同様)
    async with page.expect_navigation(wait_until="networkidle", timeout=300_000):
        await page.click("button:has-text('ログイン')")

    current_url = page.url
    print(f"ログイン後URL: {current_url}")

    if ANSHIN_PASS_BUTTON in await page.content():
        await _handle_anshin_pass(page)
        _push_fcm("MUFG ログイン成功", "あんしんパス承認後、明細取得を開始します",
                   data={"type": "login_success", "scraper": "mufg"})
        return

    if "LG_0001_PC01" in current_url:
        await page.screenshot(path="/tmp/mufg_login_failed.png")
        _push_fcm("MUFG ログイン失敗", "ID/PW を確認してください",
                   data={"type": "login_failed", "scraper": "mufg"})
        raise RuntimeError("ログインに失敗しました。ID/PW を確認してください。")

    # ID/PW のみで認証通過 (= あんしんパス不要)
    _push_fcm("MUFG ログイン成功", "明細取得を開始します",
               data={"type": "login_success", "scraper": "mufg"})


async def _handle_anshin_pass(page: Page) -> None:
    print("=" * 50)
    print("MUFGあんしんパス認証が必要です")
    print("iMessage で QR コードを送信します...")
    print("=" * 50)

    await _send_qr_via_s3_imessage(page)

    print("=" * 50)
    print("【操作手順】")
    print("1. iPhoneの三菱UFJ銀行アプリでQRを読み取り承認")
    print("2. ブラウザの「承認完了したのでログイン」を自動クリック or 手動クリック")
    print("3. スクリプトが自動で続きを実行します（最大3分待機）")
    print("=" * 50)

    try:
        input("承認完了後、Enterを押してください... ")
    except EOFError:
        pass

    deadline = asyncio.get_event_loop().time() + 300
    iteration = 0
    while asyncio.get_event_loop().time() < deadline:
        iteration += 1
        if "LG_0001_PC10" not in page.url:
            print(f"承認後URL: {page.url}")
            return

        try:
            await page.bring_to_front()
            btn = page.locator(f"button:has-text('{ANSHIN_PASS_BUTTON}')")
            count = await btn.count()
            visible = await btn.first.is_visible() if count > 0 else False
            print(f"  [{iteration}] url={page.url[-40:]} btn_count={count} visible={visible}")

            if count > 0 and visible:
                try:
                    await btn.first.click(timeout=5_000)
                    print(f"  [{iteration}] click() 成功")
                except Exception as e:
                    print(f"  [{iteration}] click() 失敗: {e}")
                await asyncio.sleep(2)
                if "LG_0001_PC10" not in page.url:
                    print(f"承認後URL（自動クリック）: {page.url}")
                    return
        except Exception as e:
            print(f"  [{iteration}] ループ例外: {e}")

        await asyncio.sleep(3)

    await page.screenshot(path="/tmp/mufg_anshin_timeout.png")
    print("タイムアウト時のスクリーンショット: /tmp/mufg_anshin_timeout.png")
    raise RuntimeError("あんしんパス承認がタイムアウトしました（5分）")


# ─────────────────────────────────────────────
# QR → S3 → iMessage
# ─────────────────────────────────────────────

async def _send_qr_via_s3_imessage(page: Page) -> None:
    qr_path = Path("/tmp/mufg_qr.png")
    await asyncio.sleep(3)

    try:
        qr_el = page.locator("img[alt='qrcode']").first
        await qr_el.wait_for(state="visible", timeout=10_000)
        await qr_el.screenshot(path=str(qr_path))
        print("  QRコードを切り出しました")
    except Exception as e:
        await page.screenshot(path=str(qr_path))
        print(f"  QR切り出し失敗のためページ全体を使用 ({e})")

    # quiet zone (余白) を追加してカメラのフォーカスを取りやすくする
    _pad_qr_image(qr_path, margin=160)

    url = _upload_qr_to_s3(qr_path)
    if url:
        _send_url_via_imessage(url)
    else:
        subprocess.Popen(["open", "-a", "Preview", str(qr_path)])
        print("  S3失敗のため Preview で開きました")

    _notify("QRコードのURLをiMessageで送信しました。承認後ブラウザで「承認完了したのでログイン」を押してください。")


def _pad_qr_image(qr_path: Path, margin: int = 80) -> None:
    """QR コード画像に白い余白 (quiet zone) を追加。
    カメラフォーカスが効きやすくなる。失敗してもログだけ出して進む。
    """
    try:
        from PIL import Image
        img = Image.open(qr_path).convert("RGB")
        w, h = img.size
        new = Image.new("RGB", (w + margin * 2, h + margin * 2), "white")
        new.paste(img, (margin, margin))
        new.save(qr_path)
        print(f"  QR に {margin}px の余白を追加 ({w}x{h} → {new.size[0]}x{new.size[1]})")
    except Exception as e:
        print(f"  QR 余白追加に失敗 (元画像のまま): {e}")


def _upload_qr_to_s3(qr_path: Path) -> str | None:
    bucket = os.environ.get("AWS_S3_BUCKET", "money-forward2-qr")
    region = os.environ.get("AWS_REGION", "ap-northeast-1")
    key = f"qr/{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.png"
    try:
        session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "money-forward2-bot"))
        s3 = session.client("s3", region_name=region)
        s3.upload_file(str(qr_path), bucket, key, ExtraArgs={"ContentType": "image/png"})
        url = s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=300
        )
        print(f"  S3アップロード完了: {bucket}/{key}")
        return url
    except (BotoCoreError, ClientError) as e:
        print(f"  S3アップロード失敗: {e}")
        return None


def _send_url_via_imessage(url: str) -> None:
    recipient = os.environ.get("IMESSAGE_RECIPIENT", "")
    message = f"MUFGあんしんパスQRコード（5分で期限切れ）:\\n{url}"
    script = f'''
tell application "Messages"
    set targetService to 1st service whose service type = iMessage
    set targetBuddy to buddy "{recipient}" of targetService
    send "{message}" to targetBuddy
end tell
'''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  iMessage 送信完了 → {recipient}")
    else:
        print(f"  iMessage 送信失敗: {result.stderr.strip()}")
        _notify(f"QR URL: {url[:80]}...")


def _notify(message: str) -> None:
    """macOS デスクトップ通知。MF2_DESKTOP_NOTIFY=1 のみ発火（既定 OFF）。"""
    print(f"[notify:MUFG] {message}")
    if os.environ.get("MF2_DESKTOP_NOTIFY") == "1":
        script = f'display notification "{message}" with title "MUFGあんしんパス認証" sound name "Ping"'
        subprocess.run(["osascript", "-e", script], capture_output=True)


def _push_fcm(title: str, body: str, data: dict | None = None) -> bool:
    """device_token があれば FCM Push (Android の通知バーに視覚表示)。

    notification + data 両方含む payload で、Android アプリの onMessageReceived
    実装に依存せず OS が直接通知を表示する。失敗しても scrape は続行。
    """
    try:
        from src.fcm import send_visual
        import sqlite3 as _sqlite3
        con = _sqlite3.connect(DB_PATH, timeout=10)
        try:
            row = con.execute(
                "SELECT value FROM daemon_state WHERE key='device_token'"
            ).fetchone()
            token = row[0] if row else None
        finally:
            con.close()
        if not token:
            print("  FCM Push: device_token 未登録 (Android アプリ起動 + 設定保存が必要)")
            return False
        return send_visual(token, title=title, body=body, data=data or {})
    except Exception as e:
        print(f"  FCM Push 失敗: {e}")
        return False


# ─────────────────────────────────────────────
# 明細取得
# ─────────────────────────────────────────────

async def _navigate_to_meisai(page: Page) -> None:
    for selector in [
        "a:has-text('入出金明細')",
        "a:has-text('入出金・明細')",
        "a:has-text('明細照会')",
        "a:has-text('入出金')",
    ]:
        try:
            await page.click(selector, timeout=3_000)
            await page.wait_for_load_state("networkidle")
            return
        except PlaywrightTimeout:
            continue
    raise RuntimeError("入出金明細リンクが見つかりません")


async def _set_date_filter(page: Page, start: date, end: date) -> None:
    """期間を指定して条件を変更する。

    MUFG の form は Angular reactive form。 _navigate_to_meisai 直後だと form の
    bind 初期化と競合して値が反映されない事象があるため、 冒頭で必ず wait する。
    debug 検証で標準 select_option / fill / click() で正常に 2026/03 等の過去明細が
    取れることを確認済み。
    """
    # form 初期化待ち (= ng-pristine から ng-dirty に変える前)
    await page.wait_for_selector("#sl-period", state="visible")
    await page.wait_for_timeout(1_500)

    cur_period = await page.locator("#sl-period").input_value()
    print(f"  [debug] sl-period 変更前 = {cur_period}", flush=True)

    await page.select_option("#sl-period", "5")  # 期間指定
    await page.wait_for_timeout(800)
    after_period = await page.locator("#sl-period").input_value()
    classes_after = await page.locator("#sl-period").get_attribute("class") or ""
    print(f"  [debug] sl-period 変更後 = {after_period} class={classes_after!r}", flush=True)

    date_inputs = await page.locator("input[type='date']").all()
    if len(date_inputs) < 2:
        await page.screenshot(path="/tmp/mufg_date_filter_debug.png")
        raise RuntimeError(
            f"日付入力フィールドが見つかりません (found={len(date_inputs)})"
        )

    for i, (loc, d) in enumerate(zip(date_inputs[:2], [start, end])):
        iso_value = d.strftime("%Y-%m-%d")
        await loc.fill(iso_value)
        await page.wait_for_timeout(300)
        actual = await loc.input_value()
        id_ = await loc.get_attribute("id") or f"[{i}]"
        print(f"    日付入力 {id_}: {iso_value} → 実値={actual}", flush=True)

    await page.select_option("#sl-filter-number", "100")
    await page.wait_for_timeout(300)

    # click 直前の form state
    period_class = await page.locator("#sl-period").get_attribute("class") or ""
    print(f"  [debug] click 直前 sl-period class={period_class!r}", flush=True)

    await page.click("button#bt-inquiry")  # id 指定で確実に「条件を変更」 を click
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(800)

    # デバッグ: 検索後の URL + 「該当なし」 系メッセージの有無
    url_after = page.url
    body_text = await page.inner_text("body")
    no_data_keywords = ("該当なし", "該当する", "明細はありません",
                          "0件", "存在しません", "見つかりません")
    has_no_data_msg = any(kw in body_text for kw in no_data_keywords)
    table_rows = await page.locator("table tr").count()
    print(f"  [debug] 検索後 URL={url_after[-60:]} table 行数={table_rows} 該当なし表示={has_no_data_msg}", flush=True)
    if table_rows < 3 or has_no_data_msg:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = f"/tmp/mufg_empty_{start.strftime('%Y%m%d')}_{ts}.png"
        await page.screenshot(path=path, full_page=True)
        print(f"  [debug] 0 件らしいので screenshot 保存: {path}", flush=True)
        print(f"  [debug] body text (head 500): {body_text[:500]!r}", flush=True)


async def _parse_table(page: Page) -> list[dict]:
    """現在のページのテーブルを解析して取引リストを返す（年ヘッダー対応）"""
    rows = await page.query_selector_all("table tr")
    transactions = []
    current_year = datetime.now().year

    for row in rows:
        cells = await row.query_selector_all("td")

        # 年ヘッダー行 (e.g. "2024年") を検出して年を更新
        if len(cells) == 1:
            text = (await cells[0].inner_text()).strip()
            if "年" in text:
                try:
                    current_year = int(text.replace("年", ""))
                except ValueError:
                    pass
            continue

        if len(cells) < 4:
            continue

        texts = [await c.inner_text() for c in cells]
        date_raw = texts[0].strip()
        if not date_raw or not any(c.isdigit() for c in date_raw):
            continue

        # "M/D" → "YYYY/MM/DD" (ゼロ埋め正規化)
        try:
            dt = datetime.strptime(f"{current_year}/{date_raw}", "%Y/%m/%d")
            date_str = dt.strftime("%Y/%m/%d")
        except ValueError:
            date_str = f"{current_year}/{date_raw}"
        transactions.append({
            "date": date_str,
            "debit": _parse_amount(texts[1]),
            "credit": _parse_amount(texts[2]),
            "description": texts[3].strip().replace("　", " "),
            "balance": _parse_amount(texts[4]) if len(texts) > 4 else "",
            "fetched_at": datetime.now().isoformat(),
        })

    return transactions


async def _fetch_period(page: Page, start: date, end: date) -> list[dict]:
    """指定期間の明細を取得（ページネーション対応）"""
    await _set_date_filter(page, start, end)
    txs = await _parse_table(page)

    # ページネーション
    while True:
        try:
            next_btn = page.locator(
                "button:has-text('次の'), a:has-text('次の'), button:has-text('次へ')"
            ).first
            if await next_btn.count() == 0 or not await next_btn.is_visible():
                break
            await next_btn.click()
            await page.wait_for_load_state("networkidle")
            txs.extend(await _parse_table(page))
        except Exception:
            break

    return txs


async def fetch_all_history(page: Page,
                              force_start: date | None = None,
                              force_end: date | None = None) -> list[dict]:
    """
    DB の状態に応じて差分のみ取得する。
    - force_start / force_end が指定されていればその期間を強制取得 (月別欠落補完用)
    - DB が空 or 過去データ不足 → 最大 HISTORY_MONTHS 月分を月単位バッチ取得
    - 過去データ充足 → 直近データのみ増分取得 (重複3日分)
    """
    today = date.today()
    history_start = today - relativedelta(months=HISTORY_MONTHS)

    oldest_in_db = _get_oldest_mufg_date()
    newest_in_db = _get_newest_mufg_date()

    # 月別欠落を最優先でチェック (= 真ん中の月が空 = MUFG サーバ側の取得抜け)
    missing = (_find_missing_months(history_start, today)
                if oldest_in_db is not None else [])

    if force_start or force_end:
        fetch_start = force_start or (today - relativedelta(months=1))
        fetch_end = force_end or today
        print(f"強制期間取得: {fetch_start} 〜 {fetch_end}")
    elif missing:
        # 月別欠落補完 (例: 2026/03 だけ抜けている)
        fetch_start = missing[0][0]
        fetch_end = missing[-1][1]
        print(f"月別欠落補完: {fetch_start} 〜 {fetch_end} ({len(missing)} ヶ月)")
    elif oldest_in_db is None:
        # 初回: 全期間取得
        fetch_start = history_start
        fetch_end = today
        print(f"初回取得: {fetch_start} 〜 {fetch_end} ({HISTORY_MONTHS}ヶ月)")
    elif oldest_in_db > history_start + timedelta(days=7):
        # 過去データ不足 → 不足分を補完
        fetch_start = history_start
        fetch_end = oldest_in_db - timedelta(days=1)
        print(f"過去データ補完: {fetch_start} 〜 {fetch_end}")
    else:
        # 差分のみ: 最新DB日付の3日前から今日
        fetch_start = (newest_in_db or today) - timedelta(days=3)
        fetch_end = today
        print(f"増分取得: {fetch_start} 〜 {fetch_end}")

    # ログイン後のお知らせ・確認ページを読み飛ばす
    for _ in range(5):
        url = page.url
        if "CM_0000" in url or "CM_RU" in url:
            print(f"  お知らせページをスキップ: {url[-50:]}")
            # 「次へ」「閉じる」「確認」系ボタンを試みる
            skipped = False
            for skip_sel in [
                "button:has-text('次へ')", "button:has-text('閉じる')",
                "button:has-text('確認')", "a:has-text('次へ')",
                "input[type='submit']",
            ]:
                try:
                    btn = page.locator(skip_sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click()
                        await page.wait_for_load_state("networkidle")
                        skipped = True
                        break
                except Exception:
                    continue
            if not skipped:
                # ボタンが見つからなければホームへ直接遷移
                await page.goto("https://directg.s.bk.mufg.jp/", wait_until="networkidle")
            await asyncio.sleep(1)
        else:
            break

    await _navigate_to_meisai(page)

    # 月単位バッチ（100件/ページ制限対策）
    all_txs: list[dict] = []
    cursor = date(fetch_start.year, fetch_start.month, 1)

    while cursor <= fetch_end:
        next_month = cursor + relativedelta(months=1)
        month_end = min(next_month - timedelta(days=1), fetch_end)
        month_start = max(cursor, fetch_start)

        print(f"  {month_start} 〜 {month_end} ...", end=" ", flush=True)
        # navigation エラー (Execution context destroyed) 対策で 2 回リトライ
        last_err = None
        txs: list[dict] = []
        for attempt in range(3):
            try:
                txs = await _fetch_period(page, month_start, month_end)
                break
            except Exception as e:
                last_err = e
                print(f"  [retry {attempt+1}/3] {type(e).__name__}: {str(e)[:80]}",
                      end=" ", flush=True)
                # 明細ページに再 navigate して状態をリセット
                try:
                    await _navigate_to_meisai(page)
                    await asyncio.sleep(2)
                except Exception:
                    pass
        else:
            print(f"  ✗ 取得断念: {last_err}")
        print(f"{len(txs)} 件")
        all_txs.extend(txs)

        cursor = next_month

    print(f"合計 {len(all_txs)} 件取得")
    return all_txs


# ─────────────────────────────────────────────
# DB
# ─────────────────────────────────────────────

def _parse_amount(text: str) -> str:
    return text.strip().replace(",", "").replace("円", "").replace("\xa0", "").strip() or "0"


def _db_connect() -> sqlite3.Connection:
    from src.db import connect as _connect_central
    return _connect_central(DB_PATH)


def _get_oldest_mufg_date() -> date | None:
    try:
        con = _db_connect()
        row = con.execute(
            "SELECT MIN(date) FROM transactions WHERE bank='MUFG'"
        ).fetchone()
        con.close()
        if row and row[0]:
            return datetime.strptime(row[0], "%Y/%m/%d").date()
    except Exception:
        pass
    return None


def _get_newest_mufg_date() -> date | None:
    try:
        con = _db_connect()
        row = con.execute(
            "SELECT MAX(date) FROM transactions WHERE bank='MUFG'"
        ).fetchone()
        con.close()
        if row and row[0]:
            return datetime.strptime(row[0], "%Y/%m/%d").date()
    except Exception:
        pass
    return None


def _find_missing_months(start: date, end: date) -> list[tuple[date, date]]:
    """指定期間内で MUFG transactions が 0 件の月を返す ([(月初, 月末), ...])。

    例: 2026/03 が空 → [(date(2026,3,1), date(2026,3,31))]
    """
    try:
        con = _db_connect()
        rows = con.execute(
            "SELECT substr(date,1,7) AS m, COUNT(*) c FROM transactions "
            "WHERE bank='MUFG' AND date >= ? AND date <= ? GROUP BY m",
            (start.strftime("%Y/%m/%d"), end.strftime("%Y/%m/%d")),
        ).fetchall()
        con.close()
    except Exception:
        return []
    counts = {r[0]: r[1] for r in rows}
    out: list[tuple[date, date]] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        ym = cursor.strftime("%Y/%m")
        if counts.get(ym, 0) == 0:
            next_month = cursor + relativedelta(months=1)
            month_end = min(next_month - timedelta(days=1), end)
            out.append((cursor, month_end))
        cursor = cursor + relativedelta(months=1)
    return out


def save_to_db(transactions: list[dict]) -> int:
    # description_normalized も同時に保存しないと auto_categorize_from_history の
    # 学習ルール (bank, description_normalized) → category がマッチせず、
    # 新規 tx が未分類のまま残る (例: 2026/03 の MUFG 30 件問題)。
    from src.server.categorize import normalize_desc
    con = _db_connect()
    inserted_before = con.execute("SELECT COUNT(*) FROM transactions WHERE bank='MUFG'").fetchone()[0]
    for tx in transactions:
        try:
            con.execute(
                "INSERT OR IGNORE INTO transactions "
                "(bank, date, description, description_normalized, debit, credit, balance, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    "MUFG", tx["date"], tx["description"],
                    normalize_desc(tx["description"]),
                    int(tx["debit"] or 0), int(tx["credit"] or 0),
                    int(tx["balance"] or 0), tx["fetched_at"],
                ),
            )
        except (ValueError, sqlite3.Error) as e:
            print(f"  DB保存スキップ: {tx} ({e})")
    con.commit()
    inserted_after = con.execute("SELECT COUNT(*) FROM transactions WHERE bank='MUFG'").fetchone()[0]
    con.close()
    return inserted_after - inserted_before


# ─────────────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────────────

async def run(headless: bool = False,
                force_start: date | None = None,
                force_end: date | None = None) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()

        await login(page)
        transactions = await fetch_all_history(page,
                                                  force_start=force_start,
                                                  force_end=force_end)

        new_count = save_to_db(transactions)
        print(f"DB保存: {new_count} 件追加 → {DB_PATH}")

        total = _db_connect().execute(
            "SELECT COUNT(*) FROM transactions WHERE bank='MUFG'"
        ).fetchone()[0]
        print(f"MUFG 累計: {total} 件")

        await browser.close()
    from src.scrape_state import mark_scrape_done
    mark_scrape_done("mufg", count=len(transactions))
    return transactions


if __name__ == "__main__":
    asyncio.run(run(headless=False))
