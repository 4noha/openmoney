"""申告書 B 第一表の値集計 + 申告 snapshot (aoiro_declaration_runs) の管理。

Phase 7 では税額計算 (申告書 B 第三表 / 復興特別所得税 / 住民税) は行わず、
所得・控除の集計までで停止。税額計算は Phase 8 で。

snapshot:
- 年度ごとに 1 レコード (aoiro_declaration_runs.fiscal_year が PK)
- locked_at が NULL の間は何度でも上書き、locked 後は変更拒否
- pl_json / bs_json / declaration_json / exports_json に各エンドポイントの
  返り値 JSON を凍結 (税務調査時のエビデンス用)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from src.aoiro.aggregate import compute_bs, compute_pl
from src.aoiro.deductions import yearly_summary as deduction_summary
from src.aoiro.real_estate import yearly_summary as real_estate_summary

# 青色申告特別控除のオプション (label, amount)
# 65万: e-Tax + 複式簿記 + 期限内申告
# 55万: 紙提出 + 複式簿記 + 期限内申告
# 10万: 簡易簿記 (現金主義 等) または期限後申告
#  0万: 適用なし (白色 / 不動産で事業的規模未満かつ簡易簿記 等)
# 不動産所得については「事業的規模 (5棟10室以上)」未満だと 10 万円が上限。
# 事業 + 不動産 を合算して 65 万円が上限 (重複適用不可)。
SPECIAL_DEDUCTION_OPTIONS: list[tuple[str, int]] = [
    ("65万円 (e-Tax + 複式簿記 + 期限内)", 650_000),
    ("55万円 (紙提出 + 複式簿記 + 期限内)", 550_000),
    ("10万円 (簡易簿記 / 期限後申告)",      100_000),
    ("0円 (適用なし)",                      0),
]
SPECIAL_DEDUCTION_BLUE_E_TAX = 650_000
SPECIAL_DEDUCTION_BLUE_SIMPLE = 100_000


# ─────────────────────────────────────────────
# 給与所得控除 (令和2年以後)
# 給与収入から所得控除を差し引いて「給与所得」を求める
# ─────────────────────────────────────────────
def salary_income_deduction(salary_revenue: int) -> int:
    """給与収入 → 給与所得控除額。"""
    s = salary_revenue
    if s <= 1_625_000:
        return 550_000
    if s <= 1_800_000:
        return int(s * 0.4 - 100_000)
    if s <= 3_600_000:
        return int(s * 0.3 + 80_000)
    if s <= 6_600_000:
        return int(s * 0.2 + 440_000)
    if s <= 8_500_000:
        return int(s * 0.1 + 1_100_000)
    return 1_950_000  # 850 万超は一律 195 万


def salary_revenue_to_income(salary_revenue: int) -> int:
    """給与収入 → 給与所得 (控除後)。"""
    return max(0, salary_revenue - salary_income_deduction(salary_revenue))


# ─────────────────────────────────────────────
# 所得税 (累進税率) + 復興特別所得税 + 住民税 参考計算
# ─────────────────────────────────────────────
# 課税所得 → (税率, 控除額) (令和2年以後・基本税率)
# 出典: 国税庁「所得税の税率」(平成27年分以降)
_INCOME_TAX_BRACKETS: list[tuple[int, float, int]] = [
    # (上限, 税率, 速算控除額)
    (1_950_000,    0.05,        0),
    (3_300_000,    0.10,    97_500),
    (6_950_000,    0.20,   427_500),
    (9_000_000,    0.23,   636_000),
    (18_000_000,   0.33, 1_536_000),
    (40_000_000,   0.40, 2_796_000),
    (10**12,       0.45, 4_796_000),  # 4000万超
]


def income_tax(taxable_income: int) -> int:
    """課税所得 → 所得税 (100 円未満切捨)。
    課税所得自体の 1000 円未満切捨は呼出側で実施 (compute_declaration 参照)。
    """
    if taxable_income <= 0:
        return 0
    for upper, rate, ded in _INCOME_TAX_BRACKETS:
        if taxable_income <= upper:
            t = int(taxable_income * rate - ded)
            return max(0, t // 100 * 100)  # 100 円未満切捨
    return 0


def reconstruction_surtax(income_tax_amount: int) -> int:
    """復興特別所得税 = 所得税 × 2.1% (1 円未満切捨)。
    100 円未満切捨は所得税本体 (32 番) のみ。45 番復興は 1 円単位で計算。
    """
    if income_tax_amount <= 0:
        return 0
    return int(income_tax_amount * 0.021)


def resident_tax(taxable_income: int) -> int:
    """住民税参考額 (所得割 10% + 均等割 5,000 円)。
    自治体により多少異なるため参考値。確定申告書では計算しないが UI で目安表示。
    """
    if taxable_income <= 0:
        return 5_000
    return int(taxable_income * 0.10) + 5_000


def compute_tax(taxable_income: int, *, withholding_tax: int = 0,
                estimated_tax: int = 0, tax_credits: int = 0) -> dict:
    """
    課税所得から所得税・復興特別を計算し、源泉/予定/税額控除を差引いて
    申告納税額 (還付/納付) を返す。
    - withholding_tax: 給与・報酬の源泉徴収税額 (差引)
    - estimated_tax: 予定納税額 (差引)
    - tax_credits: 配当控除・住宅ローン控除等 (差引)
    """
    inc_tax = income_tax(taxable_income)
    inc_tax_after_credit = max(0, inc_tax - tax_credits)
    surtax = reconstruction_surtax(inc_tax_after_credit)
    total_tax = inc_tax_after_credit + surtax
    final = total_tax - withholding_tax - estimated_tax
    return {
        "income_tax": inc_tax,
        "tax_credits": tax_credits,
        "income_tax_after_credit": inc_tax_after_credit,
        "reconstruction_surtax": surtax,
        "total_tax": total_tax,
        "withholding_tax": withholding_tax,
        "estimated_tax": estimated_tax,
        "final_payable": max(0, final),       # 納付額
        "final_refund": max(0, -final),       # 還付額
        "resident_tax_estimate": resident_tax(taxable_income),
    }


def salary_revenue_from_transactions(transactions_db: "sqlite3.Connection",
                                     year: int) -> dict:
    """transactions から category='給与' の年度合計を取得。
    - revenue: credit 合計 (源泉徴収後の振込額。本来は源泉前を入力すべき)
    - count: 件数
    - 個別行も返す (UI 確認用)
    """
    yyyy = f"{year:04d}"
    rows = []
    total = 0
    cur = transactions_db.execute(
        "SELECT id, bank, date, debit, credit, description "
        "FROM transactions WHERE substr(date,1,4)=? AND category='給与' "
        "ORDER BY date, id", (yyyy,)
    )
    for r in cur.fetchall():
        d = dict(r)
        amt = int(d["credit"] or 0) - int(d["debit"] or 0)
        total += amt
        d["amount"] = amt
        rows.append(d)
    return {
        "year": year,
        "salary_revenue": total,  # transactions ベース (源泉後)
        "salary_income": salary_revenue_to_income(total),
        "income_deduction": salary_income_deduction(total),
        "count": len(rows),
        "transactions": rows,
    }


def _income_business(pl: dict, special_deduction: int) -> int:
    """事業所得 = PL.income_before_special - 青色申告特別控除 (0 floor)。"""
    raw = pl["income_before_special"]
    return max(0, raw - special_deduction)


def _income_real_estate(re_sum: dict, special_deduction: int) -> int:
    raw = re_sum["total_net_income"]
    return max(0, raw - special_deduction)


def compute_declaration(con: sqlite3.Connection, year: int, *,
                        salary_income: int = 0, other_income: int = 0,
                        special_deduction_business: int = SPECIAL_DEDUCTION_BLUE_E_TAX,
                        special_deduction_real_estate: int = 0,
                        withholding_tax: int = 0, estimated_tax: int = 0,
                        tax_credits: int = 0) -> dict:
    """申告書 B 第一表の所得集計 + 税額計算。
    所得控除合計 → 課税所得 → 所得税 (累進) → 復興特別 → 申告納税額。
    1,000 円未満切捨は income_tax 内部で実施。
    """
    pl = compute_pl(con, year)
    bs = compute_bs(con, year)
    re_sum = real_estate_summary(con, year)
    ded = deduction_summary(con, year)

    biz_raw = pl["income_before_special"]
    re_net = re_sum["total_net_income"]
    # 損益通算: 黒字なら青色特別控除を適用してから 0 floor、赤字なら通算用にマイナスのまま
    if biz_raw >= 0:
        inc_business = max(0, biz_raw - special_deduction_business)
        biz_special_used = min(biz_raw, special_deduction_business)
    else:
        inc_business = biz_raw  # 赤字を給与等と通算
        biz_special_used = 0
    if re_net >= 0:
        inc_real_estate = max(0, re_net - special_deduction_real_estate)
    else:
        inc_real_estate = re_net
    re_special = special_deduction_real_estate
    income_total = inc_business + inc_real_estate + salary_income + other_income
    deduction_total = ded["grand_total"]
    # 課税所得は 1,000 円未満切捨
    taxable_raw = max(0, income_total - deduction_total)
    taxable_income = taxable_raw // 1000 * 1000

    tax = compute_tax(
        taxable_income,
        withholding_tax=withholding_tax,
        estimated_tax=estimated_tax,
        tax_credits=tax_credits,
    )

    return {
        "year": year,
        "income": {
            "business_raw": biz_raw,
            "business_special_deduction": special_deduction_business,
            "business": inc_business,
            "real_estate_raw": re_net,
            "real_estate_special_deduction": re_special,
            "real_estate": inc_real_estate,
            "salary": salary_income,
            "other": other_income,
            "total": income_total,
        },
        "deduction_total": deduction_total,
        "taxable_income_raw": taxable_raw,
        "taxable_income": taxable_income,
        "tax": tax,
        "pl": pl,
        "bs": bs,
        "real_estate": re_sum,
        "deductions": ded,
    }


# ─────────────────────────────────────────────
# Snapshot (aoiro_declaration_runs)
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# 申告書 B 第一表 入力値の永続化 (aoiro_declaration_inputs)
# 給与・源泉・予定納税・特別控除選択 等の年度別パラメータ
# ─────────────────────────────────────────────
_DECL_INPUT_FIELDS = (
    "salary_income", "other_income",
    "special_deduction_business", "special_deduction_real_estate",
    "withholding_tax", "estimated_tax", "tax_credits",
    "business_raw_override",
)


def get_declaration_inputs(con: sqlite3.Connection, year: int) -> dict:
    """指定年度の申告書 B 入力値。未保存ならデフォルト値を返す。"""
    cur = con.execute(
        "SELECT * FROM aoiro_declaration_inputs WHERE fiscal_year=?", (year,)
    )
    row = cur.fetchone()
    if not row:
        return {
            "fiscal_year": year,
            "salary_income": 0, "other_income": 0,
            "special_deduction_business": SPECIAL_DEDUCTION_BLUE_E_TAX,
            "special_deduction_real_estate": 0,
            "withholding_tax": 0, "estimated_tax": 0, "tax_credits": 0,
            "business_raw_override": None,
            "updated_at": None,
        }
    return dict(row)


def save_declaration_inputs(con: sqlite3.Connection, year: int, params: dict) -> None:
    from datetime import datetime
    fields = {}
    for k in _DECL_INPUT_FIELDS:
        v = params.get(k)
        if k == "business_raw_override":
            # None / 空文字 / 0 はそのまま (override なしを 0 と区別したい場合は None)
            if v is None or v == "":
                fields[k] = None
            else:
                try:
                    fields[k] = int(v)
                except (ValueError, TypeError):
                    fields[k] = None
        else:
            fields[k] = int(v) if v is not None else 0
    cur = con.execute("SELECT 1 FROM aoiro_declaration_inputs WHERE fiscal_year=?", (year,))
    if cur.fetchone():
        sets = ", ".join(f"{k}=?" for k in _DECL_INPUT_FIELDS)
        con.execute(
            f"UPDATE aoiro_declaration_inputs SET {sets}, updated_at=? WHERE fiscal_year=?",
            (*[fields[k] for k in _DECL_INPUT_FIELDS], datetime.now().isoformat(), year),
        )
    else:
        cols = ", ".join(_DECL_INPUT_FIELDS)
        ph = ", ".join("?" * len(_DECL_INPUT_FIELDS))
        con.execute(
            f"INSERT INTO aoiro_declaration_inputs (fiscal_year, {cols}, updated_at) "
            f"VALUES (?, {ph}, ?)",
            (year, *[fields[k] for k in _DECL_INPUT_FIELDS],
             datetime.now().isoformat()),
        )
    con.commit()


def get_snapshot(con: sqlite3.Connection, year: int) -> dict | None:
    cur = con.execute(
        "SELECT * FROM aoiro_declaration_runs WHERE fiscal_year=?", (year,)
    )
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("pl_json", "bs_json", "declaration_json", "exports_json"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, json.JSONDecodeError):
                pass
    return d


def save_snapshot(con: sqlite3.Connection, year: int, *,
                  declaration: dict, exports: dict | None = None,
                  lock: bool = False) -> dict:
    """snapshot を upsert。lock=True で freeze。
    既に locked のものは更新拒否 (ValueError)。
    """
    cur = con.execute("SELECT locked_at FROM aoiro_declaration_runs WHERE fiscal_year=?", (year,))
    row = cur.fetchone()
    if row and row["locked_at"]:
        raise ValueError(f"declaration {year} is locked at {row['locked_at']}")
    pl_json = json.dumps(declaration["pl"], ensure_ascii=False)
    bs_json = json.dumps(declaration["bs"], ensure_ascii=False)
    decl_json = json.dumps({k: v for k, v in declaration.items()
                             if k not in ("pl", "bs")},
                            ensure_ascii=False)
    exp_json = json.dumps(exports or {}, ensure_ascii=False)
    locked_at = datetime.now().isoformat() if lock else None
    if row:
        con.execute(
            "UPDATE aoiro_declaration_runs SET pl_json=?, bs_json=?, "
            "declaration_json=?, exports_json=?, locked_at=? WHERE fiscal_year=?",
            (pl_json, bs_json, decl_json, exp_json, locked_at, year),
        )
    else:
        con.execute(
            "INSERT INTO aoiro_declaration_runs "
            "(fiscal_year, pl_json, bs_json, declaration_json, exports_json, locked_at) "
            "VALUES (?,?,?,?,?,?)",
            (year, pl_json, bs_json, decl_json, exp_json, locked_at),
        )
    con.commit()
    return get_snapshot(con, year)


def unlock_snapshot(con: sqlite3.Connection, year: int) -> bool:
    cur = con.execute(
        "UPDATE aoiro_declaration_runs SET locked_at=NULL WHERE fiscal_year=?",
        (year,),
    )
    con.commit()
    return cur.rowcount > 0


def delete_snapshot(con: sqlite3.Connection, year: int) -> bool:
    cur = con.execute("SELECT locked_at FROM aoiro_declaration_runs WHERE fiscal_year=?", (year,))
    row = cur.fetchone()
    if row and row["locked_at"]:
        raise ValueError(f"declaration {year} is locked")
    cur = con.execute("DELETE FROM aoiro_declaration_runs WHERE fiscal_year=?", (year,))
    con.commit()
    return cur.rowcount > 0
