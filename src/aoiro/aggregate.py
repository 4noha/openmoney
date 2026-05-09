"""PL (損益計算書) / BS (貸借対照表) 集計 + 期首残高 CRUD。

仕訳 (aoiro_journal_entries) と勘定科目 (aoiro_accounts) と
期首残高 (aoiro_opening_balances) から年度別の決算サマリを生成。

- PL: 収益 / 費用を集計
- BS: 期首残高 + 当期仕訳 → 期末残高、資産=負債+資本 検算
- 月次内訳: 月別売上・月別経費 (青色申告決算書2枚目用)
"""
from __future__ import annotations

import sqlite3


# ─────────────────────────────────────────────
# 期首残高 CRUD
# ─────────────────────────────────────────────
def list_opening_balances(con: sqlite3.Connection, year: int) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM aoiro_opening_balances WHERE fiscal_year=? ORDER BY account",
        (year,),
    ).fetchall()]


def upsert_opening_balance(con: sqlite3.Connection, *, fiscal_year: int,
                           account: str, debit_balance: int = 0,
                           credit_balance: int = 0) -> None:
    cur = con.execute(
        "SELECT 1 FROM aoiro_opening_balances WHERE fiscal_year=? AND account=?",
        (fiscal_year, account),
    )
    if cur.fetchone():
        con.execute(
            "UPDATE aoiro_opening_balances SET debit_balance=?, credit_balance=? "
            "WHERE fiscal_year=? AND account=?",
            (debit_balance, credit_balance, fiscal_year, account),
        )
    else:
        con.execute(
            "INSERT INTO aoiro_opening_balances "
            "(fiscal_year, account, debit_balance, credit_balance) VALUES (?,?,?,?)",
            (fiscal_year, account, debit_balance, credit_balance),
        )
    con.commit()


def delete_opening_balance(con: sqlite3.Connection, fiscal_year: int, account: str) -> None:
    con.execute(
        "DELETE FROM aoiro_opening_balances WHERE fiscal_year=? AND account=?",
        (fiscal_year, account),
    )
    con.commit()


# ─────────────────────────────────────────────
# 集計ヘルパ
# ─────────────────────────────────────────────
def _account_map(con: sqlite3.Connection) -> dict[str, dict]:
    """code → {name, type, category, sort_order} の dict。"""
    return {r["code"]: dict(r) for r in con.execute(
        "SELECT code, name, type, category, sort_order FROM aoiro_accounts"
    ).fetchall()}


def _entries_summary(con: sqlite3.Connection, year: int) -> dict[str, dict[str, int]]:
    """code → {debit_total, credit_total} を集計。"""
    out: dict[str, dict[str, int]] = {}
    for r in con.execute(
        "SELECT debit_account AS acc, SUM(debit_amount) AS amt FROM aoiro_journal_entries "
        "WHERE fiscal_year=? GROUP BY debit_account", (year,)
    ).fetchall():
        out.setdefault(r["acc"], {"debit_total": 0, "credit_total": 0})["debit_total"] = int(r["amt"] or 0)
    for r in con.execute(
        "SELECT credit_account AS acc, SUM(credit_amount) AS amt FROM aoiro_journal_entries "
        "WHERE fiscal_year=? GROUP BY credit_account", (year,)
    ).fetchall():
        out.setdefault(r["acc"], {"debit_total": 0, "credit_total": 0})["credit_total"] = int(r["amt"] or 0)
    return out


