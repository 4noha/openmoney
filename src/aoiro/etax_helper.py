"""e-Tax 確定申告書等作成コーナー へのコピペ支援。

国税庁の確定申告書等作成コーナー (https://www.keisan.nta.go.jp/) の
入力フィールドに手動コピペで申告するための「値表」を生成する。

各項目は (label, etax_field_name, value, group) の構造で、ユーザは
これを画面で確認しながら e-Tax の対応欄にコピー&ペーストする。

直接送信 (XBRL) や PDF 帳票生成は別フェーズ。
"""
from __future__ import annotations

import sqlite3


def build_etax_values(con: sqlite3.Connection, year: int) -> dict:
    """指定年度の e-Tax 入力値表を生成。snapshot がロック済ならそちらの値を、
    無ければ live で再計算する。
    """
    # snapshot から取れるなら snapshot 優先 (= ロック済の凍結値)
    decl = None
    pl = None
    bs = None
    deductions = None
    rental = None
    invoice = None

    cur = con.execute(
        "SELECT snapshot_json, locked_at FROM aoiro_declaration_runs WHERE fiscal_year=?",
        (year,),
    )
    row = cur.fetchone()
    if row and row["snapshot_json"]:
        import json
        snap = json.loads(row["snapshot_json"])
        decl = snap.get("declaration")
        pl = snap.get("pl")
        bs = snap.get("bs")
        deductions = snap.get("deductions")
        rental = snap.get("rental_contracts")
        is_locked = bool(row["locked_at"])
    else:
        is_locked = False

    # snapshot に無いなら live で再計算
    if not decl:
        from src.aoiro.forms import compute_declaration
        from src.aoiro.aggregate import compute_bs, compute_pl
        decl = compute_declaration(con, year)
        pl = compute_pl(con, year)
        bs = compute_bs(con, year)

    # インボイス消費税 (live 計算)
    try:
        from src.aoiro.invoice_summary import compute_invoice_summary
        invoice = compute_invoice_summary(con, year)
    except Exception:
        invoice = None

    # 賃借物件 (live)
    if rental is None:
        try:
            from src.aoiro.rental_contracts import (
                annual_rent_total as _rt,
                list_contracts as _lc,
            )
            rental = {"contracts": _lc(con, fiscal_year=year),
                       "summary": _rt(con, fiscal_year=year)}
        except Exception:
            rental = {"contracts": [], "summary": {}}

    inc = (decl or {}).get("income", {}) or {}
    tax = (decl or {}).get("tax", {}) or {}

    # ─────────────────────────────────────────
    # 申告書 B 第一表 (収入金額・所得金額)
    # ─────────────────────────────────────────
    sec_income = {
        "title": "申告書 B 第一表 — 収入金額・所得金額",
        "fields": [
            {"label": "事業所得 (営業等)", "etax": "①", "value": inc.get("business", 0),
             "note": "PL 営業所得 - 青色申告特別控除"},
            {"label": "  内訳: PL 事業所得 (raw)", "etax": "—", "value": inc.get("business_raw", 0),
             "note": "青色特控前。雑収入含む"},
            {"label": "  内訳: 青色申告特別控除", "etax": "—", "value": inc.get("business_special_deduction", 0),
             "note": "55万 / 65万 / 10万 から選択"},
            {"label": "不動産所得", "etax": "②", "value": inc.get("real_estate", 0),
             "note": "事業統合 ON なら 0 (① に集約)"},
            {"label": "給与所得", "etax": "⑥", "value": inc.get("salary", 0),
             "note": "給与所得控除後の金額。源泉徴収票の「給与所得控除後の金額」"},
            {"label": "その他の所得", "etax": "—", "value": inc.get("other", 0)},
            {"label": "★ 所得合計", "etax": "⑫", "value": inc.get("total", 0),
             "note": "① + ② + ⑥ + その他"},
        ],
    }

    # ─────────────────────────────────────────
    # 申告書 B 第一表 (所得控除)
    # ─────────────────────────────────────────
    ded_total = (decl or {}).get("deduction_total", 0)
    ded_breakdown = (deductions or {}).get("by_kind", []) if deductions else []
    ded_fields = [
        {"label": f"  {b['kind']}", "etax": "—", "value": int(b.get("total") or 0)}
        for b in ded_breakdown
    ]
    sec_deduction = {
        "title": "申告書 B 第一表 — 所得から差し引かれる金額 (控除)",
        "fields": [
            *ded_fields,
            {"label": "★ 所得控除合計", "etax": "㉕", "value": ded_total},
        ],
    }

    # ─────────────────────────────────────────
    # 申告書 B 第一表 (税額計算)
    # ─────────────────────────────────────────
    sec_tax = {
        "title": "申告書 B 第一表 — 税額計算",
        "fields": [
            {"label": "課税される所得金額 (1000円未満切捨)", "etax": "㉖",
             "value": (decl or {}).get("taxable_income", 0)},
            {"label": "上記に対する税額", "etax": "㉗", "value": tax.get("income_tax", 0)},
            {"label": "税額控除", "etax": "—", "value": tax.get("tax_credits", 0)},
            {"label": "差引所得税額", "etax": "㊵", "value": tax.get("income_tax_after_credit", 0)},
            {"label": "復興特別所得税 (×2.1%)", "etax": "㊶", "value": tax.get("reconstruction_surtax", 0)},
            {"label": "★ 所得税及び復興特別所得税の額", "etax": "㊷", "value": tax.get("total_tax", 0)},
            {"label": "源泉徴収税額", "etax": "㊽", "value": tax.get("withholding_tax", 0)},
            {"label": "予定納税額", "etax": "㊾", "value": tax.get("estimated_tax", 0)},
            {"label": "★ 納付する金額" if tax.get("final_payable") else "★ 還付される金額",
             "etax": "㊿/(54)",
             "value": tax.get("final_payable") or tax.get("final_refund") or 0},
        ],
    }

    # ─────────────────────────────────────────
    # 青色申告決算書 1 枚目 (損益計算書)
    # ─────────────────────────────────────────
    pl_lines = [
        {"label": f"  {x['code']} {x['name']}", "etax": "—",
         "value": int(x.get("amount") or 0)}
        for x in (pl or {}).get("revenue_lines", []) + (pl or {}).get("expense_lines", [])
    ]
    sec_pl = {
        "title": "青色申告決算書 1 枚目 — 損益計算書",
        "fields": [
            {"label": "売上 (収入) 金額合計", "etax": "①", "value": (pl or {}).get("revenue_total", 0)},
            *pl_lines,
            {"label": "経費合計", "etax": "—", "value": (pl or {}).get("expense_total", 0)},
            {"label": "★ 青色申告特別控除前の所得金額", "etax": "㊸",
             "value": (pl or {}).get("income_before_special", 0)},
        ],
    }

    # ─────────────────────────────────────────
    # 青色申告決算書 3 枚目 (地代家賃の内訳)
    # ─────────────────────────────────────────
    rental_fields = []
    for c in (rental or {}).get("contracts", []):
        annual = int(c.get("annual_rent") or 0)
        biz_pct = c.get("business_ratio_pct") or 100
        annual_biz = annual * biz_pct // 100
        rental_fields.append({
            "label": f"  支払先: {c.get('landlord_name','')}",
            "etax": "—", "value": c.get("landlord_address", ""),
        })
        rental_fields.append({
            "label": f"  賃借物件: {c.get('property_name','')}",
            "etax": "—", "value": c.get("property_address", ""),
        })
        rental_fields.append({
            "label": "  本年中の賃借料・権利金等", "etax": "—", "value": annual,
        })
        rental_fields.append({
            "label": f"  必要経費算入額 (業務 {biz_pct}%)", "etax": "—", "value": annual_biz,
        })
    sec_rental = {
        "title": "青色申告決算書 3 枚目 — 地代家賃の内訳",
        "fields": rental_fields or [{"label": "賃借物件なし", "etax": "—", "value": ""}],
    }

    # ─────────────────────────────────────────
    # 消費税申告 (インボイス) — 課税事業者の場合
    # ─────────────────────────────────────────
    if invoice:
        sec_invoice = {
            "title": "消費税申告 (インボイス) — 課税事業者のみ",
            "fields": [
                {"label": "課税仕入合計 (税込)", "etax": "—",
                 "value": invoice.get("total_amount_tax_in", 0)},
                {"label": "  インボイス対応 (100% 控除)", "etax": "—",
                 "value": invoice.get("with_invoice", {}).get("total_tax_in", 0)},
                {"label": "  インボイス非対応 (経過措置)", "etax": "—",
                 "value": invoice.get("without_invoice", {}).get("total_tax_in", 0)},
                {"label": "★ 控除対象仕入税額 合計", "etax": "④",
                 "value": invoice.get("creditable_total", 0),
                 "note": "消費税申告書 (一般課税) の控除対象仕入税額"},
            ],
        }
    else:
        sec_invoice = None

    sections = [sec_income, sec_deduction, sec_tax, sec_pl, sec_rental]
    if sec_invoice:
        sections.append(sec_invoice)

    return {
        "year": year,
        "is_locked_snapshot": is_locked,
        "sections": sections,
    }
