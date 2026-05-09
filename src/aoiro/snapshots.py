"""年度別 snapshot — 全 section を 1 枚の JSON に凍結して
aoiro_declaration_runs.snapshot_json に保存。

snapshot_full() で集約 → save_snapshot_full() で永続化。
get_snapshot_full() / list_snapshots() で過去年度を閲覧。
lock 中は変更拒否。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from src.aoiro.aggregate import (
    compute_bs,
    compute_pl,
    list_opening_balances,
)
from src.aoiro.business_ratios import list_business_ratios
from src.aoiro.deductions import (
    list_insurance_transactions,
    list_medical_transactions,
    yearly_summary as _ded_summary,
)
from src.aoiro.depreciation import list_schedules_for_year
from src.aoiro.engine import list_journal
from src.aoiro.forms import compute_declaration
from src.aoiro.real_estate import (
    list_landlord_transactions,
    list_properties,
    list_rents,
    yearly_summary as _re_summary,
)
from src.aoiro.rental_contracts import (
    annual_rent_total as _rental_summary,
    list_contracts as list_rental_contracts,
)
from src.aoiro.accounts import list_accounts
from src.aoiro.rules import list_payment_accounts, list_rules


def snapshot_full(con: sqlite3.Connection, year: int, *,
                  declaration_params: dict | None = None) -> dict:
    """指定年度の全 section を集約した snapshot dict を返す。永続化はしない。
    declaration_params は compute_declaration の引数 (給与・特控・源泉・etc)。
    """
    pl = compute_pl(con, year)
    bs = compute_bs(con, year)
    journal = list_journal(con, year=year)
    assets = list_schedules_for_year(con, year)
    re_props = list_properties(con)
    re_rents = list_rents(con, year=year)
    re_sum = _re_summary(con, year)
    landlord_tx = list_landlord_transactions(con, year)
    ded = _ded_summary(con, year)
    medical_tx = list_medical_transactions(con, year)
    insurance_tx = list_insurance_transactions(con, year)
    opening = list_opening_balances(con, year)
    biz_ratios = list_business_ratios(con, fiscal_year=year)
    rules = list_rules(con)
    payments = list_payment_accounts(con)
    rental_contracts = list_rental_contracts(con, fiscal_year=year)
    rental_sum = _rental_summary(con, fiscal_year=year)
    accounts = list_accounts(con)  # 勘定科目マスタも凍結 (年度ごとに変わる可能性)
    decl = compute_declaration(con, year, **(declaration_params or {}))

    return {
        "fiscal_year": year,
        "captured_at": datetime.now().isoformat(),
        "pl": pl,
        "bs": bs,
        "journal": {"entries": journal, "count": len(journal)},
        "fixed_assets": {"assets": assets, "count": len(assets)},
        "real_estate": {
            "summary": re_sum,
            "properties": re_props,
            "rents": re_rents,
            "landlord_transactions": landlord_tx,
        },
        "deductions": ded,
        "medical_transactions": medical_tx,
        "insurance_transactions": insurance_tx,
        "opening_balances": opening,
        "business_ratios": biz_ratios,
        "rules_used": rules,
        "payment_accounts": payments,
        "accounts": accounts,
        "rental_contracts": {
            "contracts": rental_contracts,
            "summary": rental_sum,
        },
        "declaration": decl,
    }


def save_snapshot_full(con: sqlite3.Connection, year: int, *,
                       declaration_params: dict | None = None,
                       lock: bool = False) -> dict:
    """snapshot を生成して aoiro_declaration_runs に upsert。
    lock=True で freeze。既に locked のものは更新拒否 (ValueError)。
    """
    cur = con.execute(
        "SELECT locked_at FROM aoiro_declaration_runs WHERE fiscal_year=?", (year,)
    )
    row = cur.fetchone()
    if row and row["locked_at"]:
        raise ValueError(f"snapshot {year} is locked at {row['locked_at']}")

    snap = snapshot_full(con, year, declaration_params=declaration_params)
    snap_str = json.dumps(snap, ensure_ascii=False)
    pl_str = json.dumps(snap["pl"], ensure_ascii=False)
    bs_str = json.dumps(snap["bs"], ensure_ascii=False)
    decl_str = json.dumps(snap["declaration"], ensure_ascii=False)
    now = datetime.now().isoformat()
    locked_at = now if lock else None

    if row:
        con.execute(
            "UPDATE aoiro_declaration_runs SET snapshot_json=?, "
            "pl_json=?, bs_json=?, declaration_json=?, "
            "locked_at=?, updated_at=? WHERE fiscal_year=?",
            (snap_str, pl_str, bs_str, decl_str, locked_at, now, year),
        )
    else:
        con.execute(
            "INSERT INTO aoiro_declaration_runs "
            "(fiscal_year, snapshot_json, pl_json, bs_json, declaration_json, "
            " locked_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (year, snap_str, pl_str, bs_str, decl_str, locked_at, now),
        )
    con.commit()
    return get_snapshot_full(con, year)


def get_snapshot_full(con: sqlite3.Connection, year: int) -> dict | None:
    cur = con.execute(
        "SELECT fiscal_year, locked_at, created_at, updated_at, snapshot_json "
        "FROM aoiro_declaration_runs WHERE fiscal_year=?", (year,)
    )
    row = cur.fetchone()
    if not row:
        return None
    out: dict = dict(row)
    if out.get("snapshot_json"):
        try:
            out["snapshot"] = json.loads(out["snapshot_json"])
        except json.JSONDecodeError:
            out["snapshot"] = None
        del out["snapshot_json"]
    else:
        out["snapshot"] = None
    return out


def list_snapshots(con: sqlite3.Connection) -> list[dict]:
    """snapshot 一覧 (軽量 — snapshot_json は含まない)。
    summary 表示用に主要な KPI (income/tax) を declaration_json から抽出して返す。
    """
    out = []
    for r in con.execute(
        "SELECT fiscal_year, locked_at, created_at, updated_at, "
        "       declaration_json, snapshot_json IS NOT NULL AS has_snapshot "
        "FROM aoiro_declaration_runs ORDER BY fiscal_year DESC"
    ).fetchall():
        d = dict(r)
        kpis: dict = {}
        if d.get("declaration_json"):
            try:
                decl = json.loads(d["declaration_json"])
                kpis = {
                    "income_total":     (decl.get("income") or {}).get("total"),
                    "deduction_total":  decl.get("deduction_total"),
                    "taxable_income":   decl.get("taxable_income"),
                    "final_payable":    (decl.get("tax") or {}).get("final_payable"),
                    "final_refund":     (decl.get("tax") or {}).get("final_refund"),
                }
            except json.JSONDecodeError:
                pass
        out.append({
            "fiscal_year": d["fiscal_year"],
            "locked_at": d["locked_at"],
            "created_at": d["created_at"],
            "updated_at": d["updated_at"],
            "has_snapshot": bool(d.get("has_snapshot")),
            "kpis": kpis,
        })
    return out


def lock_snapshot(con: sqlite3.Connection, year: int) -> bool:
    cur = con.execute(
        "UPDATE aoiro_declaration_runs SET locked_at=? WHERE fiscal_year=?",
        (datetime.now().isoformat(), year),
    )
    con.commit()
    return cur.rowcount > 0


def unlock_snapshot(con: sqlite3.Connection, year: int) -> bool:
    cur = con.execute(
        "UPDATE aoiro_declaration_runs SET locked_at=NULL WHERE fiscal_year=?",
        (year,),
    )
    con.commit()
    return cur.rowcount > 0


def delete_snapshot_full(con: sqlite3.Connection, year: int) -> bool:
    cur = con.execute("SELECT locked_at FROM aoiro_declaration_runs WHERE fiscal_year=?", (year,))
    row = cur.fetchone()
    if row and row["locked_at"]:
        raise ValueError(f"snapshot {year} is locked")
    cur = con.execute("DELETE FROM aoiro_declaration_runs WHERE fiscal_year=?", (year,))
    con.commit()
    return cur.rowcount > 0


def get_section(con: sqlite3.Connection, year: int, kind: str) -> dict | None:
    """snapshot から特定 section だけ取得。kind:
    pl | bs | journal | fixed_assets | real_estate | deductions |
    declaration | opening_balances | business_ratios | rules_used |
    payment_accounts | user_settings"""
    s = get_snapshot_full(con, year)
    if not s or not s.get("snapshot"):
        return None
    snap = s["snapshot"]
    if kind not in snap:
        return None
    return {
        "fiscal_year": year,
        "locked_at": s["locked_at"],
        "captured_at": snap.get("captured_at"),
        kind: snap[kind],
    }


# ─────────────────────────────────────────────
# 比較 (2 年度)
# ─────────────────────────────────────────────
def compare_snapshots(con: sqlite3.Connection, year_a: int, year_b: int) -> dict:
    sa = get_snapshot_full(con, year_a)
    sb = get_snapshot_full(con, year_b)
    if not sa or not sa.get("snapshot"):
        raise ValueError(f"snapshot {year_a} not found")
    if not sb or not sb.get("snapshot"):
        raise ValueError(f"snapshot {year_b} not found")
    a = sa["snapshot"]; b = sb["snapshot"]

    def _line_diff(la: list[dict], lb: list[dict], code_key: str = "code") -> list[dict]:
        ma = {x[code_key]: x for x in la}
        mb = {x[code_key]: x for x in lb}
        keys = sorted(set(ma) | set(mb))
        out = []
        for k in keys:
            xa = ma.get(k); xb = mb.get(k)
            amt_a = (xa or {}).get("amount", 0)
            amt_b = (xb or {}).get("amount", 0)
            out.append({
                "code": k,
                "name": (xa or xb or {}).get("name"),
                "amount_a": amt_a, "amount_b": amt_b,
                "diff": amt_b - amt_a,
            })
        return out

    return {
        "year_a": year_a, "year_b": year_b,
        "pl": {
            "revenue": _line_diff(a["pl"]["revenue_lines"], b["pl"]["revenue_lines"]),
            "expense": _line_diff(a["pl"]["expense_lines"], b["pl"]["expense_lines"]),
            "totals": {
                "revenue": [a["pl"]["revenue_total"], b["pl"]["revenue_total"]],
                "expense": [a["pl"]["expense_total"], b["pl"]["expense_total"]],
                "income":  [a["pl"]["income_before_special"], b["pl"]["income_before_special"]],
            },
        },
        "bs": {
            "asset":     _line_diff_with(a["bs"]["asset_lines"], b["bs"]["asset_lines"]),
            "liability": _line_diff_with(a["bs"]["liability_lines"], b["bs"]["liability_lines"]),
            "equity":    _line_diff_with(a["bs"]["equity_lines"], b["bs"]["equity_lines"]),
            "totals": {
                "asset":     [a["bs"]["asset_total"], b["bs"]["asset_total"]],
                "liability": [a["bs"]["liability_total"], b["bs"]["liability_total"]],
                "equity":    [a["bs"]["equity_total_with_income"], b["bs"]["equity_total_with_income"]],
            },
        },
        "declaration": {
            "income_total":   [a["declaration"]["income"]["total"], b["declaration"]["income"]["total"]],
            "deduction":      [a["declaration"]["deduction_total"], b["declaration"]["deduction_total"]],
            "taxable_income": [a["declaration"]["taxable_income"], b["declaration"]["taxable_income"]],
            "final_payable":  [a["declaration"]["tax"]["final_payable"], b["declaration"]["tax"]["final_payable"]],
        },
    }


def _line_diff_with(la: list[dict], lb: list[dict]) -> list[dict]:
    """BS 用: ending を比較。"""
    ma = {x["code"]: x for x in la}
    mb = {x["code"]: x for x in lb}
    keys = sorted(set(ma) | set(mb))
    out = []
    for k in keys:
        xa = ma.get(k); xb = mb.get(k)
        end_a = (xa or {}).get("ending", 0)
        end_b = (xb or {}).get("ending", 0)
        out.append({
            "code": k,
            "name": (xa or xb or {}).get("name"),
            "ending_a": end_a, "ending_b": end_b,
            "diff": end_b - end_a,
        })
    return out