# ─────────────────────────────────────────────
# PL (損益計算書)
# ─────────────────────────────────────────────
def compute_pl(con: sqlite3.Connection, year: int) -> dict:
    """
    青色申告決算書 1枚目 (損益計算書) 用のサマリ。
    - revenue: credit - debit (売上戻し等で借方計上したら相殺)
    - expense: debit - credit
    - 売上原価 = 期首棚卸高 + 仕入高 - 期末棚卸高 (該当科目があれば)
    - 青色申告特別控除前 所得 = 収益合計 - 費用合計
    """
    accs = _account_map(con)
    summ = _entries_summary(con, year)

    revenue_lines: list[dict] = []
    expense_lines: list[dict] = []
    for code, a in accs.items():
        s = summ.get(code, {"debit_total": 0, "credit_total": 0})
        if a["type"] == "revenue":
            amount = s["credit_total"] - s["debit_total"]
            if amount or s["credit_total"] or s["debit_total"]:
                revenue_lines.append({
                    "code": code, "name": a["name"], "category": a["category"],
                    "amount": amount, "sort_order": a["sort_order"],
                })
        elif a["type"] == "expense":
            amount = s["debit_total"] - s["credit_total"]
            if amount or s["debit_total"] or s["credit_total"]:
                expense_lines.append({
                    "code": code, "name": a["name"], "category": a["category"],
                    "amount": amount, "sort_order": a["sort_order"],
                })

    revenue_lines.sort(key=lambda x: (x["sort_order"], x["code"]))
    expense_lines.sort(key=lambda x: (x["sort_order"], x["code"]))

    revenue_total = sum(x["amount"] for x in revenue_lines)
    expense_total = sum(x["amount"] for x in expense_lines)

    # 売上原価 = 期首棚卸 + 仕入 - 期末棚卸 (5001/5002/5003)
    cogs_breakdown = {
        code: next((x["amount"] for x in expense_lines if x["code"] == code), 0)
        for code in ("5001", "5002", "5003")
    }
    cogs = cogs_breakdown["5002"] + cogs_breakdown["5001"] - cogs_breakdown["5003"]

    # 月別売上
    monthly_revenue = _monthly_amounts(con, year, "revenue")
    # 月別仕入 (5001 のみ)
    monthly_purchase = _monthly_account(con, year, "5001", "debit")

    return {
        "year": year,
        "revenue_lines": revenue_lines,
        "expense_lines": expense_lines,
        "revenue_total": revenue_total,
        "expense_total": expense_total,
        "income_before_special": revenue_total - expense_total,
        "cogs_breakdown": cogs_breakdown,
        "cogs_total": cogs,
        "monthly_revenue": monthly_revenue,
        "monthly_purchase": monthly_purchase,
    }


def _monthly_amounts(con: sqlite3.Connection, year: int, type_: str) -> list[dict]:
    """type=revenue なら credit-debit、expense なら debit-credit を月別に。"""
    sign_credit = 1 if type_ == "revenue" else -1
    sign_debit = -sign_credit
    rows = con.execute("""
        SELECT substr(date, 6, 2) AS m,
               SUM(CASE WHEN debit_account IN (SELECT code FROM aoiro_accounts WHERE type=?) THEN debit_amount ELSE 0 END) AS d,
               SUM(CASE WHEN credit_account IN (SELECT code FROM aoiro_accounts WHERE type=?) THEN credit_amount ELSE 0 END) AS c
        FROM aoiro_journal_entries WHERE fiscal_year=?
        GROUP BY m ORDER BY m
    """, (type_, type_, year)).fetchall()
    return [{"month": r["m"], "amount": int(r["c"] or 0) * sign_credit + int(r["d"] or 0) * sign_debit}
            for r in rows]


def _monthly_account(con: sqlite3.Connection, year: int, code: str, side: str) -> list[dict]:
    """特定 account の月別 (debit/credit 指定)。"""
    col = "debit_account" if side == "debit" else "credit_account"
    amt = "debit_amount" if side == "debit" else "credit_amount"
    rows = con.execute(f"""
        SELECT substr(date, 6, 2) AS m, SUM({amt}) AS a
        FROM aoiro_journal_entries WHERE fiscal_year=? AND {col}=?
        GROUP BY m ORDER BY m
    """, (year, code)).fetchall()
    return [{"month": r["m"], "amount": int(r["a"] or 0)} for r in rows]


