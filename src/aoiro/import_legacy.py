"""旧『青色申告帳簿作成ツール』(Spreadsheet GAS) からインポートした
tax_journal / tax_pl / tax_bs を、aoiro snapshot 形式に変換して
aoiro_declaration_runs に保存する。

これにより過去年度 (取込済み 2023/2024/2025 等) を /aoiro 画面の
年度タブから閲覧できるようになる。

`過去の納税/<year>/` ディレクトリ (CSV) → tax_* テーブル → snapshot
への一気通貫インポートは import_from_dir() を使用。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TAX_DIR = _PROJECT_ROOT / "過去の納税"


def _account_master_csv_path(year_dir: Path) -> Path | None:
    for p in year_dir.glob("*.csv"):
        if "科目マスタ" in p.name or "科目マスター" in p.name:
            return p
    return None


def parse_account_master_csv(path: Path) -> list[dict]:
    """科目マスタ CSV をパース。
    列: 科目名 / 科目貸借タイプ (借方|貸方) / 出力順番 / 精算種別 (損益計算書|貸借対照表)
    + 後ろに解説列が付くがスキップ。
    精算種別 + 借貸タイプから aoiro type を決定:
      損益計算書 + 借方 → expense
      損益計算書 + 貸方 → revenue
      貸借対照表 + 借方 → asset
      貸借対照表 + 貸方 → liability  (※元入金/事業主借/事業主貸は呼出側で equity に振替)
    """
    import csv as _csv
    out = []
    EQUITY_NAMES = {"元入金", "事業主借", "事業主貸"}
    with path.open(encoding="utf-8") as f:
        reader = _csv.reader(f)
        header = next(reader, None)
        for row in reader:
            if not row or len(row) < 4:
                continue
            name = (row[0] or "").strip()
            kbn = (row[1] or "").strip()  # 借方/貸方
            try:
                order = int((row[2] or "999").strip())
            except ValueError:
                order = 999
            kind = (row[3] or "").strip()  # 損益計算書/貸借対照表
            if not name or kbn not in ("借方", "貸方"):
                continue
            if name in EQUITY_NAMES:
                typ = "equity"
            elif kind == "損益計算書":
                typ = "revenue" if kbn == "貸方" else "expense"
            elif kind == "貸借対照表":
                typ = "asset" if kbn == "借方" else "liability"
            else:
                continue
            out.append({
                "name": name, "type": typ,
                "category": kind, "sort_order": order,
            })
    return out


def register_legacy_accounts(con: sqlite3.Connection, year_dir: Path) -> dict:
    """科目マスタ CSV を読んで aoiro_accounts に新規 name のみ追加。
    code は L-<name> プレフィックスで衝突回避 (builtin code 1xxx-7xxx は触らない)。
    既存 name はスキップ (builtin/ユーザ追加を尊重)。
    """
    csv_path = _account_master_csv_path(year_dir)
    if not csv_path:
        return {"path": None, "found": 0, "inserted": 0, "skipped": 0}
    rows = parse_account_master_csv(csv_path)
    inserted = 0
    skipped = 0
    for r in rows:
        # 既存科目チェック: 完全一致 OR 部分一致 (一方が他方を含む)。
        # type も近いもの (type 不一致の同名なら別物として扱う) を優先。
        cur = con.execute(
            "SELECT name, type FROM aoiro_accounts "
            "WHERE name=? OR name LIKE '%'||?||'%' OR ? LIKE '%'||name||'%' "
            "ORDER BY (name=?) DESC, length(name) DESC LIMIT 1",
            (r["name"], r["name"], r["name"], r["name"]),
        )
        existing = cur.fetchone()
        # type が一致しない部分一致は別物 (例: 「売上」(revenue) と既存の「売上高」(revenue) はマッチ、
        # 「事業主貸」(equity) と何かが偶然部分一致しても type 違いなら追加)
        if existing and existing["type"] == r["type"]:
            skipped += 1
            continue
        # 衝突しない code を生成: L-<name>
        code = f"L-{r['name']}"
        con.execute(
            "INSERT OR IGNORE INTO aoiro_accounts (code, name, type, category, sort_order, is_builtin, is_active) "
            "VALUES (?,?,?,?,?,0,1)",
            (code, r["name"], r["type"], r["category"], r["sort_order"]),
        )
        if con.total_changes:
            inserted += 1
    con.commit()
    return {"path": str(csv_path), "found": len(rows),
            "inserted": inserted, "skipped": skipped}


# ファイル名パターン → 書類区分
_DOC_PATTERNS: list[tuple[str, str]] = [
    # (キーワード, kind)
    ("青色申告決算書", "青色申告決算書"),
    ("申告書等送信票", "申告書等送信票"),
    ("送信票", "申告書等送信票"),
    ("申告内容確認票", "申告内容確認票"),
    ("確認票", "申告内容確認票"),
    ("医療費控除", "医療費控除の明細書"),
    ("医療費", "医療費控除の明細書"),
    ("青色申告帳簿作成", "帳簿 (補助)"),
]


def classify_document(filename: str) -> str:
    for kw, kind in _DOC_PATTERNS:
        if kw in filename:
            return kind
    return "その他"


def collect_documents(year_dir: Path) -> list[dict]:
    """過去の納税/<year>/ 配下の PDF を分類して列挙。
    UI で snapshot から開けるように {kind, filename, size, path} を返す。
    """
    if not year_dir.exists():
        return []
    out = []
    for p in sorted(year_dir.iterdir()):
        if not p.is_file():
            continue
        suffix = p.suffix.lower()
        if suffix not in (".pdf", ".data"):
            continue
        out.append({
            "kind": classify_document(p.name),
            "filename": p.name,
            "size": p.stat().st_size,
            "ext": suffix.lstrip("."),
        })
    return out


def extract_pdf_text(year_dir: Path) -> dict:
    """過去の納税/<year>/ 配下の PDF を MarkItDown でテキスト化して
    {filename: markdown_text} で返す。重い処理なので import 時のみ実行。"""
    if not year_dir.exists():
        return {}
    try:
        from markitdown import MarkItDown
    except ImportError:
        return {}
    md = MarkItDown()
    out: dict[str, str] = {}
    for p in year_dir.glob("*.pdf"):
        try:
            text = md.convert(str(p)).text_content
            out[p.name] = text
        except Exception:
            continue
    return out


def fetch_deductions_for_year(con: sqlite3.Connection, year: int) -> dict:
    """aoiro_deductions テーブルから該当年度の控除サマリを取得。
    Phase 6 UI で入力済みのデータがあれば snapshot に取り込まれる。"""
    from src.aoiro.deductions import yearly_summary
    return yearly_summary(con, year)


def list_dir_years() -> list[dict]:
    """過去の納税/<year>/ にある年度ディレクトリを列挙。
    各年度の CSV/PDF の有無も返す (UI 表示用)。
    """
    out = []
    if not TAX_DIR.exists():
        return out
    for p in sorted(TAX_DIR.iterdir(), reverse=True):
        if not p.is_dir():
            continue
        try:
            year = int(p.name)
        except ValueError:
            continue
        csv_files = list(p.glob("*.csv"))
        pdf_files = list(p.glob("*.pdf"))
        has_journal = any("仕訳帳" in f.name for f in csv_files)
        has_pl = any("損益計算書" in f.name for f in csv_files)
        has_bs = any("貸借対照表" in f.name for f in csv_files)
        has_decl_pdf = any("青色申告決算書" in f.name for f in pdf_files)
        out.append({
            "year": year,
            "path": str(p),
            "has_journal_csv": has_journal,
            "has_pl_csv": has_pl,
            "has_bs_csv": has_bs,
            "has_decl_pdf": has_decl_pdf,
            "csv_count": len(csv_files),
            "pdf_count": len(pdf_files),
        })
    return out


def _import_pdf_extracted_values(con: sqlite3.Connection, year: int,
                                   year_dir: Path) -> dict:
    """申告書等送信票 PDF をテキスト化 → 第一表 KPI と控除明細を抽出して
    aoiro_declaration_inputs / aoiro_deductions に保存。
    """
    from src.aoiro.pdf_parser import extract_all
    from src.aoiro.forms import (get_declaration_inputs,
                                  save_declaration_inputs,
                                  salary_income_deduction)
    # 申告書等送信票 / 確定申告書 / 青色申告決算書 PDF のテキスト抽出
    送信票_text = ""
    try:
        from markitdown import MarkItDown
        md = MarkItDown()
        # 優先順位: 送信票 > 申告内容確認票 > 確定申告書 > 青色申告決算書
        priority_keywords = ["送信票", "申告内容確認", "確定申告書", "青色申告決算書", "青色決算書"]
        candidates = []
        for kw in priority_keywords:
            for p in year_dir.glob("*.pdf"):
                if kw in p.name and p not in candidates:
                    candidates.append(p)
                    break
        if candidates:
            送信票_text = md.convert(str(candidates[0])).text_content
    except (ImportError, Exception):
        pass
    if not 送信票_text:
        return {"first_table": {}, "deductions_inserted": 0,
                "deductions_skipped": 0, "inputs_saved": False}

    parsed = extract_all(送信票_text)
    first = parsed["first_table"]
    extracted_deductions = parsed["deductions"]

    # 1) declaration_inputs を更新
    inputs = dict(get_declaration_inputs(con, year))
    if first.get("salary_income"):
        inputs["salary_income"] = first["salary_income"]
    elif first.get("salary_revenue"):
        # 給与所得 (6) が取れなければ 給与収入 (オ) から計算
        inputs["salary_income"] = first["salary_revenue"] - salary_income_deduction(first["salary_revenue"])
    if first.get("withholding_tax"):
        inputs["withholding_tax"] = first["withholding_tax"]
    if first.get("tax_credits"):
        # 令和6年定額減税等 (PDF (44) 番)
        inputs["tax_credits"] = first["tax_credits"]
    save_declaration_inputs(con, year, inputs)

    # 2) aoiro_deductions: 同年・payee='PDF 自動抽出' のレコードを削除して再登録
    con.execute(
        "DELETE FROM aoiro_deductions WHERE fiscal_year=? AND payee='PDF 自動抽出'",
        (year,),
    )
    inserted = 0
    skipped_existing = 0
    for d in extracted_deductions:
        # 同 kind が手入力済 (payee != 'PDF 自動抽出') の場合は重複を避けるため skip
        cur = con.execute(
            "SELECT 1 FROM aoiro_deductions WHERE fiscal_year=? AND kind=? "
            "AND (payee != 'PDF 自動抽出' OR payee IS NULL)",
            (year, d["kind"]),
        )
        if cur.fetchone():
            skipped_existing += 1
            continue
        con.execute(
            "INSERT INTO aoiro_deductions "
            "(fiscal_year, kind, payee, amount, note) VALUES (?,?,?,?,?)",
            (year, d["kind"], d["payee"], d["amount"], d.get("note", "")),
        )
        inserted += 1
    con.commit()
    return {
        "first_table": first,
        "deductions_inserted": inserted,
        "deductions_skipped": skipped_existing,
        "inputs_saved": True,
    }


def import_from_dir(con: sqlite3.Connection, year: int) -> dict:
    """過去の納税/<year>/ から CSV + PDF を取込んで snapshot まで作成する
    一気通貫処理。

    1) 科目マスタ.csv → aoiro_accounts (新規のみ追加)
    2) 仕訳帳/PL/BS CSV + 決算書 PDF → tax_* テーブル
    3) 申告書等送信票.pdf → MarkItDown 抽出 → 第一表 KPI と控除明細を
       aoiro_declaration_inputs / aoiro_deductions に保存
    4) tax_* + declaration_inputs + deductions → snapshot に変換

    既に lock 済の snapshot がある場合は ValueError。
    """
    year_dir = TAX_DIR / str(year)
    if not year_dir.exists() or not year_dir.is_dir():
        raise FileNotFoundError(f"directory not found: {year_dir}")

    # 1) 科目マスタ CSV → aoiro_accounts (CSV 無ければ no-op)
    accounts_result = register_legacy_accounts(con, year_dir)

    # 2) tax_* に取込 (CSV 無くてもエラーにしない)
    try:
        from src.tax.importer import import_year as _tax_import_year
        tax_result = _tax_import_year(year_dir, con)
    except Exception as e:
        tax_result = {"journal": 0, "pl": 0, "bs": 0, "docs": 0,
                      "error": str(e)}

    # 3) PDF (申告書等送信票) → declaration_inputs + deductions に自動取込
    pdf_result = _import_pdf_extracted_values(con, year, year_dir)

    # 4) tax_* → snapshot に変換 (deductions/inputs が増えた状態で集計)
    snap_result = import_legacy_year(con, year)
    return {
        **snap_result,
        "tax_import": tax_result,
        "accounts_import": accounts_result,
        "pdf_import": pdf_result,
        "source_dir": str(year_dir),
    }


def list_legacy_years(con: sqlite3.Connection) -> list[int]:
    """tax_pl / tax_bs / tax_journal に存在する年度を新しい順で。"""
    rows = con.execute(
        "SELECT DISTINCT y FROM ("
        "  SELECT fiscal_year y FROM tax_pl UNION "
        "  SELECT fiscal_year y FROM tax_bs UNION "
        "  SELECT fiscal_year y FROM tax_journal"
        ") ORDER BY y DESC"
    ).fetchall()
    return [r[0] for r in rows]


def _resolve_account(con: sqlite3.Connection, name: str,
                     type_hint: str) -> dict:
    """旧 account 名 → aoiro_accounts の {code, name, category, sort_order, type}。
    一致しなければ仮 code (X-<name>) で dict を返す (DB には追加しない)。
    """
    if not name:
        return {"code": "X-blank", "name": "(空)", "category": "(legacy)",
                "sort_order": 9999, "type": type_hint}
    cur = con.execute(
        "SELECT code, name, category, sort_order, type FROM aoiro_accounts WHERE name=?",
        (name,),
    )
    row = cur.fetchone()
    if row:
        return dict(row)
    # 代替: 部分一致 (短い側を含む方を優先)
    cur = con.execute(
        "SELECT code, name, category, sort_order, type FROM aoiro_accounts "
        "WHERE name LIKE '%'||?||'%' OR ? LIKE '%'||name||'%' "
        "ORDER BY length(name) DESC LIMIT 1",
        (name, name),
    )
    row = cur.fetchone()
    if row:
        return dict(row)
    return {"code": f"X-{name}", "name": name, "category": "(legacy)",
            "sort_order": 9999, "type": type_hint}


def build_pl_from_legacy(con: sqlite3.Connection, year: int) -> dict:
    revenue_lines: list[dict] = []
    expense_lines: list[dict] = []
    for r in con.execute(
        "SELECT * FROM tax_pl WHERE fiscal_year=?", (year,)
    ).fetchall():
        d = dict(r)
        cb = int(d.get("credit_balance") or 0)
        db = int(d.get("debit_balance") or 0)
        if cb == 0 and db == 0:
            continue
        # 残高の大きい側で勘定区分を推定 (revenue=credit, expense=debit)
        if cb >= db:
            acc = _resolve_account(con, d["account"], "revenue")
            revenue_lines.append({
                "code": acc["code"], "name": acc["name"],
                "category": acc.get("category"), "amount": cb,
                "sort_order": acc.get("sort_order") or 0,
            })
        else:
            acc = _resolve_account(con, d["account"], "expense")
            expense_lines.append({
                "code": acc["code"], "name": acc["name"],
                "category": acc.get("category"), "amount": db,
                "sort_order": acc.get("sort_order") or 0,
            })
    revenue_lines.sort(key=lambda x: (x["sort_order"], x["code"]))
    expense_lines.sort(key=lambda x: (x["sort_order"], x["code"]))
    rev_total = sum(x["amount"] for x in revenue_lines)
    exp_total = sum(x["amount"] for x in expense_lines)
    return {
        "year": year,
        "revenue_lines": revenue_lines,
        "expense_lines": expense_lines,
        "revenue_total": rev_total,
        "expense_total": exp_total,
        "income_before_special": rev_total - exp_total,
        "cogs_breakdown": {"5001": 0, "5002": 0, "5003": 0},
        "cogs_total": 0,
        "monthly_revenue": [],
        "monthly_purchase": [],
    }


def build_bs_from_legacy(con: sqlite3.Connection, year: int,
                          pl_net_income: int | None = None) -> dict:
    """旧 BS データから aoiro snapshot 形式の BS を構築。
    pl_net_income を渡すと balance_check がより正確に計算される
    (旧 BS の事業主借は期末時点で純損失を吸収済のため、equity に
     PL の純利益を加算した値を「資本合計」として扱う)。
    """
    if pl_net_income is None:
        pl = build_pl_from_legacy(con, year)
        pl_net_income = pl["income_before_special"]
    asset, liability, equity = [], [], []
    for r in con.execute(
        "SELECT * FROM tax_bs WHERE fiscal_year=?", (year,)
    ).fetchall():
        d = dict(r)
        db = int(d.get("debit_balance") or 0)
        cb = int(d.get("credit_balance") or 0)
        if db == 0 and cb == 0:
            continue
        # 借方残 = 資産扱い (※事業主貸も借方残だが資本)
        # 貸方残 = 負債/資本 (元入金/事業主借)
        if db > 0 and cb == 0:
            acc = _resolve_account(con, d["account"], "asset")
            line = {
                "code": acc["code"], "name": acc["name"],
                "category": acc.get("category"),
                "opening": 0, "ending": db,
                "debit_total": 0, "credit_total": 0,
                "sort_order": acc.get("sort_order") or 0,
            }
            if acc["type"] == "asset":
                asset.append(line)
            elif acc["type"] == "equity":
                equity.append(line)
            else:
                asset.append(line)
        else:
            acc = _resolve_account(con, d["account"], "liability")
            line = {
                "code": acc["code"], "name": acc["name"],
                "category": acc.get("category"),
                "opening": 0, "ending": cb,
                "debit_total": 0, "credit_total": 0,
                "sort_order": acc.get("sort_order") or 0,
            }
            if acc["type"] == "liability":
                liability.append(line)
            elif acc["type"] == "equity":
                equity.append(line)
            else:
                liability.append(line)
    for lst in (asset, liability, equity):
        lst.sort(key=lambda x: (x["sort_order"], x["code"]))
    a_total = sum(x["ending"] for x in asset)
    l_total = sum(x["ending"] for x in liability)
    e_total = sum(x["ending"] for x in equity)
    return {
        "year": year,
        "asset_lines": asset,
        "liability_lines": liability,
        "equity_lines": equity,
        "asset_total": a_total,
        "liability_total": l_total,
        "equity_total": e_total,
        "net_income": pl_net_income,
        "equity_total_with_income": e_total + pl_net_income,
        "balance_check": a_total - (l_total + e_total + pl_net_income),
    }


def build_journal_from_legacy(con: sqlite3.Connection, year: int) -> list[dict]:
    out = []
    for r in con.execute(
        "SELECT id, fiscal_year, date, debit_account, debit_amount, "
        "       credit_account, credit_amount, description, imported_at "
        "FROM tax_journal WHERE fiscal_year=? ORDER BY date, id", (year,)
    ).fetchall():
        d = dict(r)
        out.append({
            "id": d["id"], "fiscal_year": d["fiscal_year"], "date": d["date"],
            "debit_account": d["debit_account"] or "",
            "debit_amount": int(d["debit_amount"] or 0),
            "credit_account": d["credit_account"] or "",
            "credit_amount": int(d["credit_amount"] or 0),
            "description": d["description"] or "",
            "source": "legacy",
            "source_tx_id": None, "source_rule_id": None,
            "created_at": d.get("imported_at"),
        })
    return out


def preview_legacy_year(con: sqlite3.Connection, year: int) -> dict:
    """import 前の preview。実際の保存はしない。"""
    pl = build_pl_from_legacy(con, year)
    bs = build_bs_from_legacy(con, year, pl_net_income=pl["income_before_special"])
    journal = build_journal_from_legacy(con, year)
    return {
        "year": year,
        "pl_revenue_lines": len(pl["revenue_lines"]),
        "pl_expense_lines": len(pl["expense_lines"]),
        "pl_revenue_total": pl["revenue_total"],
        "pl_expense_total": pl["expense_total"],
        "pl_income": pl["income_before_special"],
        "bs_asset_lines": len(bs["asset_lines"]),
        "bs_liability_lines": len(bs["liability_lines"]),
        "bs_equity_lines": len(bs["equity_lines"]),
        "bs_balance_check": bs["balance_check"],
        "journal_count": len(journal),
        "unmapped_accounts": [
            x["name"] for x in pl["revenue_lines"] + pl["expense_lines"] +
                             bs["asset_lines"] + bs["liability_lines"] + bs["equity_lines"]
            if x["code"].startswith("X-")
        ],
    }


def import_legacy_year(con: sqlite3.Connection, year: int) -> dict:
    """tax_* から snapshot を生成して aoiro_declaration_runs に保存。
    既に lock 済みの snapshot は更新拒否 (ValueError)。
    """
    cur = con.execute(
        "SELECT locked_at FROM aoiro_declaration_runs WHERE fiscal_year=?", (year,)
    )
    row = cur.fetchone()
    if row and row["locked_at"]:
        raise ValueError(f"snapshot {year} is locked")

    pl = build_pl_from_legacy(con, year)
    bs = build_bs_from_legacy(con, year, pl_net_income=pl["income_before_special"])
    journal = build_journal_from_legacy(con, year)
    # 既存の aoiro_deductions テーブルから控除データを取得 (ユーザが Phase 6
    # UI で入力済みなら反映、未入力なら空サマリ)
    ded = fetch_deductions_for_year(con, year)
    deduction_total = int(ded.get("grand_total") or 0)
    # 申告書 B 入力値 (給与・源泉・特別控除等) を年度別の保存テーブルから取得
    from src.aoiro.forms import get_declaration_inputs
    inputs = get_declaration_inputs(con, year)
    salary_income = int(inputs.get("salary_income") or 0)
    other_income = int(inputs.get("other_income") or 0)
    spd_biz = int(inputs.get("special_deduction_business") or 0)
    spd_re = int(inputs.get("special_deduction_real_estate") or 0)
    withholding_tax = int(inputs.get("withholding_tax") or 0)
    estimated_tax = int(inputs.get("estimated_tax") or 0)
    tax_credits = int(inputs.get("tax_credits") or 0)
    # 事業所得: business_raw_override が指定されていれば PL を無視してその値を使う
    # (CSV が無い古い年度で PDF の値を直接入れる用途)
    override = inputs.get("business_raw_override")
    if override is not None:
        biz_raw = int(override)
    else:
        biz_raw = pl["income_before_special"]
    # 黒字なら特控適用 + 0 floor、赤字なら損益通算 (マイナスのまま)
    if biz_raw >= 0:
        biz_income = max(0, biz_raw - spd_biz)
    else:
        biz_income = biz_raw  # 給与等と通算
    income_total = biz_income + salary_income + other_income
    taxable_raw = max(0, income_total - deduction_total)
    taxable_income = taxable_raw // 1000 * 1000
    # 税額計算
    from src.aoiro.forms import income_tax, reconstruction_surtax, resident_tax
    inc_tax = income_tax(taxable_income)
    inc_tax_after = max(0, inc_tax - tax_credits)
    surtax = reconstruction_surtax(inc_tax_after)
    total_tax = inc_tax_after + surtax
    final = total_tax - withholding_tax - estimated_tax
    decl = {
        "year": year,
        "income": {
            "business_raw": biz_raw,
            "business_raw_pl_only": biz_raw,
            "business_special_deduction": spd_biz,
            "business": biz_income,
            "real_estate_raw": 0,
            "real_estate_special_deduction": spd_re,
            "real_estate": 0,
            "salary": salary_income, "other": other_income,
            "total": income_total,
        },
        "deduction_total": deduction_total,
        "taxable_income_raw": taxable_raw,
        "taxable_income": taxable_income,
        "tax": {
            "income_tax": inc_tax, "tax_credits": tax_credits,
            "income_tax_after_credit": inc_tax_after,
            "reconstruction_surtax": surtax, "total_tax": total_tax,
            "withholding_tax": withholding_tax,
            "estimated_tax": estimated_tax,
            "final_payable": max(0, final), "final_refund": max(0, -final),
            "resident_tax_estimate": resident_tax(taxable_income),
        },
        "pl": pl, "bs": bs,
        "real_estate": {
            "year": year, "rows": [],
            "total_gross": 0, "total_vacancy": 0,
            "total_expenses": 0, "total_net_income": 0,
        },
        "deductions": ded,
    }
    # 過去の納税/<year>/ 配下に PDF/data ファイルがあれば documents に列挙
    documents = collect_documents(TAX_DIR / str(year))
    # PDF を MarkItDown でテキスト化 (重い処理だが import 時のみ)
    documents_text = extract_pdf_text(TAX_DIR / str(year))

    # 新スキーマ section (rental_contracts / accounts / invoice 等) も含める
    # 既存 DB の値をそのまま埋め込み (legacy 年度には通常データ無いので空が多い)
    try:
        from src.aoiro.accounts import list_accounts
        snap_accounts = list_accounts(con)
    except Exception:
        snap_accounts = []
    try:
        from src.aoiro.rental_contracts import (
            annual_rent_total as _rental_summary,
            list_contracts as list_rental_contracts,
        )
        snap_rentals = {
            "contracts": list_rental_contracts(con, fiscal_year=year),
            "summary": _rental_summary(con, fiscal_year=year),
        }
    except Exception:
        snap_rentals = {"contracts": [], "summary": {"contract_count": 0}}
    try:
        from src.aoiro.business_ratios import list_business_ratios
        snap_br = list_business_ratios(con, fiscal_year=year)
    except Exception:
        snap_br = []

    snap = {
        "fiscal_year": year,
        "captured_at": datetime.now().isoformat(),
        "pl": pl, "bs": bs,
        "journal": {"entries": journal, "count": len(journal)},
        "fixed_assets": {"assets": [], "count": 0},
        "real_estate": {
            "summary": {
                "year": year, "rows": [],
                "total_gross": 0, "total_vacancy": 0,
                "total_expenses": 0, "total_net_income": 0,
            },
            "properties": [], "rents": [], "landlord_transactions": [],
        },
        "deductions": ded,
        "medical_transactions": [],
        "insurance_transactions": [],
        "opening_balances": [],
        "business_ratios": snap_br,
        "rules_used": [],
        "payment_accounts": [],
        "accounts": snap_accounts,
        "rental_contracts": snap_rentals,
        "declaration": decl,
        "documents": documents,
        "documents_text": documents_text,
        "_imported_from_legacy": True,
    }
    snap_str = json.dumps(snap, ensure_ascii=False)
    pl_str = json.dumps(pl, ensure_ascii=False)
    bs_str = json.dumps(bs, ensure_ascii=False)
    decl_str = json.dumps(decl, ensure_ascii=False)
    now = datetime.now().isoformat()
    if row:
        con.execute(
            "UPDATE aoiro_declaration_runs SET snapshot_json=?, pl_json=?, "
            "bs_json=?, declaration_json=?, updated_at=? WHERE fiscal_year=?",
            (snap_str, pl_str, bs_str, decl_str, now, year),
        )
    else:
        con.execute(
            "INSERT INTO aoiro_declaration_runs "
            "(fiscal_year, snapshot_json, pl_json, bs_json, "
            " declaration_json, updated_at) VALUES (?,?,?,?,?,?)",
            (year, snap_str, pl_str, bs_str, decl_str, now),
        )
    con.commit()
    return {
        "year": year,
        "pl_lines": len(pl["revenue_lines"]) + len(pl["expense_lines"]),
        "bs_lines": len(bs["asset_lines"]) + len(bs["liability_lines"]) + len(bs["equity_lines"]),
        "journal": len(journal),
    }
