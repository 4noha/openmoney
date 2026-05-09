"""
メルカード（メルペイ翌月払い）明細スクレイパー。

Mercari の既存セッション（.browser_data/mercari）を再利用し、
「購入した商品を定額払いに変更」ページを開いて
get_easypay_convertible_payment_list / get_easypay_repayment_top
API レスポンスを横取りして DB に保存する。

当月・前月分の購入明細を取得し、MUFG 引き落とし行と月次突合する。
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, BrowserContext, Page

load_dotenv()

_JST = timezone(timedelta(hours=9))
_BROWSER_DATA_DIR = Path(__file__).parent.parent.parent / ".browser_data" / "mercari"
DB_PATH = Path(__file__).parent.parent.parent / "transactions.db"

EASYPAY_SELECT_URL = "https://jp.mercari.com/mypage/merpay/smartpayment/easypay/select"


# ─────────────────────────────────────────────
# DB 保存・マイグレーション
# ─────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    from src.db import connect as _connect_central
    con = _connect_central(DB_PATH)
    # メルカード固有テーブル（中央スキーマには無い）
    # mercard_billing: 月次請求サマリ（MUFG引き落とし突合用）
    con.execute("""
        CREATE TABLE IF NOT EXISTS mercard_billing (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            use_month    TEXT NOT NULL UNIQUE,  -- 'YYYY/MM'
            total_amount INTEGER NOT NULL,
            fee_amount   INTEGER NOT NULL DEFAULT 0,
            due_date     TEXT,
            status       TEXT,
            fetched_at   TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    # #19 Phase 5: legacy mercard_mufg_matches / mercard_mufg_tx_links は廃止、
    # tx_links link_type='mercard_mufg' に統一
    con.commit()
    return con


def _to_jst_date(iso: str) -> str:
    """ISO 8601 UTC 文字列 → JST 日付文字列 YYYY/MM/DD"""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_JST)
    return dt.strftime("%Y/%m/%d")


def _save_purchases(purchases: list[dict]) -> int:
    """個別購入明細を bank='メルカード' として transactions に保存"""
    if not purchases:
        return 0
    con = _db()
    saved = 0
    for p in purchases:
        cur = con.execute(
            "INSERT OR IGNORE INTO transactions (bank, date, description, debit, fetched_at) VALUES (?,?,?,?,?)",
            ("メルカード", p["date"], p["title"], p["amount"], datetime.now().isoformat()),
        )
        saved += cur.rowcount
    con.commit()
    con.close()
    return saved


def _save_billing(month: str, total: int, fee: int, due_date: str | None, status: str) -> None:
    """月次請求サマリを mercard_billing に保存"""
    con = _db()
    con.execute("""
        INSERT INTO mercard_billing (use_month, total_amount, fee_amount, due_date, status)
        VALUES (?,?,?,?,?)
        ON CONFLICT(use_month) DO UPDATE SET
            total_amount=excluded.total_amount,
            fee_amount=excluded.fee_amount,
            due_date=excluded.due_date,
            status=excluded.status,
            fetched_at=datetime('now','localtime')
    """, (month, total, fee, due_date, status))
    con.commit()
    con.close()


def _match_mufg(month: str, billed_amount: int) -> None:
    """指定月の MUFG メルペイ引き落とし行と月次請求を突合して mercard_mufg_matches に保存"""
    con = _db()
    year, mon = month.split("/")
    # 翌月の引き落とし（例: 4月利用分 → 5月26日引き落とし）
    next_month = int(mon) % 12 + 1
    next_year = int(year) + (1 if int(mon) == 12 else 0)
    search_month_prefix = f"{next_year}/{next_month:02d}"

    row = con.execute("""
        SELECT id, debit FROM transactions
        WHERE bank = 'MUFG'
          AND date LIKE ? || '%'
          AND (description_normalized LIKE '%メルペイ%' OR description_normalized LIKE '%メルカ%'
               OR description LIKE '%メルペイ%' OR description LIKE '%メルカ%')
        ORDER BY date ASC LIMIT 1
    """, (search_month_prefix,)).fetchone()

    if row:
        diff = billed_amount - row["debit"]
        # tx_links に書込 (#19 Phase 5: legacy mercard_mufg_matches への dual-write 停止)
        from src.matching import _upsert_tx_link
        _upsert_tx_link(
            con,
            tx_a_id=row["id"], tx_b_id=None,
            link_type="mercard_mufg",
            diff=diff,
            extra={"use_month": month, "billed_amount": billed_amount, "mufg_amount": row["debit"]},
        )
        con.commit()
        print(f"[mercard] {month} 突合: 請求¥{billed_amount:,} / MUFG¥{row['debit']:,} (diff={diff:+d})")
    else:
        print(f"[mercard] {month} 対応するMUFG引き落としが見つかりません (翌月={search_month_prefix})")

    con.close()