def compute_monthly_account_balance(con: sqlite3.Connection, year: int,
                                     account: str) -> list[dict]:
    """指定 account の月別 期末残高 (借方残基準で正値) を返す。
    asset 系: opening_debit + Σdebit累計 - Σcredit累計
    liability/equity 系: 同上 (符号は呼び出し側で扱う)

    1〜12 月すべて返す (取引が無い月は前月残を引き継ぎ)。
    """
    # 期首残
    has_opening = con.execute(
        "SELECT 1 FROM aoiro_journal_entries WHERE fiscal_year=? "
        "AND source='opening' LIMIT 1", (year,),
    ).fetchone() is not None
    opening = 0
    if not has_opening:
        ob = con.execute(
            "SELECT debit_balance, credit_balance FROM aoiro_opening_balances "
            "WHERE fiscal_year=? AND account=?", (year, account),
        ).fetchone()
        if ob:
            opening = int(ob["debit_balance"] or 0) - int(ob["credit_balance"] or 0)

    # 月別の借方/貸方額 (auto + opening + closing 全 source)
    monthly = {f"{m:02d}": {"d": 0, "c": 0} for m in range(1, 13)}
    for r in con.execute("""
        SELECT substr(date, 6, 2) AS m,
               SUM(CASE WHEN debit_account=? THEN debit_amount ELSE 0 END) AS d,
               SUM(CASE WHEN credit_account=? THEN credit_amount ELSE 0 END) AS c
        FROM aoiro_journal_entries WHERE fiscal_year=?
        GROUP BY m ORDER BY m
    """, (account, account, year)).fetchall():
        m = r["m"]
        if m in monthly:
            monthly[m] = {"d": int(r["d"] or 0), "c": int(r["c"] or 0)}

    # 累積期末残
    out = []
    ending = opening
    for m in sorted(monthly.keys()):
        ending += monthly[m]["d"] - monthly[m]["c"]
        out.append({
            "month": m,
            "debit": monthly[m]["d"],
            "credit": monthly[m]["c"],
            "ending": ending,
        })
    return out


def compute_monthly_pl(con: sqlite3.Connection, year: int) -> dict:
    """月次 PL: 科目別 12 ヶ月推移。

    Returns:
      {
        "year", "months": ["01"..."12"],
        "revenue_lines": [{code, name, amounts: [m01, ..., m12], total}],
        "expense_lines": [...],
        "monthly_revenue_total": [m01..m12],
        "monthly_expense_total": [m01..m12],
        "monthly_net":           [m01..m12],
      }
    """
    accs = _account_map(con)
    months = [f"{i:02d}" for i in range(1, 13)]

    # 月別の科目ごとの差額 (revenue=credit-debit, expense=debit-credit) を計算
    rows: dict[str, dict] = {}  # code → {amounts: dict(m→amount), name, type}
    for r in con.execute("""
        SELECT substr(date,6,2) AS m,
               debit_account, credit_account,
               SUM(debit_amount) AS d, SUM(credit_amount) AS c
        FROM aoiro_journal_entries WHERE fiscal_year=?
        GROUP BY m, debit_account, credit_account
    """, (year,)).fetchall():
        m = r["m"]
        if not m:
            continue
        for acc_code, side in ((r["debit_account"], "debit"), (r["credit_account"], "credit")):
            a = accs.get(acc_code)
            if not a or a["type"] not in ("revenue", "expense"):
                continue
            rows.setdefault(acc_code, {
                "code": acc_code, "name": a["name"], "type": a["type"],
                "category": a["category"], "sort_order": a["sort_order"],
                "amounts": {mo: 0 for mo in months},
            })
            sign = (
                +1 if (a["type"] == "revenue" and side == "credit") else
                -1 if (a["type"] == "revenue" and side == "debit") else
                +1 if (a["type"] == "expense" and side == "debit") else
                -1
            )
            amt = int((r["d"] if side == "debit" else r["c"]) or 0)
            rows[acc_code]["amounts"][m] = rows[acc_code]["amounts"].get(m, 0) + sign * amt

    rev_lines, exp_lines = [], []
    for acc_code, row in rows.items():
        amounts = [row["amounts"][m] for m in months]
        if not any(amounts):
            continue
        line = {**row, "amounts": amounts, "total": sum(amounts)}
        (rev_lines if row["type"] == "revenue" else exp_lines).append(line)

    rev_lines.sort(key=lambda x: (x["sort_order"], x["code"]))
    exp_lines.sort(key=lambda x: (x["sort_order"], x["code"]))

    monthly_revenue_total = [sum(line["amounts"][i] for line in rev_lines) for i in range(12)]
    monthly_expense_total = [sum(line["amounts"][i] for line in exp_lines) for i in range(12)]
    monthly_net = [r - e for r, e in zip(monthly_revenue_total, monthly_expense_total)]

    return {
        "year": year, "months": months,
        "revenue_lines": rev_lines, "expense_lines": exp_lines,
        "monthly_revenue_total": monthly_revenue_total,
        "monthly_expense_total": monthly_expense_total,
        "monthly_net": monthly_net,
    }


