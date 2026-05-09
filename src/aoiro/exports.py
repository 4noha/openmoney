"""CSV エクスポート群。Phase 7 のシンプルな出力レイヤ。

freee/MFクラウド/弥生のような特定フォーマットには合わせず、
人間も読める汎用 CSV (BOM 付き UTF-8、ヘッダ日本語可) を返す。
"""
from __future__ import annotations

import csv
import io
import sqlite3

from src.aoiro.aggregate import (
    compute_bs,
    compute_card_monthly,
    compute_general_ledger,
    compute_monthly_pl,
    compute_pl,
    compute_trial_balance,
)
from src.aoiro.deductions import yearly_summary as deduction_summary
from src.aoiro.depreciation import list_schedules_for_year
from src.aoiro.real_estate import yearly_summary as real_estate_summary

_BOM = "﻿"


def _csv_response(headers: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    buf.write(_BOM)
    w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    w.writerow(headers)
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def export_journal(con: sqlite3.Connection, year: int,
                    exclude_personal: bool = False) -> str:
    where_personal = " AND COALESCE(j.source_category,'') != '個人支出'" if exclude_personal else ""
    rows = []
    for r in con.execute(
        "SELECT j.id, j.fiscal_year, j.date, "
        "  j.debit_account, da.name AS debit_name, j.debit_amount, "
        "  j.credit_account, ca.name AS credit_name, j.credit_amount, "
        "  j.description, j.source, j.source_tx_id, j.source_category "
        "FROM aoiro_journal_entries j "
        "LEFT JOIN aoiro_accounts da ON da.code = j.debit_account "
        "LEFT JOIN aoiro_accounts ca ON ca.code = j.credit_account "
        f"WHERE j.fiscal_year=?{where_personal} ORDER BY j.date, j.id", (year,)
    ).fetchall():
        rows.append([r[i] for i in range(13)])
    return _csv_response(
        ["id", "年度", "日付", "借方科目code", "借方科目名", "借方金額",
         "貸方科目code", "貸方科目名", "貸方金額", "摘要", "source", "source_tx_id", "元category"],
        rows,
    )


def export_pl(con: sqlite3.Connection, year: int) -> str:
    pl = compute_pl(con, year)
    rows: list[list] = []
    rows.append(["── 収益 ──", "", "", "", ""])
    for x in pl["revenue_lines"]:
        rows.append([x["code"], x["name"], x["category"] or "", "", x["amount"]])
    rows.append(["", "収益合計", "", "", pl["revenue_total"]])
    rows.append([])
    rows.append(["── 費用 ──", "", "", "", ""])
    for x in pl["expense_lines"]:
        rows.append([x["code"], x["name"], x["category"] or "", x["amount"], ""])
    rows.append(["", "費用合計", "", pl["expense_total"], ""])
    rows.append([])
    rows.append(["", "青色申告特別控除前 所得", "", "", pl["income_before_special"]])
    rows.append([])
    rows.append(["── 月別売上 ──", "", "", "", ""])
    rows.append(["月", "売上", "仕入 (5001)", "", ""])
    by_m = {m["month"]: m["amount"] for m in pl["monthly_revenue"]}
    by_p = {m["month"]: m["amount"] for m in pl["monthly_purchase"]}
    for mm in sorted(set(list(by_m) + list(by_p))):
        rows.append([mm, by_m.get(mm, 0), by_p.get(mm, 0), "", ""])
    return _csv_response(["code", "name/ラベル", "category", "費用額", "収益額"], rows)


def export_bs(con: sqlite3.Connection, year: int) -> str:
    bs = compute_bs(con, year)
    rows: list[list] = []
    def emit(title, lines):
        rows.append([f"── {title} ──", "", "", "", "", ""])
        for x in lines:
            rows.append([x["code"], x["name"], x["category"] or "",
                         x["opening"], x["debit_total"], x["credit_total"]])
            rows[-1].append(x["ending"])
    rows_ext = []
    def append_ext(r):
        rows_ext.append(r)
    # 簡易: 期首/借/貸/期末 を 4 列で出力
    rows.append(["section", "code", "name", "category", "期首", "借方", "貸方", "期末"])
    for sec, lines in (("資産", bs["asset_lines"]),
                       ("負債", bs["liability_lines"]),
                       ("資本", bs["equity_lines"])):
        for x in lines:
            rows.append([sec, x["code"], x["name"], x["category"] or "",
                         x["opening"], x["debit_total"], x["credit_total"], x["ending"]])
    rows.append([])
    rows.append(["", "", "資産合計", "", "", "", "", bs["asset_total"]])
    rows.append(["", "", "負債合計", "", "", "", "", bs["liability_total"]])
    rows.append(["", "", "資本合計 (純利益込)", "", "", "", "", bs["equity_total_with_income"]])
    rows.append(["", "", "balance_check", "", "", "", "", bs["balance_check"]])
    # csv の最初の行はすでに header っぽいが、_csv_response が header を別途書くので除去
    headers = rows.pop(0)
    return _csv_response(headers, rows)


def export_deductions(con: sqlite3.Connection, year: int) -> str:
    sm = deduction_summary(con, year)
    rows = []
    for k in sm["kinds"]:
        for it in k["items"]:
            rows.append([year, k["kind"], it["payee"], it["amount"],
                         it["evidence_path"] or "", it["note"] or ""])
    rows.append([])
    rows.append(["", "合計", "", sm["grand_total"], "", ""])
    return _csv_response(
        ["年度", "種類", "支払先", "金額", "証憑", "note"], rows,
    )


def export_real_estate(con: sqlite3.Connection, year: int) -> str:
    sm = real_estate_summary(con, year)
    rows = []
    for r in sm["rows"]:
        p = r["property"]
        for m in r["monthly"]:
            rows.append([p["id"], p["name"], p["address"] or "", m["month"],
                         m["gross_rent"], m["vacancy"], m["expenses"],
                         m["gross_rent"] - m["vacancy"] - m["expenses"], m["note"] or ""])
        rows.append(["", f"{p['name']} 年計", "", "",
                     r["gross_rent"], r["vacancy"], r["expenses"], r["net_income"], ""])
        rows.append([])
    rows.append(["", "全物件年計", "", "", sm["total_gross"], sm["total_vacancy"],
                 sm["total_expenses"], sm["total_net_income"], ""])
    return _csv_response(
        ["物件id", "物件名", "住所", "月", "総賃料", "空室損", "経費", "純収益", "note"],
        rows,
    )


def export_trial_balance(con: sqlite3.Connection, year: int) -> str:
    """合計残高試算表 CSV."""
    tb = compute_trial_balance(con, year)
    rows = [[r["code"], r["name"], r["type"], r["category"],
             r["opening_debit"], r["opening_credit"],
             r["period_debit"], r["period_credit"],
             r["ending_debit"], r["ending_credit"]] for r in tb["rows"]]
    t = tb["totals"]
    rows.append(["", "合計", "", "", "", "",
                 t["period_debit"], t["period_credit"],
                 t["ending_debit"], t["ending_credit"]])
    return _csv_response(
        ["code", "科目名", "type", "category",
         "期首借方", "期首貸方", "当期借方計", "当期貸方計",
         "期末借方", "期末貸方"], rows,
    )


def export_general_ledger(con: sqlite3.Connection, year: int, account: str) -> str:
    """元帳 CSV (指定 account)."""
    gl = compute_general_ledger(con, year, account)
    if "error" in gl:
        return _csv_response(["error"], [[gl["error"]]])
    rows = []
    rows.append(["", "期首残高", "", "", 0, 0, gl["opening_balance"], "", ""])
    for e in gl["entries"]:
        rows.append([e["id"], e["date"], e["counter_account"],
                     "", e["debit"], e["credit"], e["running_balance"],
                     e["source"], e["description"] or ""])
    rows.append(["", "期末残高", "", "", "", "", gl["ending_balance"], "", ""])
    return _csv_response(
        ["仕訳id", "日付", "相手科目", "", "借方", "貸方", "残高", "source", "摘要"],
        rows,
    )


def export_monthly_pl(con: sqlite3.Connection, year: int) -> str:
    """月次 PL CSV (科目別 12 ヶ月推移)."""
    mpl = compute_monthly_pl(con, year)
    months = mpl["months"]
    rows = []
    rows.append(["[収益]"])
    for line in mpl["revenue_lines"]:
        rows.append([line["code"], line["name"]] + line["amounts"] + [line["total"]])
    rows.append(["", "収益合計"] + mpl["monthly_revenue_total"] + [sum(mpl["monthly_revenue_total"])])
    rows.append(["[費用]"])
    for line in mpl["expense_lines"]:
        rows.append([line["code"], line["name"]] + line["amounts"] + [line["total"]])
    rows.append(["", "費用合計"] + mpl["monthly_expense_total"] + [sum(mpl["monthly_expense_total"])])
    rows.append(["", "純利益"] + mpl["monthly_net"] + [sum(mpl["monthly_net"])])
    return _csv_response(["code", "科目名"] + months + ["年計"], rows)


def export_pl_yoy(con: sqlite3.Connection, year: int) -> str:
    """PL 前年比較 CSV."""
    cur = compute_pl(con, year)
    prev = compute_pl(con, year - 1)
    prev_rev = {x["code"]: x for x in prev.get("revenue_lines") or []}
    prev_exp = {x["code"]: x for x in prev.get("expense_lines") or []}
    rows = [["[収益]"]]
    for x in cur.get("revenue_lines") or []:
        pa = int((prev_rev.get(x["code"]) or {}).get("amount") or 0)
        rows.append([x["code"], x["name"], int(x["amount"]), pa, int(x["amount"]) - pa])
    rows.append(["", "収益合計", int(cur.get("revenue_total") or 0),
                 int(prev.get("revenue_total") or 0),
                 int(cur.get("revenue_total") or 0) - int(prev.get("revenue_total") or 0)])
    rows.append(["[費用]"])
    for x in cur.get("expense_lines") or []:
        pa = int((prev_exp.get(x["code"]) or {}).get("amount") or 0)
        rows.append([x["code"], x["name"], int(x["amount"]), pa, int(x["amount"]) - pa])
    rows.append(["", "費用合計", int(cur.get("expense_total") or 0),
                 int(prev.get("expense_total") or 0),
                 int(cur.get("expense_total") or 0) - int(prev.get("expense_total") or 0)])
    return _csv_response(
        ["code", "科目名", f"{year} 当期", f"{year - 1} 前期", "差額"], rows,
    )


def export_card_monthly(con: sqlite3.Connection, year: int) -> str:
    """カード別月次推移 CSV."""
    cm = compute_card_monthly(con, year)
    months = [f"{i:02d}" for i in range(1, 13)]
    rows = []
    for c in cm["cards"]:
        # 発生
        gen_row = [c["bank"], "発生 (利用)"] + [m["generated"] for m in c["monthly"]] + [c["year_total_generated"]]
        set_row = [c["bank"], "引落 (返済)"] + [m["settled"]   for m in c["monthly"]] + [c["year_total_settled"]]
        out_row = [c["bank"], "残債 (累積)"] + [m["outstanding"] for m in c["monthly"]] + [c["ending_outstanding"]]
        rows += [gen_row, set_row, out_row]
    return _csv_response(["card", "種別"] + months + ["年計/期末"], rows)


def export_yayoi_journal(con: sqlite3.Connection, year: int,
                          exclude_personal: bool = False) -> str:
    """弥生会計「一般仕訳」 互換 CSV 出力。

    弥生の取込仕様 (24 列):
      識別フラグ / 伝票No / 決算 / 取引日付 /
      借方勘定科目 / 借方補助科目 / 借方部門 / 借方税区分 / 借方金額 / 借方税金額 /
      貸方勘定科目 / 貸方補助科目 / 貸方部門 / 貸方税区分 / 貸方金額 / 貸方税金額 /
      摘要 / 番号 / 期日 / タイプ / 生成元 / 仕訳メモ / 付箋1 / 付箋2 / 調整

    税区分はシンプルに「対象外」 (取込後に弥生側で自動仕訳で再分類)。
    勘定科目は code + name 形式 (弥生は名称マッチで取込)。
    """
    accs = {r["code"]: dict(r) for r in con.execute(
        "SELECT code, name FROM aoiro_accounts"
    ).fetchall()}

    def _acc_label(code: str) -> str:
        a = accs.get(code)
        return f"{code} {a['name']}" if a else code

    where_personal = " AND COALESCE(source_category,'') != '個人支出'" if exclude_personal else ""
    rows = []
    seq = 0
    for r in con.execute(
        "SELECT id, date, debit_account, debit_amount, credit_account, credit_amount, "
        "       description, source FROM aoiro_journal_entries "
        f"WHERE fiscal_year=?{where_personal} ORDER BY date, id",
        (year,),
    ).fetchall():
        seq += 1
        date = (r["date"] or "").replace("-", "/")
        memo = (r["description"] or "")[:128]
        # 「仕訳メモ」 に source を残す
        memo_extra = f"source={r['source']}"
        rows.append([
            "2000",                           # 識別フラグ (一般仕訳)
            str(seq),                         # 伝票No
            "0",                              # 決算
            date,                             # 取引日付
            _acc_label(r["debit_account"]),   # 借方勘定科目
            "",                               # 借方補助科目
            "",                               # 借方部門
            "対象外",                         # 借方税区分
            int(r["debit_amount"] or 0),      # 借方金額
            "0",                              # 借方税金額
            _acc_label(r["credit_account"]),  # 貸方勘定科目
            "",                               # 貸方補助科目
            "",                               # 貸方部門
            "対象外",                         # 貸方税区分
            int(r["credit_amount"] or 0),     # 貸方金額
            "0",                              # 貸方税金額
            memo,                             # 摘要
            "",                               # 番号
            "",                               # 期日
            "3",                              # タイプ (3=本文)
            "0",                              # 生成元 (0=入力)
            memo_extra,                       # 仕訳メモ
            "0",                              # 付箋1
            "0",                              # 付箋2
            "no",                             # 調整
        ])
    return _csv_response(
        ["識別フラグ", "伝票No", "決算", "取引日付",
         "借方勘定科目", "借方補助科目", "借方部門", "借方税区分", "借方金額", "借方税金額",
         "貸方勘定科目", "貸方補助科目", "貸方部門", "貸方税区分", "貸方金額", "貸方税金額",
         "摘要", "番号", "期日", "タイプ", "生成元", "仕訳メモ", "付箋1", "付箋2", "調整"],
        rows,
    )


def export_mfcloud_journal(con: sqlite3.Connection, year: int,
                            exclude_personal: bool = False) -> str:
    """MF クラウド会計「仕訳帳取込」 CSV 互換出力。

    MF の標準形式 (簡易):
      取引No / 取引日 / 借方勘定科目 / 借方補助科目 / 借方部門 / 借方税区分 /
      借方金額(円) / 借方税額 /
      貸方勘定科目 / 貸方補助科目 / 貸方部門 / 貸方税区分 /
      貸方金額(円) / 貸方税額 / 摘要 / 仕訳メモ / タグ / MF仕訳タイプ / 決算整理仕訳 / 作成日時
    """
    accs = {r["code"]: dict(r) for r in con.execute(
        "SELECT code, name FROM aoiro_accounts"
    ).fetchall()}

    def _name(code: str) -> str:
        a = accs.get(code)
        return a["name"] if a else code

    where_personal = " AND COALESCE(source_category,'') != '個人支出'" if exclude_personal else ""
    rows = []
    seq = 0
    for r in con.execute(
        "SELECT id, date, debit_account, debit_amount, credit_account, credit_amount, "
        "       description, source, created_at FROM aoiro_journal_entries "
        f"WHERE fiscal_year=?{where_personal} ORDER BY date, id",
        (year,),
    ).fetchall():
        seq += 1
        is_closing = (r["source"] in ("closing", "opening"))
        rows.append([
            seq,                              # 取引No
            (r["date"] or "").replace("-", "/"),
            _name(r["debit_account"]),
            "", "", "対象外",
            int(r["debit_amount"] or 0), 0,
            _name(r["credit_account"]),
            "", "", "対象外",
            int(r["credit_amount"] or 0), 0,
            (r["description"] or "")[:128],
            f"source={r['source']}",
            "",                               # タグ
            "",                               # MF仕訳タイプ
            "1" if is_closing else "0",       # 決算整理仕訳
            r["created_at"] or "",
        ])
    return _csv_response(
        ["取引No", "取引日",
         "借方勘定科目", "借方補助科目", "借方部門", "借方税区分", "借方金額(円)", "借方税額",
         "貸方勘定科目", "貸方補助科目", "貸方部門", "貸方税区分", "貸方金額(円)", "貸方税額",
         "摘要", "仕訳メモ", "タグ", "MF仕訳タイプ", "決算整理仕訳", "作成日時"],
        rows,
    )


def export_rental_contracts(con: sqlite3.Connection,
                             fiscal_year: int | None = None) -> str:
    """賃借物件 (地代家賃) 内訳 CSV — 確定申告書 第三表 用。
    年度指定なら該当年度のみ、未指定なら全年度。
    """
    from src.aoiro.rental_contracts import list_contracts
    rows = []
    total = 0
    business = 0
    for c in list_contracts(con, fiscal_year=fiscal_year):
        annual = int(c["annual_rent"] or 0)
        annual_biz = annual * (c["business_ratio_pct"] or 100) // 100
        total += annual
        business += annual_biz
        rows.append([
            c["fiscal_year"], c["id"], c["property_name"],
            c["property_address"] or "",
            c["area_sqm"] or 0, c["usage"] or "",
            c["business_ratio_pct"] or 100,
            annual, annual_biz,
            c["landlord_name"] or "", c["landlord_address"] or "",
            c["landlord_phone"] or "",
            c["note"] or "",
        ])
    rows.append(["", "", "合計", "", "", "", "", total, business,
                  "", "", "", ""])
    return _csv_response(
        ["年度", "id", "物件名", "所在地", "使用面積m²", "用途", "業務%",
         "本年賃借料 (年額)", "業務按分後年額",
         "貸主名", "貸主住所", "貸主電話", "備考"],
        rows,
    )


def export_fixed_assets(con: sqlite3.Connection, year: int) -> str:
    rows = []
    for a in list_schedules_for_year(con, year):
        rows.append([a["id"], a["name"], a["acquired_date"], a["acquired_cost"],
                     a["method"], a["useful_years"], a["business_ratio"],
                     a["account"], a["opening_book"] or 0,
                     a["depreciation"], a["closing_book"] or 0,
                     "完了" if a["fully_depreciated"] else ""])
    return _csv_response(
        ["id", "name", "取得日", "取得価額", "method", "耐用年数",
         "業務%", "BS科目", "期首簿価", "当年償却", "期末簿価", "完了"],
        rows,
    )