def _rebuild_tx_links() -> None:
    """MUFG メルペイ引き落とし行 → use_month のマッピングを tx_links に再構築 (#19 Phase 5)。
    legacy mercard_mufg_tx_links は廃止し、tx_links link_type='mercard_mufg' に統一。"""
    from src.matching import _upsert_tx_link
    con = _db()
    mufg_rows = con.execute("""
        SELECT id, date FROM transactions
        WHERE bank='MUFG'
          AND (description LIKE '%メルペイ%' OR description_normalized LIKE '%メルペイ%')
    """).fetchall()
    for row in mufg_rows:
        yr = int(row["date"][:4])
        mo = int(row["date"][5:7])
        use_yr, use_mo = _prev_month(yr, mo)
        use_month = f"{use_yr}/{use_mo:02d}"
        _upsert_tx_link(
            con,
            tx_a_id=row["id"], tx_b_id=None,
            link_type="mercard_mufg",
            extra={"use_month": use_month},
        )
    con.commit()
    con.close()
    print(f"[mercard] tx_links 再構築: {len(mufg_rows)} 件")


# ─────────────────────────────────────────────
# 過去データ補完（MUFG引き落とし + Mercari購入履歴から生成）
# ─────────────────────────────────────────────

def _prev_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def backfill_from_mufg() -> int:
    """
    MUFG の「RTK メルペイ」引き落とし行と Mercari 購入履歴から
    過去の bank='メルカード' トランザクションを補完生成する。

    - API で取得済みの月（mercard_billing に存在する月）はスキップ
    - 26日前後の引き落としを月次請求とみなし、前月の Mercari 購入をコピー
    - Mercari 購入合計 < MUFG 引き落とし の差額は「その他の支払い」として追加
    """
    con = _db()
    saved_total = 0

    # MUFG メルペイ引き落とし行を全取得
    mufg_rows = con.execute("""
        SELECT id, date, debit,
               substr(date,1,4) as yr, CAST(substr(date,6,2) AS INT) as mo,
               CAST(substr(date,9,2) AS INT) as dy
        FROM transactions
        WHERE bank='MUFG'
          AND (description LIKE '%メルペイ%' OR description_normalized LIKE '%メルペイ%')
        ORDER BY date ASC
    """).fetchall()

    # 月次請求（26日±5日）と臨時払い（それ以外）を区別
    # 同じ use_month に複数ある場合は合算する
    billing_by_use_month: dict[str, dict] = {}
    for row in mufg_rows:
        yr, mo, dy = row["yr"], row["mo"], row["dy"]
        use_yr, use_mo = _prev_month(int(yr), mo)
        use_month = f"{use_yr}/{use_mo:02d}"

        if use_month not in billing_by_use_month:
            billing_by_use_month[use_month] = {
                "mufg_tx_id": row["id"],   # 代表行（最初の引き落とし）
                "mufg_date": row["date"],
                "total": 0,
            }
        billing_by_use_month[use_month]["total"] += row["debit"]

    for use_month, info in sorted(billing_by_use_month.items()):
        mufg_amount = info["total"]
        mufg_tx_id  = info["mufg_tx_id"]

        # API 取得済みの月はスキップ
        existing_billing = con.execute(
            "SELECT 1 FROM mercard_billing WHERE use_month=?", (use_month,)
        ).fetchone()
        if existing_billing:
            continue

        # すでにメルカード行がある月もスキップ
        existing_mercard = con.execute(
            "SELECT 1 FROM transactions WHERE bank='メルカード' AND date LIKE ? || '%' LIMIT 1",
            (use_month.replace("/", "/"),)
        ).fetchone()
        if existing_mercard:
            continue

        # Mercari 購入履歴をその月から取得
        mercari_rows = con.execute("""
            SELECT id, date, description, debit FROM transactions
            WHERE bank='Mercari' AND debit>0 AND date LIKE ? || '%'
            ORDER BY date ASC
        """, (use_month,)).fetchall()

        mercari_total = sum(r["debit"] for r in mercari_rows)
        saved = 0

        # Mercari 購入 → メルカード行として複製
        for r in mercari_rows:
            cur = con.execute(
                "INSERT OR IGNORE INTO transactions (bank, date, description, debit, fetched_at) VALUES (?,?,?,?,?)",
                ("メルカード", r["date"], r["description"], r["debit"], datetime.now().isoformat()),
            )
            saved += cur.rowcount

        # 差額（非Mercari の外部加盟店チャージ）を「その他」として追加
        other = mufg_amount - mercari_total
        if other > 100:  # 手数料と思われる小額は無視
            # 月末日付で「その他」エントリ
            other_date = f"{use_month}/28"
            cur = con.execute(
                "INSERT OR IGNORE INTO transactions (bank, date, description, debit, fetched_at) VALUES (?,?,?,?,?)",
                ("メルカード", other_date, "その他の支払い（外部加盟店）", other, datetime.now().isoformat()),
            )
            saved += cur.rowcount

        con.commit()

        # mercard_billing と tx_links に記録 (#19 Phase 5)
        fee_est = max(0, mercari_total - mufg_amount) if mercari_total > mufg_amount else 0
        _save_billing(use_month, mufg_amount, fee_est, None, "BACKFILLED")
        from src.matching import _upsert_tx_link
        _upsert_tx_link(
            con,
            tx_a_id=mufg_tx_id, tx_b_id=None,
            link_type="mercard_mufg", diff=0,
            extra={"use_month": use_month, "billed_amount": mufg_amount,
                   "mufg_amount": mufg_amount},
        )
        con.commit()

        print(f"[mercard] backfill {use_month}: MUFG¥{mufg_amount:,} / Mercari¥{mercari_total:,}"
              f" (other¥{max(0,other):,}) → {saved}件追加")
        saved_total += saved

    con.close()
    _rebuild_tx_links()
    return saved_total