def compute_trial_balance(con: sqlite3.Connection, year: int) -> dict:
    """合計残高試算表: 勘定科目ごとの 期首 / 当期借方計 / 当期貸方計 / 期末 を返す。

    Returns:
        {
          "year": YYYY,
          "rows": [
            {"code", "name", "type", "category",
             "opening_debit", "opening_credit",
             "period_debit", "period_credit",
             "ending_debit", "ending_credit"}, ...
          ],
          "totals": {"period_debit", "period_credit",
                     "ending_debit", "ending_credit"},
          "balance": {"period_diff", "ending_diff"}  # 0 なら平衡
        }
    """
    accs = _account_map(con)
    summ = _entries_summary(con, year)

    has_opening_journal = con.execute(
        "SELECT 1 FROM aoiro_journal_entries WHERE fiscal_year=? AND source='opening' LIMIT 1",
        (year,),
    ).fetchone() is not None
    if has_opening_journal:
        openings: dict[str, dict] = {}
    else:
        openings = {r["account"]: dict(r) for r in con.execute(
            "SELECT * FROM aoiro_opening_balances WHERE fiscal_year=?", (year,)
        ).fetchall()}

    rows = []
    for code, a in accs.items():
        s = summ.get(code, {"debit_total": 0, "credit_total": 0})
        ob = openings.get(code, {"debit_balance": 0, "credit_balance": 0})
        pd = int(s["debit_total"] or 0)
        pc = int(s["credit_total"] or 0)
        od = int(ob["debit_balance"] or 0)
        oc = int(ob["credit_balance"] or 0)
        # 期末残: 借方系科目 (asset/expense) は借方残正、貸方系は貸方残正
        if a["type"] in ("asset", "expense"):
            net = (od - oc) + pd - pc
            ending_debit, ending_credit = (max(0, net), max(0, -net))
        else:  # liability / equity / revenue
            net = (oc - od) + pc - pd
            ending_credit, ending_debit = (max(0, net), max(0, -net))

        # 期首・当期・期末いずれも 0 なら省略
        if not (od or oc or pd or pc or ending_debit or ending_credit):
            continue

        rows.append({
            "code": code, "name": a["name"], "type": a["type"],
            "category": a["category"], "sort_order": a["sort_order"],
            "opening_debit": od, "opening_credit": oc,
            "period_debit": pd, "period_credit": pc,
            "ending_debit": ending_debit, "ending_credit": ending_credit,
        })
    rows.sort(key=lambda x: (x["sort_order"], x["code"]))

    totals = {
        "period_debit":  sum(r["period_debit"]  for r in rows),
        "period_credit": sum(r["period_credit"] for r in rows),
        "ending_debit":  sum(r["ending_debit"]  for r in rows),
        "ending_credit": sum(r["ending_credit"] for r in rows),
    }
    return {
        "year": year,
        "rows": rows,
        "totals": totals,
        "balance": {
            "period_diff":  totals["period_debit"]  - totals["period_credit"],
            "ending_diff":  totals["ending_debit"]  - totals["ending_credit"],
        },
    }


