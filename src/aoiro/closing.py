"""期末整理仕訳 (closing entries) — 個人事業主の慣例:

各事業年度末 (12/31) に下記の振替を行い、事業主借/事業主貸/純利益を
すべて元入金 (3001) に集約する。翌期首の元入金がそのまま事業の純資産
として始まる。

  借方 3003 事業主借 (期末残)  / 貸方 3001 元入金
  借方 3001 元入金              / 貸方 3002 事業主貸 (期末残)

純利益の元入金集約は compute_bs / declaration の equity_total_with_income
で表示用に加算済みのため、ここで仕訳化する必要はない (実務上、決算整理は
申告書 B 集計で完結)。

冪等: rebuild_closing(year) は year の source='closing' を全削除→再生成。
"""
from __future__ import annotations

import sqlite3

from src.aoiro.aggregate import compute_bs


def rebuild_closing(con: sqlite3.Connection, year: int) -> dict:
    """指定年度の期末振替仕訳 (source='closing') を再生成。
    BS の equity_lines から事業主借 / 事業主貸の期末残を取得し、
    元入金 (3001) への振替仕訳を 12/31 付で挿入する。
    """
    con.execute(
        "DELETE FROM aoiro_journal_entries WHERE fiscal_year=? AND source='closing'",
        (year,),
    )
    bs = compute_bs(con, year)
    # 3003 事業主借 ending: 貸方残 (正値で返る)
    # 3002 事業主貸 ending: 借方残 (compute_bs では equity を貸方残基準で
    # 計算するため、借方残はマイナスで返る)
    karikae = 0   # 事業主借 期末残 (貸方残正値)
    kashi = 0     # 事業主貸 期末残 (借方残正値)
    for x in bs.get("equity_lines", []):
        if x["code"] == "3003":
            karikae = max(0, int(x.get("ending") or 0))
        elif x["code"] == "3002":
            kashi = max(0, -int(x.get("ending") or 0))

    inserted = 0
    date = f"{year:04d}/12/31"
    if karikae > 0:
        con.execute(
            "INSERT INTO aoiro_journal_entries "
            "(fiscal_year, date, debit_account, debit_amount, credit_account, credit_amount, "
            " description, source) VALUES (?,?,?,?,?,?,?,'closing')",
            (year, date, "3003", karikae, "3001", karikae,
             f"[期末振替] 事業主借 → 元入金 ¥{karikae:,}"),
        )
        inserted += 1
    if kashi > 0:
        con.execute(
            "INSERT INTO aoiro_journal_entries "
            "(fiscal_year, date, debit_account, debit_amount, credit_account, credit_amount, "
            " description, source) VALUES (?,?,?,?,?,?,?,'closing')",
            (year, date, "3001", kashi, "3002", kashi,
             f"[期末振替] 元入金 → 事業主貸 ¥{kashi:,}"),
        )
        inserted += 1
    con.commit()
    return {
        "year": year,
        "inserted": inserted,
        "karikae_transferred": karikae,
        "kashi_transferred": kashi,
        "net_to_motoirekin": karikae - kashi,  # 元入金への純振替額
    }
