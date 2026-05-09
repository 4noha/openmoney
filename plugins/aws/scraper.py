"""AWS 請求 scraper (= 請求専用 IAM ユーザの Cost Explorer API)。

Console ログイン + PDF DL ではなく、 IAM ユーザに ce:GetCostAndUsage 権限を
与えて API で月次支出を自動取得 → transactions に bank='AWS' で記録。
発行事業者は「Amazon Web Services Japan G.K. (T1010001113295)」 で適格請求書
発行事業者なので、 receipts.invoice_number に T 番号を保存して /invoices に
表示される (= 仕入税額控除対象)。

ローカルに DL 済の PDF (= invoices/aws/...) は別 UI で receipts.filename に
紐付けることを想定 (= /api/receipts の 「ローカル PDF 選択 UI」 が完成すれば
自動マッチも可)。

env:
  AWS_ACCESS_KEY_ID      : 請求専用 IAM ユーザの アクセスキー
  AWS_SECRET_ACCESS_KEY  : 同上 シークレット
  AWS_REGION             : リージョン (= 既定 us-east-1、 Cost Explorer は
                           グローバル API なのでどこでもいい)

IAM ポリシー例 (= 最小権限):
  {
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["ce:GetCostAndUsage"],
      "Resource": "*"
    }]
  }
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "transactions.db"

_BANK = "AWS"
_MERCHANT = "アマゾン ウェブ サービス ジャパン合同会社"
# AWS Japan G.K. の適格請求書発行事業者番号 (= 公開情報)
_INVOICE_NUMBER = "T1010001113295"


def _fetch_monthly_cost(months: int = 24) -> list[dict]:
    """過去 N ヶ月の月次 cost (= USD or JPY) を取得。 各 dict は
    {"date": "YYYY/MM/DD" (= 月末日), "amount": int, "currency": str}。

    AWS Cost Explorer API は通常 USD 返答。 Japan 法人で billing が JPY の場合、
    AWS_BILLING_CURRENCY=JPY 環境変数で JPY 取得可能。
    """
    import boto3
    region = os.environ.get("AWS_REGION", "us-east-1")
    client = boto3.client(
        "ce",
        region_name=region,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    )

    today = datetime.utcnow().date()
    # Cost Explorer の月次は当月終了月の翌月 1 日を End に指定
    start = (today.replace(day=1) - timedelta(days=months * 31)).replace(day=1)
    end = today.replace(day=1)

    response = client.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY",
        Metrics=["BlendedCost"],
    )

    rows: list[dict] = []
    for r in response.get("ResultsByTime", []):
        period_start = r["TimePeriod"]["Start"]
        amt = r["Total"]["BlendedCost"]["Amount"]
        unit = r["Total"]["BlendedCost"]["Unit"]
        try:
            f = float(amt)
        except ValueError:
            continue
        if f <= 0:
            continue
        # USD ならセント単位、 JPY なら円
        if unit == "USD":
            amount_int = int(round(f * 100))
        else:
            amount_int = int(round(f))
        # 月末日 (= 翌月 1 日 - 1 日)
        y, mo, _ = period_start.split("-")
        from calendar import monthrange
        last_day = monthrange(int(y), int(mo))[1]
        rows.append({
            "date": f"{int(y):04d}/{int(mo):02d}/{last_day:02d}",
            "amount": amount_int,
            "currency": unit,
        })
    return rows


def _save_to_db(rows: list[dict]) -> int:
    if not rows:
        return 0
    from src.db import connect as _connect_central
    from src.server.categorize import normalize_desc
    con = _connect_central(DB_PATH)
    inserted = 0
    receipt_added = 0
    for r in rows:
        currency = r.get("currency", "USD")
        ym = r["date"][:7]
        if currency == "JPY":
            desc = f"AWS 利用料 ({ym})"
        else:
            desc = f"AWS 利用料 ({currency}) ({ym})"
        try:
            cur = con.execute(
                "INSERT OR IGNORE INTO transactions "
                "(bank, date, description, description_normalized, debit, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_BANK, r["date"], desc, normalize_desc(desc),
                 int(r["amount"]), r["fetched_at"]),
            )
            inserted += cur.rowcount
            tx_row = con.execute(
                "SELECT id FROM transactions WHERE bank=? AND date=? AND description=?",
                (_BANK, r["date"], desc),
            ).fetchone()
            if not tx_row:
                continue
            tx_id = tx_row[0]
            existing = con.execute(
                "SELECT id, invoice_number FROM receipts WHERE merchant=? AND transaction_id=?",
                (_MERCHANT, tx_id),
            ).fetchone()
            if existing:
                rid, existing_inv = existing
                if not existing_inv:
                    con.execute(
                        "UPDATE receipts SET invoice_number=? WHERE id=?",
                        (_INVOICE_NUMBER, rid),
                    )
                con.execute(
                    "INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id) VALUES (?, ?)",
                    (rid, tx_id),
                )
                continue
            cur = con.execute(
                "INSERT INTO receipts "
                "(filename, receipt_date, merchant, invoice_number, amount, transaction_id, saved_at) "
                "VALUES (NULL, ?, ?, ?, ?, ?, ?)",
                (r["date"], _MERCHANT, _INVOICE_NUMBER, int(r["amount"]),
                 tx_id, r["fetched_at"]),
            )
            rid = cur.lastrowid
            con.execute(
                "INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id) VALUES (?, ?)",
                (rid, tx_id),
            )
            receipt_added += 1
        except (ValueError, sqlite3.Error) as e:
            print(f"  DB保存スキップ: {r} ({e})")
    con.commit()
    con.close()
    if receipt_added:
        print(f"  receipt: +{receipt_added} 件 (= T{_INVOICE_NUMBER[1:]} 付与)")
    return inserted


def _link_vpass_aws() -> int:
    """VPASS の "AWS" "AMAZON WEB SERVICES" 行に AWS plugin の receipt を流用 link。"""
    from src.db import connect as _connect_central
    con = _connect_central(DB_PATH)
    before = con.execute("SELECT COUNT(*) FROM receipt_links").fetchone()[0]
    con.execute("""
        WITH match AS (
            SELECT v.id AS v_tx_id, r.id AS receipt_id
            FROM transactions v
            JOIN transactions a
              ON a.bank = 'AWS'
             AND substr(a.date, 1, 7) = substr(v.date, 1, 7)
            JOIN receipts r ON r.transaction_id = a.id AND r.invoice_number = ?
            WHERE v.bank = 'VPASS'
              AND (v.description LIKE '%AWS%' OR v.description LIKE '%AMAZON WEB SERVICES%')
        )
        INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id)
        SELECT receipt_id, v_tx_id FROM match
    """, (_INVOICE_NUMBER,))
    after = con.execute("SELECT COUNT(*) FROM receipt_links").fetchone()[0]
    con.commit()
    con.close()
    n = after - before
    if n > 0:
        print(f"  VPASS AWS link: +{n} 件")
    return n


async def run(*, months: int = 12, dry_run: bool = False) -> list[dict]:
    if not os.environ.get("AWS_ACCESS_KEY_ID") or not os.environ.get("AWS_SECRET_ACCESS_KEY"):
        print("[aws] AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY 未設定")
        return []

    rows = await asyncio.get_event_loop().run_in_executor(
        None, _fetch_monthly_cost, months,
    )
    now = datetime.now().isoformat()
    for r in rows:
        r["fetched_at"] = now

    if dry_run:
        print(f"[aws] dry_run: {len(rows)} 件")
        for r in rows:
            print(f"  {r}")
        return rows

    n = _save_to_db(rows)
    linked = _link_vpass_aws()
    shop_link = 0
    try:
        from src.matching import run_shop_matching
        matches = run_shop_matching()
        shop_link = sum(1 for m in matches if m.shop_bank == "AWS")
    except Exception as e:
        print(f"  shop_matching 失敗: {e}")
    try:
        from src.server import _invalidate_tx_cache
        _invalidate_tx_cache()
    except Exception:
        pass
    print(f"[aws] {len(rows)} 件取得 / DB +{n} / VPASS link +{linked} / shop_card {shop_link}")
    return rows


if __name__ == "__main__":
    asyncio.run(run(dry_run=True))