def compute_general_ledger(con: sqlite3.Connection, year: int,
                            account: str) -> dict:
    """元帳 (General Ledger): 指定 account の取引一覧 + 各取引時点の累積残高。

    Returns:
        {
          "year", "account_code", "account_name", "type",
          "opening_debit", "opening_credit", "opening_balance",
          "entries": [{"date", "ref_id", "ref_type", "description",
                       "debit", "credit", "running_balance"}],
          "ending_balance"
        }
        running_balance: 借方系科目は借方残正, 貸方系は貸方残正
    """
    accs = _account_map(con)
    a = accs.get(account)
    if a is None:
        return {"year": year, "account_code": account, "error": "account not found",
                "entries": []}

    has_opening_journal = con.execute(
        "SELECT 1 FROM aoiro_journal_entries WHERE fiscal_year=? AND source='opening' LIMIT 1",
        (year,),
    ).fetchone() is not None
    od = oc = 0
    if not has_opening_journal:
        ob = con.execute(
            "SELECT debit_balance, credit_balance FROM aoiro_opening_balances "
            "WHERE fiscal_year=? AND account=?", (year, account)
        ).fetchone()
        if ob:
            od = int(ob["debit_balance"] or 0)
            oc = int(ob["credit_balance"] or 0)

    is_debit_side = a["type"] in ("asset", "expense")
    if is_debit_side:
        opening_balance = od - oc
    else:
        opening_balance = oc - od

    entries = []
    running = opening_balance
    for r in con.execute("""
        SELECT id, date, debit_account, debit_amount, credit_account, credit_amount,
               description, source
        FROM aoiro_journal_entries
        WHERE fiscal_year=? AND (debit_account=? OR credit_account=?)
        ORDER BY date, id
    """, (year, account, account)).fetchall():
        d = int(r["debit_amount"] or 0) if r["debit_account"] == account else 0
        c = int(r["credit_amount"] or 0) if r["credit_account"] == account else 0
        if is_debit_side:
            running += d - c
        else:
            running += c - d
        entries.append({
            "id": r["id"], "date": r["date"],
            "debit": d, "credit": c,
            "running_balance": running,
            "counter_account": r["credit_account"] if d else r["debit_account"],
            "description": r["description"], "source": r["source"],
        })

    return {
        "year": year,
        "account_code": account, "account_name": a["name"], "type": a["type"],
        "opening_debit": od, "opening_credit": oc,
        "opening_balance": opening_balance,
        "entries": entries,
        "ending_balance": running,
        "is_debit_side": is_debit_side,
    }


# 引落 description に含まれる文字列 → どのカード (bank) の引落かを引く対応表
# (aoiro_journal_entries の description は [MUFG] 口座振替X ミツイスミトモカード のような形式)
_CARD_DEBIT_KEYWORDS = {
    "ミツイスミトモ":      "VPASS",
    "ミツビシＵＦＪニコス": "MUFGAmex",
    "ミツビシUFJニコス":   "MUFGAmex",
    "メルペイ":           "メルカード",
    "オリコ":             "Orico",
    "ＲＫＳ":             "RKS",
    "ポケツトカ":          "ポケットカード",
    "ＤＦ．ペイデイ":       "ペイディ",
    "アクサシユウノウＳ":   "アクサ",
    "ＲＴＫペイペイ":       "PayPay",
}


def _bank_from_description(desc: str | None) -> str | None:
    """description 先頭の `[bank]` から bank 名を抽出。"""
    if not desc:
        return None
    import re
    m = re.match(r"\s*(?:\[家事按分\]\s*)?\[([^\]]+)\]", desc)
    return m.group(1) if m else None


def _card_from_debit_description(desc: str | None) -> str | None:
    """引落仕訳 description (= [MUFG] 口座振替X xxx) から、対応するカード bank を逆引き。"""
    if not desc:
        return None
    for kw, bank in _CARD_DEBIT_KEYWORDS.items():
        if kw in desc:
            return bank
    return None