# ─────────────────────────────────────────────
# スクレイピング
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


SMARTPAYMENT_URL = "https://jp.mercari.com/mypage/merpay/smartpayment/repayment/pay"


async def _fetch_mercard_data(page: Page) -> tuple[list[dict], list[dict]]:
    """
    Returns (purchases, billings)
    purchases: [{date, title, amount, use_month}]
    billings:  [{use_month, total_amount, fee_amount, due_date, status}]
    """
    purchases: list[dict] = []
    billings: list[dict] = []

    payment_list_raw: dict | None = None
    all_invoices: list[dict] = []

    async def on_response(resp):
        nonlocal payment_list_raw
        if "get_easypay_convertible_payment_list" in resp.url:
            try:
                payment_list_raw = await resp.json()
            except Exception:
                pass
        elif "get_easypay_repayment_top" in resp.url:
            try:
                data = await resp.json()
                all_invoices.extend(data.get("invoices", []))
            except Exception:
                pass

    page.on("response", on_response)

    # 請求サマリ取得: 当月・前月の repayment ページを巡回
    now = datetime.now(_JST)
    months = []
    if now.month == 1:
        months.append((now.year - 1, 12))
    else:
        months.append((now.year, now.month - 1))
    months.append((now.year, now.month))

    for y, m in months:
        await page.goto(f"{SMARTPAYMENT_URL}/{y}/{m}", wait_until="domcontentloaded")
        await asyncio.sleep(3)

    # 個別購入明細取得（easypay select ページ）
    await page.goto(EASYPAY_SELECT_URL, wait_until="domcontentloaded")
    await asyncio.sleep(4)
    page.remove_listener("response", on_response)

    # 個別購入明細をパース
    if payment_list_raw:
        for monthly in payment_list_raw.get("monthlyConvertiblePayments", []):
            ym = monthly.get("useMonth", {})
            use_month = f"{ym['year']}/{ym['month']:02d}"
            for item in monthly.get("monthlyClearPayments", []):
                if item.get("isCanceled"):
                    continue
                purchases.append({
                    "date": _to_jst_date(item["purchasedAt"]),
                    "title": item["title"],
                    "amount": int(item["amount"]),
                    "use_month": use_month,
                })

    # 月次請求サマリをパース（重複除去）
    seen_months: set[str] = set()
    for inv in all_invoices:
        ym = inv.get("useMonth", {})
        use_month = f"{ym['year']}/{ym['month']:02d}"
        if use_month in seen_months:
            continue
        seen_months.add(use_month)
        total = int(inv.get("totalAmount", 0))
        fee = int(inv.get("totalEasypayFeeAmount", 0))
        sd = inv.get("scheduledDate", {})
        due_date = f"{sd['year']}/{sd['month']:02d}/{sd['day']:02d}" if sd else None
        billings.append({
            "use_month": use_month,
            "total_amount": total,
            "fee_amount": fee,
            "due_date": due_date,
            "status": inv.get("status", ""),
        })

    return purchases, billings


# ─────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────

async def run(headless: bool = True) -> None:
    _BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        ctx = await _launch_context(p, headless)
        page = await ctx.new_page()
        purchases, billings = await _fetch_mercard_data(page)
        await ctx.close()

    print(f"[mercard] 購入明細 {len(purchases)} 件取得")
    for b in billings:
        print(f"  {b['use_month']} 請求: ¥{b['total_amount']:,} (手数料¥{b['fee_amount']:,}) 引落={b['due_date']}")
    for p in purchases:
        print(f"  {p['date']} {p['title'][:30]} ¥{p['amount']:,} ({p['use_month']})")

    saved = _save_purchases(purchases)
    print(f"[mercard] {saved}/{len(purchases)} 件 DB 保存")

    for b in billings:
        _save_billing(b["use_month"], b["total_amount"], b["fee_amount"], b["due_date"], b["status"])
        _match_mufg(b["use_month"], b["total_amount"])

    # 過去分を MUFG引き落とし + Mercari購入履歴から補完
    backfilled = backfill_from_mufg()
    if backfilled:
        print(f"[mercard] backfill: {backfilled} 件補完生成")

    # MUFG tx_id → use_month マッピングを再構築（突合UI用）
    _rebuild_tx_links()

    from src.scrape_state import mark_scrape_done
    mark_scrape_done("mercard", count=len(purchases),
                     extra={"saved": saved, "billings": len(billings)})


if __name__ == "__main__":
    asyncio.run(run(headless=False))
