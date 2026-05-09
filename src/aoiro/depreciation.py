"""固定資産台帳 + 減価償却。

対応する method:
- '定額': 月割 (取得月から年度末までの月数で按分)、最終年は簿価1円残し
- '一括': 一括償却資産。取得価額 / 3 を3年定額 (月割なし)
- '少額': 少額減価償却資産 (青色特例、30万円未満)。全額即時償却
- '定率': 簡易対応 (残価 × 償却率)。償却率は耐用年数から table 引き

償却仕訳:
- 借方: 6011 減価償却費 × business_ratio/100
- 貸方: asset.account (建物 / 工具器具備品 等) — 直接法
- 個人事業主は通常直接法。間接法 (減価償却累計額) は採用しない。

冪等: rebuild_depreciation(year) は year の source='depreciation' を全削除 → 再生成。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime


# 耐用年数 → 定率法 償却率 (200%定率法。平成24年4月1日以後取得分)
# 完全な税法表ではないが代表値だけ。簡易計算用。
_DECLINING_RATE = {
    2: 1.000, 3: 0.667, 4: 0.500, 5: 0.400, 6: 0.333, 7: 0.286, 8: 0.250,
    9: 0.222, 10: 0.200, 11: 0.182, 12: 0.167, 13: 0.154, 14: 0.143, 15: 0.133,
    16: 0.125, 17: 0.118, 18: 0.111, 19: 0.105, 20: 0.100, 22: 0.091, 24: 0.083,
    25: 0.080, 30: 0.067, 35: 0.057, 38: 0.053, 40: 0.050, 45: 0.044, 47: 0.043,
    50: 0.040,
}


def _parse_date(s: str) -> datetime:
    s = (s or "").replace("-", "/").strip()
    for fmt in ("%Y/%m/%d", "%Y/%m"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"invalid date: {s}")


# ─────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────
def list_assets(con: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM aoiro_fixed_assets ORDER BY acquired_date, id"
    ).fetchall()]


def upsert_asset(con: sqlite3.Connection, *, id_: int | None, name: str,
                 acquired_date: str, acquired_cost: int, useful_years: int,
                 method: str, account: str, business_ratio: int = 100,
                 salvage_value: int = 0, retired_date: str = "",
                 note: str = "") -> int:
    if method not in ("定額", "定率", "一括", "少額"):
        raise ValueError(f"invalid method: {method}")
    if id_:
        con.execute(
            "UPDATE aoiro_fixed_assets SET name=?, acquired_date=?, acquired_cost=?, "
            "useful_years=?, method=?, account=?, business_ratio=?, salvage_value=?, "
            "retired_date=?, note=? WHERE id=?",
            (name, acquired_date, acquired_cost, useful_years, method, account,
             business_ratio, salvage_value, retired_date or None, note, id_),
        )
        con.commit()
        return id_
    cur = con.execute(
        "INSERT INTO aoiro_fixed_assets (name, acquired_date, acquired_cost, "
        "useful_years, method, account, business_ratio, salvage_value, retired_date, note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (name, acquired_date, acquired_cost, useful_years, method, account,
         business_ratio, salvage_value, retired_date or None, note),
    )
    con.commit()
    return cur.lastrowid


def delete_asset(con: sqlite3.Connection, id_: int) -> bool:
    cur = con.execute("DELETE FROM aoiro_fixed_assets WHERE id=?", (id_,))
    con.commit()
    return cur.rowcount > 0


# ─────────────────────────────────────────────
# 償却スケジュール計算
# ─────────────────────────────────────────────
def schedule(asset: dict) -> list[dict]:
    """資産1件の年度別償却スケジュールを返す。各 row =
    {year, opening_book, depreciation, closing_book, fully_depreciated}。
    """
    method = asset["method"]
    cost = int(asset["acquired_cost"] or 0)
    salvage = int(asset["salvage_value"] or 0)
    useful = int(asset["useful_years"] or 0)
    acq = _parse_date(asset["acquired_date"])
    biz = max(0, min(100, int(asset.get("business_ratio") or 100))) / 100
    retired = None
    if asset.get("retired_date"):
        try:
            retired = _parse_date(asset["retired_date"])
        except ValueError:
            retired = None

    rows: list[dict] = []
    if method == "少額":
        rows.append({
            "year": acq.year, "opening_book": cost,
            "depreciation": int((cost - 1) * biz),
            "closing_book": 1, "fully_depreciated": True,
        })
        return rows

    if method == "一括":
        per_year = (cost - 1) // 3 if cost > 3 else 0  # 3 年で 1/3 ずつ、最終年は簿価1円残し
        book = cost
        for i in range(3):
            y = acq.year + i
            dep = per_year if i < 2 else max(0, book - 1)
            book2 = book - dep
            rows.append({
                "year": y, "opening_book": book,
                "depreciation": int(dep * biz),
                "closing_book": book2, "fully_depreciated": (i == 2),
            })
            book = book2
            if retired and y >= retired.year:
                break
        return rows

    if method == "定額":
        if useful <= 0:
            return rows
        annual = (cost - salvage) / useful  # 月割の前の年間額
        book = cost
        for i in range(useful + 2):
            y = acq.year + i
            if retired and y > retired.year:
                break
            if i == 0:
                # 取得年: 取得月 m から 12 月までの月数 m_cnt
                m_cnt = 12 - acq.month + 1
                dep_raw = annual * m_cnt / 12
            else:
                dep_raw = annual
            dep = int(dep_raw)
            # 最終年: 簿価1円残し (定額法の慣例)
            if book - dep <= max(salvage, 1):
                dep = max(0, book - max(salvage, 1))
            book2 = book - dep
            rows.append({
                "year": y, "opening_book": book,
                "depreciation": int(dep * biz),
                "closing_book": book2,
                "fully_depreciated": (book2 <= max(salvage, 1)),
            })
            book = book2
            if book <= max(salvage, 1):
                break
        return rows

    if method == "定率":
        if useful <= 0:
            return rows
        rate = _DECLINING_RATE.get(useful, 1.0 / useful * 2)
        book = cost
        for i in range(useful + 5):
            y = acq.year + i
            if retired and y > retired.year:
                break
            if i == 0:
                m_cnt = 12 - acq.month + 1
                dep_raw = book * rate * m_cnt / 12
            else:
                dep_raw = book * rate
            dep = int(dep_raw)
            if book - dep <= max(salvage, 1):
                dep = max(0, book - max(salvage, 1))
            book2 = book - dep
            rows.append({
                "year": y, "opening_book": book,
                "depreciation": int(dep * biz),
                "closing_book": book2,
                "fully_depreciated": (book2 <= max(salvage, 1)),
            })
            book = book2
            if book <= max(salvage, 1):
                break
        return rows

    return rows


# ─────────────────────────────────────────────
# 仕訳生成
# ─────────────────────────────────────────────
def rebuild_depreciation(con: sqlite3.Connection, year: int,
                          monthly: bool = True) -> dict:
    """指定年度の source='depreciation' 仕訳を全削除 → 全資産分を再生成。

    monthly=True (デフォルト): 各資産の年間償却額を 12 ヶ月で按分し、
      毎月末に仕訳を計上 (月次 PL の信頼性向上)。
      取得月以降のみ計上 (取得日が 5 月なら 5〜12 月の 8 ヶ月)。
      端数は最後の月に寄せる。
    monthly=False: 旧ロジック (12/31 一括計上)。
    """
    con.execute(
        "DELETE FROM aoiro_journal_entries WHERE fiscal_year=? AND source='depreciation'",
        (year,),
    )
    assets = list_assets(con)
    inserted = 0
    skipped = 0
    for a in assets:
        try:
            sch = schedule(a)
        except ValueError:
            skipped += 1
            continue
        row = next((r for r in sch if r["year"] == year), None)
        if not row or row["depreciation"] <= 0:
            continue
        annual = int(row["depreciation"])
        # 取得年度の場合、取得月から 12 月までの月数で按分
        # (それ以降の年度は 1 月から 12 月)
        try:
            acq = _parse_date(a.get("acquired_date") or "")
        except Exception:
            acq = None
        first_month = 1
        if acq and acq.year == year:
            first_month = acq.month
        # 一括/少額は月按分しない (年一括が会計慣行)
        if a.get("method") in ("一括", "少額") or not monthly:
            con.execute(
                "INSERT INTO aoiro_journal_entries "
                "(fiscal_year, date, debit_account, debit_amount, credit_account, credit_amount, "
                " description, source) VALUES (?,?,?,?,?,?,?,'depreciation')",
                (year, f"{year:04d}/12/31", "6011", annual,
                 a["account"], annual,
                 f"減価償却 [{a['method']}] {a['name']}"),
            )
            inserted += 1
            continue
        # 月按分 (定額/定率)
        n_months = 12 - first_month + 1
        if n_months <= 0:
            continue
        per_month = annual // n_months
        residual = annual - per_month * n_months  # 端数は最後の月へ
        for m in range(first_month, 13):
            amt = per_month + (residual if m == 12 else 0)
            if amt <= 0:
                continue
            # 月末日付 (簡易: 28日にしておく → どの月でも有効)
            con.execute(
                "INSERT INTO aoiro_journal_entries "
                "(fiscal_year, date, debit_account, debit_amount, credit_account, credit_amount, "
                " description, source) VALUES (?,?,?,?,?,?,?,'depreciation')",
                (year, f"{year:04d}/{m:02d}/28", "6011", amt,
                 a["account"], amt,
                 f"減価償却 [{a['method']}] {a['name']} ({m}月分)"),
            )
            inserted += 1
    con.commit()
    return {"year": year, "inserted": inserted, "skipped": skipped,
            "assets": len(assets), "monthly": monthly}


def list_schedules_for_year(con: sqlite3.Connection, year: int) -> list[dict]:
    """全資産について、指定年度の償却額 + 期首/期末簿価を返す (一覧表示用)。"""
    out = []
    for a in list_assets(con):
        try:
            sch = schedule(a)
        except ValueError:
            sch = []
        row = next((r for r in sch if r["year"] == year), None)
        out.append({
            **a,
            "year": year,
            "opening_book": row["opening_book"] if row else None,
            "depreciation": row["depreciation"] if row else 0,
            "closing_book": row["closing_book"] if row else None,
            "fully_depreciated": row["fully_depreciated"] if row else False,
            "schedule": sch,
        })
    return out