def compute_card_monthly(con: sqlite3.Connection, year: int) -> dict:
    """カード別 月次推移 (発生 / 引落 / 残債).

    Returns:
        {
          "year": YYYY,
          "cards": [{
            "bank": "VPASS",
            "monthly": [{
              "month": "01",
              "generated": 当月発生 (借方=経費 等 / 貸方=2010 のうち bank 一致),
              "settled":   当月引落 (借方=2010 / 貸方=1002 で description 一致),
              "outstanding": 累積残債 (期首 + 累積発生 - 累積引落)
            }, ...]
          }, ...]
        }
    """
    cards: dict[str, dict[str, dict]] = {}

    # 1) 発生 (credit_account=2010 の仕訳) を bank 別に月次集計
    for r in con.execute("""
        SELECT date, description, credit_amount
        FROM aoiro_journal_entries
        WHERE fiscal_year=? AND credit_account='2010'
    """, (year,)).fetchall():
        bank = _bank_from_description(r["description"])
        if not bank:
            continue
        m = (r["date"] or "")[5:7]
        if not m:
            continue
        cards.setdefault(bank, {})
        cards[bank].setdefault(m, {"generated": 0, "settled": 0})
        cards[bank][m]["generated"] += int(r["credit_amount"] or 0)

    # 2) 引落 (debit_account=2010 / credit_account=1002) を description キーワードで bank 別に
    for r in con.execute("""
        SELECT date, description, debit_amount
        FROM aoiro_journal_entries
        WHERE fiscal_year=? AND debit_account='2010' AND credit_account='1002'
    """, (year,)).fetchall():
        bank = _card_from_debit_description(r["description"])
        if not bank:
            continue
        m = (r["date"] or "")[5:7]
        if not m:
            continue
        cards.setdefault(bank, {})
        cards[bank].setdefault(m, {"generated": 0, "settled": 0})
        cards[bank][m]["settled"] += int(r["debit_amount"] or 0)

    # 3) 期首未払金 (opening_balances or opening 仕訳) ※ bank 別の内訳がないため
    #    全カード合算で持っている → カード別期首は 0 として扱い、月次差から推定。
    # 月次配列を 1〜12 月で展開し残債を累積。
    out_cards = []
    for bank in sorted(cards.keys()):
        monthly = []
        outstanding = 0
        for mi in range(1, 13):
            m = f"{mi:02d}"
            cell = cards[bank].get(m, {"generated": 0, "settled": 0})
            outstanding += cell["generated"] - cell["settled"]
            monthly.append({
                "month": m,
                "generated": cell["generated"],
                "settled": cell["settled"],
                "outstanding": outstanding,
            })
        out_cards.append({
            "bank": bank,
            "year_total_generated": sum(x["generated"] for x in monthly),
            "year_total_settled":   sum(x["settled"]   for x in monthly),
            "ending_outstanding":   monthly[-1]["outstanding"],
            "monthly": monthly,
        })
    # 残債が大きい順に並べる
    out_cards.sort(key=lambda c: -abs(c["ending_outstanding"]))
    return {"year": year, "cards": out_cards}


def compute_monthly_health_check(con: sqlite3.Connection, year: int) -> dict:
    """月別の異常検出: 月末ごとの BS 検算と 2010 借方残。

    Returns:
      {
        "year", "months": [{
          "month", "asset_total", "liability_total", "equity_total",
          "balance_check", "liability_2010_negative"
        }, ...]
      }
    """
    accs = _account_map(con)
    months = [f"{i:02d}" for i in range(1, 13)]

    has_opening = con.execute(
        "SELECT 1 FROM aoiro_journal_entries WHERE fiscal_year=? "
        "AND source='opening' LIMIT 1", (year,),
    ).fetchone() is not None
    openings: dict[str, dict] = {}
    if not has_opening:
        for r in con.execute(
            "SELECT * FROM aoiro_opening_balances WHERE fiscal_year=?", (year,)
        ).fetchall():
            openings[r["account"]] = dict(r)

    # 月別の各 account 累積残を算出 (PL 累計から純利益も計算)
    out_months = []
    cum: dict[str, dict] = {}  # code → {"d": total, "c": total}
    for m in months:
        for r in con.execute("""
            SELECT debit_account AS d_acc, credit_account AS c_acc,
                   SUM(debit_amount) AS d, SUM(credit_amount) AS c
            FROM aoiro_journal_entries
            WHERE fiscal_year=? AND substr(date,6,2)=?
            GROUP BY d_acc, c_acc
        """, (year, m)).fetchall():
            cum.setdefault(r["d_acc"], {"d": 0, "c": 0})["d"] += int(r["d"] or 0)
            cum.setdefault(r["c_acc"], {"d": 0, "c": 0})["c"] += int(r["c"] or 0)
        asset_total = liab_total = equity_total = 0
        revenue_total = expense_total = 0
        liab_2010_neg = 0
        for code, a in accs.items():
            t = a["type"]
            s = cum.get(code, {"d": 0, "c": 0})
            ob = openings.get(code, {"debit_balance": 0, "credit_balance": 0})
            if t == "asset":
                opening = int(ob["debit_balance"] or 0) - int(ob["credit_balance"] or 0)
                ending = opening + s["d"] - s["c"]
                asset_total += ending
            elif t == "liability":
                opening = int(ob["credit_balance"] or 0) - int(ob["debit_balance"] or 0)
                ending = opening + s["c"] - s["d"]
                liab_total += ending
                if code == "2010" and ending < 0:
                    liab_2010_neg = -ending
            elif t == "equity":
                opening = int(ob["credit_balance"] or 0) - int(ob["debit_balance"] or 0)
                ending = opening + s["c"] - s["d"]
                equity_total += ending
            elif t == "revenue":
                revenue_total += s["c"] - s["d"]
            elif t == "expense":
                expense_total += s["d"] - s["c"]
        net_income = revenue_total - expense_total
        equity_total_with_income = equity_total + net_income
        balance_check = asset_total - liab_total - equity_total_with_income
        out_months.append({
            "month": m,
            "asset_total": asset_total,
            "liability_total": liab_total,
            "equity_total": equity_total,
            "net_income": net_income,
            "equity_total_with_income": equity_total_with_income,
            "balance_check": balance_check,
            "liability_2010_negative": liab_2010_neg,
        })
    return {"year": year, "months": out_months}


def compute_monthly_bs(con: sqlite3.Connection, year: int) -> dict:
    """月次 BS: 各資産/負債/資本科目の月末残高 (12 ヶ月)。

    asset 系は借方残正、liability/equity 系は貸方残正で表示する。
    """
    accs = _account_map(con)
    months = [f"{i:02d}" for i in range(1, 13)]
    rows = []
    for code, a in accs.items():
        if a["type"] not in ("asset", "liability", "equity"):
            continue
        monthly = compute_monthly_account_balance(con, year, code)
        amounts = [m["ending"] for m in monthly]
        # opening_journal がある年度: 期首が 0、当期仕訳のみで計算 (= 既に正しい)
        # opening_journal がない年度: opening_balances から期首を読み込み済み
        if a["type"] in ("liability", "equity"):
            display = [-x for x in amounts]  # 貸方残正に
        else:
            display = amounts
        if not any(display):
            continue
        rows.append({
            "code": code, "name": a["name"], "type": a["type"],
            "category": a["category"], "sort_order": a["sort_order"],
            "amounts": display, "ending": display[-1],
        })
    # type でグループ化されるよう asset → liability → equity の順
    type_order = {"asset": 0, "liability": 1, "equity": 2}
    rows.sort(key=lambda x: (type_order.get(x["type"], 9), x["sort_order"], x["code"]))
    return {"year": year, "months": months, "rows": rows}


def compute_monthly_bank_balance(con: sqlite3.Connection, year: int,
                                   bank: str = "MUFG") -> list[dict]:
    """transactions テーブルの月末 balance を返す。各月の最後の取引の balance を採用。
    取引が無い月は前月の balance を引き継ぐ。
    """
    yyyy = f"{year:04d}"
    raw = {}
    for r in con.execute("""
        SELECT substr(date, 6, 2) AS m, MAX(id) AS max_id
        FROM transactions
        WHERE bank=? AND substr(date,1,4)=? AND balance IS NOT NULL
        GROUP BY m
    """, (bank, yyyy)).fetchall():
        b = con.execute(
            "SELECT balance FROM transactions WHERE id=?", (r["max_id"],)
        ).fetchone()
        if b and b["balance"] is not None:
            raw[r["m"]] = int(b["balance"])

    out = []
    last = None
    for m in (f"{i:02d}" for i in range(1, 13)):
        if m in raw:
            last = raw[m]
        out.append({"month": m, "balance": last})
    return out


# ─────────────────────────────────────────────
# BS (貸借対照表)
# ─────────────────────────────────────────────
def compute_bs(con: sqlite3.Connection, year: int) -> dict:
    """
    期首残高 + 当期仕訳 → 期末残高。
    資産: opening_debit + Σdebit - Σcredit
    負債/資本: opening_credit + Σcredit - Σdebit
    """
    accs = _account_map(con)
    summ = _entries_summary(con, year)
    # source='opening' 仕訳がある年度は期首残として既に当期仕訳合計に含まれているため、
    # aoiro_opening_balances を二重計上しないよう無視する。
    has_opening_journal = con.execute(
        "SELECT 1 FROM aoiro_journal_entries WHERE fiscal_year=? AND source='opening' LIMIT 1",
        (year,),
    ).fetchone() is not None
    if has_opening_journal:
        openings: dict[str, dict] = {}
    else:
        openings = {r["account"]: dict(r) for r in con.execute(
            "SELECT * FROM aoiro_opening_balances WHERE fiscal_year=?", (year,)
        ).fetchall()}

    asset_lines: list[dict] = []
    liability_lines: list[dict] = []
    equity_lines: list[dict] = []
    for code, a in accs.items():
        if a["type"] not in ("asset", "liability", "equity"):
            continue
        s = summ.get(code, {"debit_total": 0, "credit_total": 0})
        ob = openings.get(code, {"debit_balance": 0, "credit_balance": 0})
        if a["type"] == "asset":
            opening = int(ob["debit_balance"] or 0) - int(ob["credit_balance"] or 0)
            ending = opening + s["debit_total"] - s["credit_total"]
            target = asset_lines
        else:  # liability / equity は貸方残
            opening = int(ob["credit_balance"] or 0) - int(ob["debit_balance"] or 0)
            ending = opening + s["credit_total"] - s["debit_total"]
            target = liability_lines if a["type"] == "liability" else equity_lines
        if opening == 0 and ending == 0:
            continue
        target.append({
            "code": code, "name": a["name"], "category": a["category"],
            "opening": opening, "ending": ending,
            "debit_total": s["debit_total"], "credit_total": s["credit_total"],
            "sort_order": a["sort_order"],
        })

    for lst in (asset_lines, liability_lines, equity_lines):
        lst.sort(key=lambda x: (x["sort_order"], x["code"]))

    asset_total = sum(x["ending"] for x in asset_lines)
    liability_total = sum(x["ending"] for x in liability_lines)
    equity_total = sum(x["ending"] for x in equity_lines)

    # 当期純利益 (PL から)
    pl = compute_pl(con, year)
    net_income = pl["income_before_special"]
    equity_total_with_income = equity_total + net_income

    return {
        "year": year,
        "asset_lines": asset_lines,
        "liability_lines": liability_lines,
        "equity_lines": equity_lines,
        "asset_total": asset_total,
        "liability_total": liability_total,
        "equity_total": equity_total,
        "net_income": net_income,
        "equity_total_with_income": equity_total_with_income,
        "balance_check": asset_total - (liability_total + equity_total_with_income),
    }
