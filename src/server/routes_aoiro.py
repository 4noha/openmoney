"""青色申告ページ /aoiro と関連 API。

src.aoiro モジュールへの薄い HTTP ラッパ。FastAPI app 以外の
src.server.* には依存しない (疎結合)。
"""
from __future__ import annotations

from fastapi import File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from src.aoiro.accounts import (
    delete_account,
    init_with_seed,
    list_accounts,
    upsert_account,
)
from src.aoiro.aggregate import (
    compute_bs,
    compute_pl,
    delete_opening_balance,
    list_opening_balances,
    upsert_opening_balance,
)
from src.aoiro.closing import rebuild_closing
from src.aoiro.business_ratios import (
    delete_business_ratio,
    list_business_ratios,
    seed_template as seed_business_ratio_template,
    upsert_business_ratio,
)
from src.aoiro.deductions import (
    KIND_ORDER as DEDUCTION_KINDS,
    bulk_add_from_transactions,
    delete_deduction,
    list_deductions,
    list_insurance_transactions,
    list_medical_transactions,
    upsert_deduction,
    yearly_summary as deduction_summary,
)
from src.aoiro.depreciation import (
    delete_asset,
    list_assets,
    list_schedules_for_year,
    rebuild_depreciation,
    schedule as asset_schedule,
    upsert_asset,
)
from src.aoiro.engine import (
    delete_entry,
    insert_manual_entry,
    list_journal,
    rebuild_year,
)
from src.aoiro.exports import (
    export_bs as csv_bs,
    export_card_monthly as csv_card_monthly,
    export_deductions as csv_deductions,
    export_fixed_assets as csv_fixed_assets,
    export_general_ledger as csv_general_ledger,
    export_journal as csv_journal,
    export_mfcloud_journal as csv_mf_journal,
    export_monthly_pl as csv_monthly_pl,
    export_pl as csv_pl,
    export_pl_yoy as csv_pl_yoy,
    export_yayoi_journal as csv_yayoi_journal,
    export_real_estate as csv_real_estate,
    export_rental_contracts as csv_rental_contracts,
    export_trial_balance as csv_trial_balance,
)
from src.aoiro.forms import (
    SPECIAL_DEDUCTION_BLUE_E_TAX,
    SPECIAL_DEDUCTION_OPTIONS,
    compute_declaration,
    delete_snapshot,
    get_declaration_inputs,
    get_snapshot,
    salary_revenue_from_transactions,
    save_declaration_inputs,
    save_snapshot,
    unlock_snapshot,
)
from src.aoiro.real_estate import (
    delete_property,
    delete_rent,
    list_landlord_transactions,
    list_properties,
    list_rents,
    reflect_from_transactions,
    upsert_property,
    upsert_rent,
    yearly_summary,
)
from src.aoiro.import_legacy import (
    import_from_dir,
    import_legacy_year,
    list_dir_years,
    list_legacy_years,
    preview_legacy_year,
    register_legacy_accounts,
)
from src.aoiro.snapshots import (
    compare_snapshots,
    delete_snapshot_full,
    get_section as snapshot_get_section,
    get_snapshot_full,
    list_snapshots,
    lock_snapshot,
    save_snapshot_full,
    unlock_snapshot as unlock_snapshot_full,
)
from src.aoiro.rules import (
    delete_rule,
    list_payment_accounts,
    list_rules,
    upsert_payment_account,
    upsert_rule,
)
from src.server import app


# 起動時に schema + seed を走らせる (冪等)
_initialized = False


def _con():
    global _initialized
    from src.aoiro.schema import open_db
    if not _initialized:
        init_with_seed().close()
        _initialized = True
    return open_db()


# ─────────────────────────────────────────────
# ページ
# ─────────────────────────────────────────────
@app.get("/blue_form", response_class=HTMLResponse)
async def blue_form_page():
    from src.server.templates_helper import render
    return HTMLResponse(render("aoiro.html"))


# ─────────────────────────────────────────────
# 勘定科目マスタ
# ─────────────────────────────────────────────
@app.get("/api/aoiro/accounts")
async def api_aoiro_accounts(type: str = "", active_only: bool = False):
    con = _con()
    try:
        rows = list_accounts(con, type_=type or None, active_only=active_only)
        return {"accounts": rows}
    finally:
        con.close()


class AccountBody(BaseModel):
    code: str = Field(..., min_length=1, max_length=16)
    name: str = Field(..., min_length=1, max_length=64)
    type: str
    category: str = ""
    sort_order: int = 0
    note: str = ""
    is_active: bool = True


@app.post("/api/aoiro/accounts")
async def api_aoiro_account_upsert(body: AccountBody):
    con = _con()
    try:
        try:
            upsert_account(
                con,
                code=body.code.strip(),
                name=body.name.strip(),
                type_=body.type,
                category=body.category.strip(),
                sort_order=body.sort_order,
                note=body.note.strip(),
                is_active=body.is_active,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "ok"}
    finally:
        con.close()


@app.delete("/api/aoiro/accounts/{code}")
async def api_aoiro_account_delete(code: str):
    con = _con()
    try:
        ok = delete_account(con, code)
        if not ok:
            raise HTTPException(status_code=400, detail="builtin account or not found")
        return {"status": "ok"}
    finally:
        con.close()


# ─────────────────────────────────────────────
# 仕訳ルール
# ─────────────────────────────────────────────
class RuleBody(BaseModel):
    id: int | None = None
    name: str = Field(..., min_length=1, max_length=128)
    category_match: str = ""
    tag_match: str = ""
    bank_match: str = ""
    keyword_match: str = ""
    min_amount: int = 0
    max_amount: int = 0
    debit_account: str = ""
    credit_account: str = ""
    priority: int = 100
    is_active: bool = True
    business_ratio: int = 100


@app.get("/api/aoiro/rules")
async def api_aoiro_rules():
    con = _con()
    try:
        return {"rules": list_rules(con)}
    finally:
        con.close()


@app.post("/api/aoiro/rules")
async def api_aoiro_rule_upsert(body: RuleBody):
    con = _con()
    try:
        rid = upsert_rule(
            con, id_=body.id, name=body.name.strip(),
            category_match=body.category_match.strip(),
            tag_match=body.tag_match.strip(),
            bank_match=body.bank_match.strip(),
            keyword_match=body.keyword_match.strip(),
            min_amount=body.min_amount, max_amount=body.max_amount,
            debit_account=body.debit_account.strip(),
            credit_account=body.credit_account.strip(),
            priority=body.priority, is_active=body.is_active,
            business_ratio=body.business_ratio,
        )
        return {"status": "ok", "id": rid}
    finally:
        con.close()


@app.delete("/api/aoiro/rules/{rule_id}")
async def api_aoiro_rule_delete(rule_id: int):
    con = _con()
    try:
        if not delete_rule(con, rule_id):
            raise HTTPException(status_code=400, detail="builtin or not found")
        return {"status": "ok"}
    finally:
        con.close()


# ─────────────────────────────────────────────
# bank → 支払/受取科目マップ
# ─────────────────────────────────────────────
class PaymentBody(BaseModel):
    bank: str = Field(..., min_length=1)
    expense_credit_account: str
    income_debit_account: str = ""
    note: str = ""


@app.get("/api/aoiro/payments")
async def api_aoiro_payments():
    con = _con()
    try:
        return {"payments": list_payment_accounts(con)}
    finally:
        con.close()


@app.post("/api/aoiro/payments")
async def api_aoiro_payment_upsert(body: PaymentBody):
    con = _con()
    try:
        upsert_payment_account(
            con, bank=body.bank.strip(),
            expense_credit_account=body.expense_credit_account.strip(),
            income_debit_account=body.income_debit_account.strip(),
            note=body.note.strip(),
        )
        return {"status": "ok"}
    finally:
        con.close()


# ─────────────────────────────────────────────
# 仕訳帳
# ─────────────────────────────────────────────
@app.get("/api/aoiro/journal")
async def api_aoiro_journal(year: int = 0, source: str = "", limit: int = 5000):
    con = _con()
    try:
        rows = list_journal(con, year=year or None, source=source or None, limit=limit)
        return {"entries": rows, "count": len(rows)}
    finally:
        con.close()


class ManualEntryBody(BaseModel):
    fiscal_year: int
    date: str
    debit_account: str
    debit_amount: int
    credit_account: str
    credit_amount: int
    description: str = ""


@app.post("/api/aoiro/journal/manual")
async def api_aoiro_journal_manual(body: ManualEntryBody):
    con = _con()
    try:
        eid = insert_manual_entry(
            con, fiscal_year=body.fiscal_year, date=body.date,
            debit_account=body.debit_account, debit_amount=body.debit_amount,
            credit_account=body.credit_account, credit_amount=body.credit_amount,
            description=body.description,
        )
        return {"status": "ok", "id": eid}
    finally:
        con.close()


@app.delete("/api/aoiro/journal/{entry_id}")
async def api_aoiro_journal_delete(entry_id: int):
    con = _con()
    try:
        if not delete_entry(con, entry_id):
            raise HTTPException(status_code=400, detail="auto entry or not found")
        return {"status": "ok"}
    finally:
        con.close()


@app.post("/api/aoiro/journal/rebuild")
async def api_aoiro_journal_rebuild(year: int):
    con = _con()
    try:
        return rebuild_year(con, year=year)
    finally:
        con.close()


@app.post("/api/aoiro/closing/rebuild")
async def api_aoiro_closing_rebuild(year: int):
    """期末整理仕訳 (事業主借/事業主貸 → 元入金 振替) を再生成。
    年度末 12/31 付で source='closing' として書き込む。"""
    con = _con()
    try:
        return rebuild_closing(con, year)
    finally:
        con.close()


@app.post("/api/aoiro/attachments")
async def api_aoiro_attachment_upload(file: UploadFile = File(...),
                                       journal_entry_id: int | None = None,
                                       run_ocr: bool = False):
    """領収書ファイルをアップロードして保存 (任意で OCR テキストも保存)。"""
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty file")
    con = _con()
    try:
        from src.aoiro.attachments import save_attachment
        ocr_text = None
        if run_ocr:
            from src.aoiro.ocr import extract_receipt
            try:
                ocr = extract_receipt(content, file.filename or "upload.bin")
                ocr_text = ocr.get("raw_text")
            except Exception:
                pass
        result = save_attachment(
            con, file_bytes=content, filename=file.filename or "upload.bin",
            mime_type=file.content_type or "",
            journal_entry_id=journal_entry_id, ocr_text=ocr_text,
        )
        return result
    finally:
        con.close()


@app.get("/api/aoiro/attachments")
async def api_aoiro_attachment_list(journal_entry_id: int | None = None):
    con = _con()
    try:
        from src.aoiro.attachments import list_attachments
        return {"attachments": list_attachments(con, journal_entry_id)}
    finally:
        con.close()


@app.get("/api/aoiro/attachments/{attachment_id}/file")
async def api_aoiro_attachment_file(attachment_id: int):
    """添付ファイルの実体を返す。"""
    from fastapi.responses import FileResponse
    con = _con()
    try:
        from src.aoiro.attachments import get_attachment
        att = get_attachment(con, attachment_id)
        if not att:
            raise HTTPException(status_code=404, detail="not found")
        from pathlib import Path as _P
        p = _P(att["stored_path"])
        if not p.exists():
            raise HTTPException(status_code=404, detail="file missing")
        return FileResponse(str(p), media_type=att["mime_type"] or "application/octet-stream",
                             filename=att["filename"])
    finally:
        con.close()


@app.delete("/api/aoiro/attachments/{attachment_id}")
async def api_aoiro_attachment_delete(attachment_id: int):
    con = _con()
    try:
        from src.aoiro.attachments import delete_attachment
        ok = delete_attachment(con, attachment_id)
        if not ok:
            raise HTTPException(status_code=404, detail="not found")
        return {"status": "ok"}
    finally:
        con.close()


@app.post("/api/aoiro/ocr/receipt")
async def api_aoiro_ocr_receipt(file: UploadFile = File(...)):
    """レシート画像/PDF をアップロードして仕訳候補を返す。
    - file: multipart/form-data の file フィールド
    """
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty file")
    from src.aoiro.ocr import extract_receipt
    return extract_receipt(content, file.filename or "upload.bin")


@app.get("/api/aoiro/rental-contracts/estimate-monthly")
async def api_aoiro_rental_estimate_monthly(keyword: str, year: int | None = None,
                                              min_amount: int = 10000):
    """transactions から keyword を含む振込/引落の median (月額参考) と
    指定年度の合計 (= 申告書「本年賃借料」) を返す。
    """
    if not keyword.strip():
        raise HTTPException(status_code=400, detail="keyword required")
    con = _con()
    try:
        rows = con.execute("""
            SELECT date, debit, description FROM transactions
            WHERE debit >= ? AND description LIKE ?
            ORDER BY date DESC LIMIT 100
        """, (min_amount, f"%{keyword.strip()}%")).fetchall()
        amounts = [int(r["debit"]) for r in rows if r["debit"]]
        if not amounts:
            return {"keyword": keyword, "match_count": 0,
                    "monthly_rent": 0, "annual_rent": 0,
                    "samples": [], "note": f"'{keyword}' に該当する取引なし"}
        from statistics import median
        med = int(median(amounts))
        # 指定年度の合計 (= 本年賃借料)
        annual = 0
        in_year_count = 0
        if year:
            yyyy = f"{year:04d}"
            for r in rows:
                if (r["date"] or "")[:4] == yyyy:
                    annual += int(r["debit"] or 0)
                    in_year_count += 1
        samples = [{"date": r["date"], "amount": int(r["debit"]),
                     "desc": (r["description"] or "")[:60]} for r in rows[:8]]
        return {
            "keyword": keyword, "match_count": len(amounts),
            "monthly_rent": med, "annual_rent": annual,
            "in_year_count": in_year_count, "year": year,
            "samples": samples,
            "min_amount": min(amounts), "max_amount": max(amounts),
        }
    finally:
        con.close()


@app.get("/api/aoiro/rental-contracts/parse-pdf")
async def api_aoiro_rental_parse_pdf(year: int):
    """過去の納税/<year>/青色申告決算書.pdf から賃借物件候補を抽出。
    transactions の支払履歴から月額を実費推定する (PDF 年額/12 より正確)。
    """
    from src.aoiro.rental_pdf_import import find_kessan_pdf, parse_rental_section
    pdf = find_kessan_pdf(year)
    if pdf is None:
        return {"found": False, "error": f"過去の納税/{year}/ に決算書 PDF 無し"}
    con = _con()
    try:
        r = parse_rental_section(pdf, con=con)
        r["pdf_path"] = str(pdf)
        return r
    finally:
        con.close()


@app.get("/api/aoiro/rental-contracts")
async def api_aoiro_rental_contracts_list(year: int | None = None):
    """賃借物件 (地代家賃) の契約一覧。年度別記録。
    year を指定すれば該当年度のみ、未指定なら全年度返す。
    """
    con = _con()
    try:
        from src.aoiro.rental_contracts import annual_rent_total, list_contracts
        contracts = list_contracts(con, fiscal_year=year)
        summary = annual_rent_total(con, fiscal_year=year) if year else {
            "fiscal_year": None,
            "contract_count": len(contracts),
            "annual_rent_gross": sum(int(c["annual_rent"] or 0) for c in contracts),
            "annual_rent_business": sum(
                int(c["annual_rent"] or 0) * (c["business_ratio_pct"] or 100) // 100
                for c in contracts
            ),
        }
        return {"contracts": contracts, "summary": summary}
    finally:
        con.close()


class RentalContractBody(BaseModel):
    id: int | None = None
    fiscal_year: int
    property_name: str
    property_address: str = ""
    area_sqm: float = 0
    usage: str = ""
    business_ratio_pct: int = 100
    annual_rent: int = 0
    monthly_rent: int = 0
    deposit: int = 0
    management_fee: int = 0
    landlord_name: str = ""
    landlord_address: str = ""
    landlord_phone: str = ""
    note: str = ""


@app.post("/api/aoiro/rental-contracts")
async def api_aoiro_rental_contract_save(body: RentalContractBody):
    con = _con()
    try:
        from src.aoiro.rental_contracts import upsert_contract
        new_id = upsert_contract(
            con, id_=body.id, fiscal_year=body.fiscal_year,
            property_name=body.property_name,
            property_address=body.property_address, area_sqm=body.area_sqm,
            usage=body.usage, business_ratio_pct=body.business_ratio_pct,
            annual_rent=body.annual_rent,
            monthly_rent=body.monthly_rent, deposit=body.deposit,
            management_fee=body.management_fee,
            landlord_name=body.landlord_name,
            landlord_address=body.landlord_address,
            landlord_phone=body.landlord_phone, note=body.note,
        )
        return {"id": new_id}
    finally:
        con.close()


@app.delete("/api/aoiro/rental-contracts/{id_}")
async def api_aoiro_rental_contract_delete(id_: int):
    con = _con()
    try:
        from src.aoiro.rental_contracts import delete_contract
        if not delete_contract(con, id_):
            raise HTTPException(status_code=404, detail="not found")
        return {"status": "ok"}
    finally:
        con.close()


@app.post("/api/aoiro/rental-contracts/copy")
async def api_aoiro_rental_contracts_copy(source_year: int, target_year: int):
    """source_year の全物件を target_year にコピー (前年から複製)。"""
    con = _con()
    try:
        from src.aoiro.rental_contracts import copy_to_year
        n = copy_to_year(con, source_year, target_year)
        return {"copied": n, "source_year": source_year, "target_year": target_year}
    finally:
        con.close()


@app.get("/api/aoiro/uncovered-transactions")
async def api_aoiro_uncovered_transactions(year: int):
    """当年度の transactions のうち、aoiro_journal_entries に紐付かない (= 仕訳化漏れ) を返す。

    除外:
      - aoiro_journal_entries に source_tx_id でリンク済
      - tx_links (shop_card / amazon_split) で tx_b として skip 対象
      - category が事業仕訳対象外 (None / '今回は経費' など categorize 中間状態)
    """
    con = _con()
    try:
        yyyy = f"{year:04d}"
        journaled: set[int] = set()
        for r in con.execute(
            "SELECT DISTINCT source_tx_id FROM aoiro_journal_entries "
            "WHERE fiscal_year=? AND source_tx_id IS NOT NULL", (year,)
        ).fetchall():
            journaled.add(int(r[0]))
        redundant: set[int] = set()
        for r in con.execute(
            "SELECT tx_b_id FROM tx_links "
            "WHERE link_type IN ('shop_card','amazon_split') AND tx_b_id IS NOT NULL"
        ).fetchall():
            redundant.add(int(r[0]))

        target_cats = ("経費", "売上", "個人支出", "出金", "給与", "非課税",
                        "保険金", "返金", "家賃収入")
        ph = ",".join("?" * len(target_cats))
        rows = []
        for r in con.execute(
            f"SELECT id, bank, date, debit, credit, description, category "
            f"FROM transactions WHERE substr(date,1,4)=? AND category IN ({ph}) "
            f"ORDER BY date, id",
            (yyyy, *target_cats),
        ).fetchall():
            tx_id = int(r["id"])
            if tx_id in journaled or tx_id in redundant:
                continue
            rows.append({
                **dict(r),
                "amount": int(r["debit"] or 0) - int(r["credit"] or 0),
            })

        # category 別に集計
        cat_summary: dict[str, dict] = {}
        for x in rows:
            c = x["category"] or "(空)"
            cat_summary.setdefault(c, {"count": 0, "debit": 0, "credit": 0})
            cat_summary[c]["count"] += 1
            cat_summary[c]["debit"] += int(x.get("debit") or 0)
            cat_summary[c]["credit"] += int(x.get("credit") or 0)

        return {
            "year": year, "rows": rows, "count": len(rows),
            "category_summary": [{"category": k, **v} for k, v in cat_summary.items()],
        }
    finally:
        con.close()


@app.get("/api/aoiro/etax-helper")
async def api_aoiro_etax_helper(year: int):
    """e-Tax 確定申告書等作成コーナーへの手動コピペ用 値表。
    snapshot ロック済年度はその凍結値、それ以外は live 計算値。
    """
    con = _con()
    try:
        from src.aoiro.etax_helper import build_etax_values
        return build_etax_values(con, year)
    finally:
        con.close()


@app.get("/api/aoiro/invoice-tax-summary")
async def api_aoiro_invoice_tax_summary(year: int):
    """インボイス管理 (tax_expense_items + invoice_vendors) から消費税申告用の集計。

    - インボイス対応 / 非対応 を区別
    - 経過措置 80%/50% を考慮した控除対象仕入税額
    - vendor 別内訳
    """
    con = _con()
    try:
        from src.aoiro.invoice_summary import compute_invoice_summary
        return compute_invoice_summary(con, year)
    finally:
        con.close()


@app.get("/api/aoiro/year-end-checklist")
async def api_aoiro_year_end_checklist(year: int):
    """年末/確定申告前の処理チェックリスト。各項目の状態を返す。
    青色 65 万控除には複式簿記での記帳 + 貸借平衡 + 期限内申告が必要。
    """
    con = _con()
    try:
        from src.aoiro.aggregate import compute_bs
        from src.aoiro.forms import get_declaration_inputs

        items = []

        # 1) 期首開始仕訳
        op = con.execute(
            "SELECT COUNT(*) c FROM aoiro_journal_entries "
            "WHERE fiscal_year=? AND source='opening'", (year,),
        ).fetchone()["c"]
        items.append({
            "title": "期首開始仕訳 (前期繰越)",
            "status": "ok" if op > 0 else "todo",
            "detail": f"{op} 件" if op else "なし → 「📈 自動推定」 ボタンで生成可能",
            "anchor": "#sec-bs",
        })

        # 2) 当期 auto 仕訳
        au = con.execute(
            "SELECT COUNT(*) c FROM aoiro_journal_entries "
            "WHERE fiscal_year=? AND source='auto'", (year,),
        ).fetchone()["c"]
        items.append({
            "title": "当期自動仕訳 (auto)",
            "status": "ok" if au > 0 else "todo",
            "detail": f"{au} 件" if au else "transactions が無いか rebuild 未実行",
            "anchor": "#sec-journal",
        })

        # 3) 減価償却
        dep = con.execute(
            "SELECT COUNT(*) c FROM aoiro_journal_entries "
            "WHERE fiscal_year=? AND source='depreciation'", (year,),
        ).fetchone()["c"]
        # 固定資産があるか
        fa = con.execute(
            "SELECT COUNT(*) c FROM aoiro_fixed_assets WHERE acquired_date <= ?",
            (f"{year:04d}/12/31",),
        ).fetchone()["c"]
        if fa == 0:
            dep_status = "skip"
            dep_detail = "固定資産なし"
        elif dep > 0:
            dep_status = "ok"
            dep_detail = f"償却仕訳 {dep} 件 / 資産 {fa} 件"
        else:
            dep_status = "todo"
            dep_detail = f"資産 {fa} 件あるが償却仕訳なし → 「rebuild 償却」 を実行"
        items.append({
            "title": "減価償却",
            "status": dep_status, "detail": dep_detail,
            "anchor": "#sec-assets",
        })

        # 4) 期末整理仕訳 (closing)
        cl = con.execute(
            "SELECT COUNT(*) c FROM aoiro_journal_entries "
            "WHERE fiscal_year=? AND source='closing'", (year,),
        ).fetchone()["c"]
        items.append({
            "title": "期末整理仕訳 (事業主借/貸 → 元入金)",
            "status": "ok" if cl > 0 else "todo",
            "detail": f"{cl} 件" if cl else "なし → 「📅 期末振替」 ボタンで生成",
            "anchor": "#sec-journal",
        })

        # 5) BS 貸借平衡
        bs = compute_bs(con, year)
        bc = int(bs.get("balance_check") or 0)
        items.append({
            "title": "BS 貸借平衡 (資産 = 負債 + 資本)",
            "status": "ok" if bc == 0 else "ng",
            "detail": f"差分 ¥{bc:,}" if bc else "0 ✓ 完全一致",
            "anchor": "#sec-bs",
        })

        # 6) 棚卸高 (5003)
        cogs = con.execute(
            "SELECT SUM(debit_amount) total FROM aoiro_journal_entries "
            "WHERE fiscal_year=? AND debit_account IN ('5001','5002')", (year,),
        ).fetchone()["total"]
        if cogs and cogs > 0:
            kimatsu = con.execute(
                "SELECT SUM(credit_amount) total FROM aoiro_journal_entries "
                "WHERE fiscal_year=? AND credit_account='5003'", (year,),
            ).fetchone()["total"]
            items.append({
                "title": "期末棚卸高 (仕入があるなら必須)",
                "status": "ok" if kimatsu else "todo",
                "detail": f"棚卸 ¥{int(kimatsu or 0):,} / 仕入 ¥{int(cogs):,}",
                "anchor": "#sec-journal",
            })

        # 7) declaration_inputs (給与・源泉等)
        di = get_declaration_inputs(con, year)
        di_filled = sum(1 for k in (
            "salary_income", "withholding_tax", "special_deduction_business"
        ) if di.get(k))
        items.append({
            "title": "申告書 B 入力値 (給与・源泉・特別控除)",
            "status": "ok" if di_filled >= 2 else "todo",
            "detail": f"{di_filled}/3 項目入力済",
            "anchor": "#sec-export",
        })

        # 8) snapshot 最新性
        sn = con.execute(
            "SELECT updated_at, locked_at FROM aoiro_declaration_runs WHERE fiscal_year=?",
            (year,),
        ).fetchone()
        if sn and sn["locked_at"]:
            sn_status = "ok"; sn_detail = f"🔒 locked at {sn['locked_at'][:19]}"
        elif sn:
            sn_status = "warn"; sn_detail = f"未 lock (更新 {sn['updated_at'][:19] if sn['updated_at'] else '-'})"
        else:
            sn_status = "todo"; sn_detail = "snapshot 未作成"
        items.append({
            "title": "Snapshot 保存 + lock",
            "status": sn_status, "detail": sn_detail,
            "anchor": "#sec-overview",
        })

        # 全体ステータス
        ng_count = sum(1 for x in items if x["status"] == "ng")
        todo_count = sum(1 for x in items if x["status"] == "todo")
        if ng_count > 0:
            overall = "ng"
        elif todo_count > 0:
            overall = "todo"
        else:
            overall = "ready"

        return {
            "year": year,
            "overall": overall,
            "items": items,
            "summary": {
                "ok":   sum(1 for x in items if x["status"] == "ok"),
                "todo": todo_count, "ng": ng_count,
                "skip": sum(1 for x in items if x["status"] == "skip"),
                "warn": sum(1 for x in items if x["status"] == "warn"),
                "total": len(items),
            },
        }
    finally:
        con.close()


@app.get("/api/aoiro/health-check/monthly")
async def api_aoiro_health_check_monthly(year: int):
    """月別ヘルスチェック: 月末ごとの BS 検算と 2010 借方残。"""
    con = _con()
    try:
        from src.aoiro.aggregate import compute_monthly_health_check
        return compute_monthly_health_check(con, year)
    finally:
        con.close()


@app.get("/api/aoiro/health-check")
async def api_aoiro_health_check():
    """全年度の複式簿記ヘルスチェック指標を返す。

    各年度について:
      - balance_check: 資産 - (負債+純資産) — 0 なら ✓
      - liability_2010_negative: 2010 未払金が借方残になっていれば異常 (正数で返す)
      - opening_journal_count: source='opening' 仕訳の件数
      - bank_balance_diff: 1002 普通預金 期末と MUFG 実残高最終値の差
      - locked: スナップショット lock 状態
      - has_journal_entries: aoiro_journal_entries にデータがあるか
    """
    con = _con()
    try:
        from src.aoiro.aggregate import (
            compute_bs,
            compute_monthly_bank_balance,
        )
        from src.aoiro.opening_entries import has_opening_entries

        # 全年度を集める (snapshot がある年度 + journal がある年度の和集合)
        years: set[int] = set()
        for r in con.execute(
            "SELECT DISTINCT fiscal_year FROM aoiro_declaration_runs"
        ).fetchall():
            years.add(int(r["fiscal_year"]))
        for r in con.execute(
            "SELECT DISTINCT fiscal_year FROM aoiro_journal_entries"
        ).fetchall():
            years.add(int(r["fiscal_year"]))

        rows = []
        for year in sorted(years, reverse=True):
            bs = compute_bs(con, year)
            balance_check = int(bs.get("balance_check") or 0)
            # 2010 借方残検出 (asset_lines に出ていれば借方残異常)
            liab_2010_neg = 0
            for x in bs.get("asset_lines", []):
                if x["code"] == "2010":
                    liab_2010_neg = int(x.get("ending") or 0)
                    break
            # 1002 期末 (book) vs 実 MUFG 最終 balance
            book_1002 = 0
            for x in bs.get("asset_lines", []):
                if x["code"] == "1002":
                    book_1002 = int(x.get("ending") or 0)
                    break
            mb = compute_monthly_bank_balance(con, year, "MUFG")
            actual_last = next((m["balance"] for m in reversed(mb)
                                if m["balance"] is not None), None)
            bank_diff = book_1002 - actual_last if actual_last is not None else None
            # journal entries 件数
            j_count = con.execute(
                "SELECT COUNT(*) c FROM aoiro_journal_entries WHERE fiscal_year=?",
                (year,),
            ).fetchone()["c"]
            # opening
            op_count = con.execute(
                "SELECT COUNT(*) c FROM aoiro_journal_entries "
                "WHERE fiscal_year=? AND source='opening'", (year,),
            ).fetchone()["c"]
            # lock
            r = con.execute(
                "SELECT locked_at FROM aoiro_declaration_runs WHERE fiscal_year=?",
                (year,),
            ).fetchone()
            locked = bool(r and r["locked_at"])

            # legacy 年度 (journal 空 + snapshot あり) は閲覧専用として扱い警告対象外
            is_legacy = (j_count == 0 and locked)
            issues = []
            if not is_legacy:
                if balance_check != 0:
                    issues.append(f"balance_check={balance_check:+,}")
                if liab_2010_neg < 0:
                    issues.append(f"2010 借方残 ¥{-liab_2010_neg:,}")
                if op_count == 0 and j_count > 0 and year > min(years):
                    issues.append("opening 仕訳なし")
                if bank_diff is not None and abs(bank_diff) > 100000:
                    issues.append(f"MUFG 誤差 ¥{bank_diff:+,}")
            status = "legacy" if is_legacy else (
                "ok" if not issues else ("warn" if len(issues) <= 1 else "ng")
            )

            rows.append({
                "year": year,
                "balance_check": balance_check,
                "liability_2010_negative": -liab_2010_neg if liab_2010_neg < 0 else 0,
                "book_1002": book_1002,
                "actual_mufg_last": actual_last,
                "bank_diff": bank_diff,
                "journal_count": j_count,
                "opening_count": op_count,
                "locked": locked,
                "status": status,
                "issues": issues,
            })
        return {"rows": rows}
    finally:
        con.close()


@app.get("/api/aoiro/pl/monthly")
async def api_aoiro_pl_monthly(year: int):
    """月次 PL: 科目別 12 ヶ月推移。"""
    con = _con()
    try:
        from src.aoiro.aggregate import compute_monthly_pl
        return compute_monthly_pl(con, year)
    finally:
        con.close()


@app.get("/api/aoiro/pl/yoy")
async def api_aoiro_pl_yoy(year: int):
    """PL 前年比較: 当期 vs 前期の科目別 差分。"""
    con = _con()
    try:
        from src.aoiro.aggregate import compute_pl
        cur = compute_pl(con, year)
        prev = compute_pl(con, year - 1)
        prev_rev = {x["code"]: x for x in prev.get("revenue_lines") or []}
        prev_exp = {x["code"]: x for x in prev.get("expense_lines") or []}
        out_rev = []
        for x in cur.get("revenue_lines") or []:
            p = prev_rev.get(x["code"], {})
            pa = int(p.get("amount") or 0)
            out_rev.append({
                **x, "prev_amount": pa, "diff": int(x["amount"]) - pa,
                "ratio": ((int(x["amount"]) / pa - 1) if pa else None),
            })
        out_exp = []
        for x in cur.get("expense_lines") or []:
            p = prev_exp.get(x["code"], {})
            pa = int(p.get("amount") or 0)
            out_exp.append({
                **x, "prev_amount": pa, "diff": int(x["amount"]) - pa,
                "ratio": ((int(x["amount"]) / pa - 1) if pa else None),
            })
        return {
            "year": year, "prev_year": year - 1,
            "revenue_lines": out_rev, "expense_lines": out_exp,
            "revenue_total": int(cur.get("revenue_total") or 0),
            "prev_revenue_total": int(prev.get("revenue_total") or 0),
            "expense_total": int(cur.get("expense_total") or 0),
            "prev_expense_total": int(prev.get("expense_total") or 0),
            "income_before_special": int(cur.get("income_before_special") or 0),
            "prev_income_before_special": int(prev.get("income_before_special") or 0),
        }
    finally:
        con.close()


@app.get("/api/aoiro/trial-balance")
async def api_aoiro_trial_balance(year: int):
    """合計残高試算表 (期首 / 当期借貸 / 期末) を返す。"""
    con = _con()
    try:
        from src.aoiro.aggregate import compute_trial_balance
        return compute_trial_balance(con, year)
    finally:
        con.close()


@app.get("/api/aoiro/card-monthly")
async def api_aoiro_card_monthly(year: int):
    """カード別 月次未払金推移 (発生 / 引落 / 残債)。"""
    con = _con()
    try:
        from src.aoiro.aggregate import compute_card_monthly
        return compute_card_monthly(con, year)
    finally:
        con.close()


@app.get("/api/aoiro/journal/templates")
async def api_aoiro_journal_templates_list():
    """仕訳テンプレート 一覧。"""
    con = _con()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM aoiro_journal_templates ORDER BY id DESC"
        ).fetchall()]
        return {"templates": rows}
    finally:
        con.close()


class TemplateBody(BaseModel):
    name: str
    debit_account: str
    credit_account: str
    default_amount: int = 0
    default_description: str = ""
    note: str = ""


@app.post("/api/aoiro/journal/templates")
async def api_aoiro_journal_template_save(body: TemplateBody):
    """仕訳テンプレート 追加。"""
    con = _con()
    try:
        cur = con.execute(
            "INSERT INTO aoiro_journal_templates "
            "(name, debit_account, credit_account, default_amount, "
            " default_description, note) VALUES (?,?,?,?,?,?)",
            (body.name, body.debit_account, body.credit_account,
             body.default_amount, body.default_description, body.note),
        )
        con.commit()
        return {"id": cur.lastrowid}
    finally:
        con.close()


@app.delete("/api/aoiro/journal/templates/{tpl_id}")
async def api_aoiro_journal_template_delete(tpl_id: int):
    con = _con()
    try:
        con.execute("DELETE FROM aoiro_journal_templates WHERE id=?", (tpl_id,))
        con.commit()
        return {"status": "ok"}
    finally:
        con.close()


@app.get("/api/aoiro/journal/{entry_id}/source-tx")
async def api_aoiro_journal_source_tx(entry_id: int):
    """仕訳エントリの source_tx_id から元の transaction 詳細を返す。"""
    con = _con()
    try:
        je = con.execute(
            "SELECT source_tx_id FROM aoiro_journal_entries WHERE id=?", (entry_id,)
        ).fetchone()
        if je is None:
            raise HTTPException(status_code=404, detail="journal entry not found")
        tx_id = je["source_tx_id"]
        if tx_id is None:
            return {"entry_id": entry_id, "tx": None,
                    "reason": "この仕訳には source_tx_id がありません (手動仕訳/開始仕訳/closing 仕訳など)"}
        tx = con.execute("""
            SELECT id, bank, date, description, debit, credit, balance,
                   category, description_normalized
            FROM transactions WHERE id=?
        """, (tx_id,)).fetchone()
        if tx is None:
            return {"entry_id": entry_id, "tx": None,
                    "reason": f"tx {tx_id} は transactions に見つかりません (削除された可能性)"}
        # tx_links も引いてくる (そのtxが何と紐付いているか)
        links = []
        for r in con.execute("""
            SELECT link_type, tx_a_id, tx_b_id, extra_json
            FROM tx_links WHERE tx_a_id=? OR tx_b_id=?
        """, (tx_id, tx_id)).fetchall():
            links.append(dict(r))
        return {"entry_id": entry_id, "tx": dict(tx), "links": links}
    finally:
        con.close()


@app.get("/api/aoiro/general-ledger")
async def api_aoiro_general_ledger(year: int, account: str):
    """元帳: 指定 account の取引一覧 + 累積残高。"""
    con = _con()
    try:
        from src.aoiro.aggregate import compute_general_ledger
        return compute_general_ledger(con, year, account)
    finally:
        con.close()


@app.get("/api/aoiro/bs/monthly")
async def api_aoiro_bs_monthly(year: int):
    """月次 BS: 各 BS 科目の月末残高 (12 ヶ月)。"""
    con = _con()
    try:
        from src.aoiro.aggregate import compute_monthly_bs
        return compute_monthly_bs(con, year)
    finally:
        con.close()


@app.get("/api/aoiro/bs/monthly-bank-comparison")
async def api_aoiro_monthly_bank_comparison(year: int, bank: str = "MUFG"):
    """月次の帳簿 1002 普通預金 期末 vs 実際の bank balance 月末 を返す。
    誤差が広がる月の特定に使う。
    """
    con = _con()
    try:
        from src.aoiro.aggregate import (
            compute_monthly_account_balance,
            compute_monthly_bank_balance,
        )
        book = compute_monthly_account_balance(con, year, "1002")
        actual = compute_monthly_bank_balance(con, year, bank)
        rows = []
        for b, a in zip(book, actual):
            diff = (b["ending"] - a["balance"]) if a["balance"] is not None else None
            rows.append({
                "month": b["month"],
                "book_ending": b["ending"],
                "actual_balance": a["balance"],
                "diff": diff,
            })
        return {"year": year, "bank": bank, "rows": rows}
    finally:
        con.close()


@app.post("/api/aoiro/opening_balances/{year}/auto-estimate")
async def api_aoiro_opening_balances_auto(year: int):
    """transactions の balance と rebuild 後の差額から期首残高を自動推定し、
    aoiro_opening_balances に upsert + opening 仕訳を再生成。

    1002 普通預金: 当年最初の MUFG 取引の balance を逆算
    2010 未払金: rebuild 後の 2010 借方残 (= 期首未払金 不足分)
    """
    con = _con()
    try:
        from src.aoiro.opening_entries import estimate_opening_balances
        return estimate_opening_balances(con, year)
    finally:
        con.close()


# ─────────────────────────────────────────────
# 期首残高
# ─────────────────────────────────────────────
class OpeningBody(BaseModel):
    fiscal_year: int
    account: str
    debit_balance: int = 0
    credit_balance: int = 0


@app.get("/api/aoiro/opening")
async def api_aoiro_opening(year: int):
    con = _con()
    try:
        return {"year": year, "balances": list_opening_balances(con, year)}
    finally:
        con.close()


@app.post("/api/aoiro/opening")
async def api_aoiro_opening_upsert(body: OpeningBody):
    con = _con()
    try:
        upsert_opening_balance(
            con, fiscal_year=body.fiscal_year, account=body.account.strip(),
            debit_balance=body.debit_balance, credit_balance=body.credit_balance,
        )
        return {"status": "ok"}
    finally:
        con.close()


@app.delete("/api/aoiro/opening")
async def api_aoiro_opening_delete(year: int, account: str):
    con = _con()
    try:
        delete_opening_balance(con, year, account)
        return {"status": "ok"}
    finally:
        con.close()


# ─────────────────────────────────────────────
# 集計 (PL / BS)
# ─────────────────────────────────────────────
@app.get("/api/aoiro/pl")
async def api_aoiro_pl(year: int):
    con = _con()
    try:
        return compute_pl(con, year)
    finally:
        con.close()


@app.get("/api/aoiro/bs")
async def api_aoiro_bs(year: int):
    con = _con()
    try:
        return compute_bs(con, year)
    finally:
        con.close()


# ─────────────────────────────────────────────
# 固定資産台帳 + 減価償却
# ─────────────────────────────────────────────
class AssetBody(BaseModel):
    id: int | None = None
    name: str = Field(..., min_length=1, max_length=128)
    acquired_date: str
    acquired_cost: int
    useful_years: int = 0
    method: str = "定額"
    account: str
    business_ratio: int = 100
    salvage_value: int = 0
    retired_date: str = ""
    note: str = ""


@app.get("/api/aoiro/assets")
async def api_aoiro_assets(year: int = 0):
    con = _con()
    try:
        if year:
            return {"assets": list_schedules_for_year(con, year), "year": year}
        return {"assets": list_assets(con)}
    finally:
        con.close()


@app.post("/api/aoiro/assets")
async def api_aoiro_asset_upsert(body: AssetBody):
    con = _con()
    try:
        try:
            aid = upsert_asset(
                con, id_=body.id, name=body.name.strip(),
                acquired_date=body.acquired_date.strip(),
                acquired_cost=body.acquired_cost, useful_years=body.useful_years,
                method=body.method, account=body.account.strip(),
                business_ratio=body.business_ratio, salvage_value=body.salvage_value,
                retired_date=body.retired_date.strip(), note=body.note.strip(),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "ok", "id": aid}
    finally:
        con.close()


@app.delete("/api/aoiro/assets/{asset_id}")
async def api_aoiro_asset_delete(asset_id: int):
    con = _con()
    try:
        if not delete_asset(con, asset_id):
            raise HTTPException(status_code=404, detail="not found")
        return {"status": "ok"}
    finally:
        con.close()


@app.get("/api/aoiro/assets/{asset_id}/schedule")
async def api_aoiro_asset_schedule(asset_id: int):
    con = _con()
    try:
        for a in list_assets(con):
            if a["id"] == asset_id:
                return {"asset": a, "schedule": asset_schedule(a)}
        raise HTTPException(status_code=404, detail="not found")
    finally:
        con.close()


@app.post("/api/aoiro/depreciation/rebuild")
async def api_aoiro_depreciation_rebuild(year: int):
    con = _con()
    try:
        return rebuild_depreciation(con, year)
    finally:
        con.close()


# ─────────────────────────────────────────────
# 家事按分
# ─────────────────────────────────────────────
class BusinessRatioBody(BaseModel):
    fiscal_year: int
    scope: str = Field(..., min_length=3, max_length=64)
    ratio_pct: int
    note: str = ""


@app.get("/api/aoiro/business-ratios")
async def api_aoiro_business_ratios(year: int | None = None):
    con = _con()
    try:
        return {"ratios": list_business_ratios(con, fiscal_year=year)}
    finally:
        con.close()


@app.post("/api/aoiro/business-ratios")
async def api_aoiro_business_ratio_upsert(body: BusinessRatioBody):
    con = _con()
    try:
        try:
            upsert_business_ratio(con, fiscal_year=body.fiscal_year,
                                   scope=body.scope.strip(),
                                   ratio_pct=body.ratio_pct,
                                   note=body.note.strip())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "ok"}
    finally:
        con.close()


@app.post("/api/aoiro/business-ratios/seed-template")
async def api_aoiro_business_ratio_seed_template(year: int):
    """雛形を指定年度に投入。既存 (year, scope) はスキップ。"""
    con = _con()
    try:
        return seed_business_ratio_template(con, fiscal_year=year)
    finally:
        con.close()


@app.post("/api/aoiro/business-ratios/copy")
async def api_aoiro_business_ratio_copy(source_year: int, target_year: int):
    """source_year の按分を target_year に複製 (前年から複製)。"""
    con = _con()
    try:
        from src.aoiro.business_ratios import copy_to_year
        n = copy_to_year(con, source_year, target_year)
        return {"copied": n, "source_year": source_year, "target_year": target_year}
    finally:
        con.close()


@app.delete("/api/aoiro/business-ratios")
async def api_aoiro_business_ratio_delete(year: int, scope: str):
    con = _con()
    try:
        delete_business_ratio(con, year, scope)
        return {"status": "ok"}
    finally:
        con.close()


# ─────────────────────────────────────────────
# 不動産所得 (物件 + 月次収支)
# ─────────────────────────────────────────────
class PropertyBody(BaseModel):
    id: int | None = None
    name: str = Field(..., min_length=1, max_length=128)
    address: str = ""
    acquired_date: str = ""
    acquired_cost: int = 0
    is_active: bool = True
    match_keywords: str = ""
    note: str = ""


class RentBody(BaseModel):
    property_id: int
    month: str = Field(..., pattern=r"^\d{4}-\d{2}$")
    gross_rent: int = 0
    vacancy: int = 0
    expenses: int = 0
    note: str = ""


@app.get("/api/aoiro/properties")
async def api_aoiro_properties():
    con = _con()
    try:
        return {"properties": list_properties(con)}
    finally:
        con.close()


@app.post("/api/aoiro/properties")
async def api_aoiro_property_upsert(body: PropertyBody):
    con = _con()
    try:
        pid = upsert_property(
            con, id_=body.id, name=body.name.strip(),
            address=body.address.strip(),
            acquired_date=body.acquired_date.strip(),
            acquired_cost=body.acquired_cost,
            is_active=body.is_active,
            match_keywords=body.match_keywords.strip(),
            note=body.note.strip(),
        )
        return {"status": "ok", "id": pid}
    finally:
        con.close()


@app.delete("/api/aoiro/properties/{property_id}")
async def api_aoiro_property_delete(property_id: int):
    con = _con()
    try:
        if not delete_property(con, property_id):
            raise HTTPException(status_code=404, detail="not found")
        return {"status": "ok"}
    finally:
        con.close()


@app.get("/api/aoiro/rents")
async def api_aoiro_rents(property_id: int = 0, year: int = 0):
    con = _con()
    try:
        return {"rents": list_rents(con, property_id=property_id or None,
                                    year=year or None)}
    finally:
        con.close()


@app.post("/api/aoiro/rents")
async def api_aoiro_rent_upsert(body: RentBody):
    con = _con()
    try:
        upsert_rent(
            con, property_id=body.property_id, month=body.month,
            gross_rent=body.gross_rent, vacancy=body.vacancy,
            expenses=body.expenses, note=body.note.strip(),
        )
        return {"status": "ok"}
    finally:
        con.close()


@app.delete("/api/aoiro/rents")
async def api_aoiro_rent_delete(property_id: int, month: str):
    con = _con()
    try:
        delete_rent(con, property_id, month)
        return {"status": "ok"}
    finally:
        con.close()


@app.get("/api/aoiro/real-estate/summary")
async def api_aoiro_real_estate_summary(year: int):
    con = _con()
    try:
        return yearly_summary(con, year)
    finally:
        con.close()


@app.post("/api/aoiro/real-estate/reflect")
async def api_aoiro_real_estate_reflect(year: int, dry_run: bool = True,
                                        overwrite: bool = False):
    """transactions の家賃収入を物件マスタの match_keywords で
    マッチさせて月次収支に反映。dry_run=True で preview のみ。"""
    con = _con()
    try:
        return reflect_from_transactions(con, year, dry_run=dry_run,
                                         overwrite=overwrite)
    finally:
        con.close()


@app.get("/api/aoiro/real-estate/landlord-tx")
async def api_aoiro_landlord_tx(year: int):
    con = _con()
    try:
        return {"transactions": list_landlord_transactions(con, year)}
    finally:
        con.close()


# ─────────────────────────────────────────────
# 所得控除
# ─────────────────────────────────────────────
class DeductionBody(BaseModel):
    id: int | None = None
    fiscal_year: int
    kind: str = Field(..., min_length=1, max_length=32)
    payee: str = ""
    amount: int = 0
    evidence_path: str = ""
    note: str = ""


@app.get("/api/aoiro/deductions")
async def api_aoiro_deductions(year: int = 0, kind: str = ""):
    con = _con()
    try:
        return {
            "deductions": list_deductions(con, year=year or None, kind=kind),
            "kinds": DEDUCTION_KINDS,
        }
    finally:
        con.close()


@app.post("/api/aoiro/deductions")
async def api_aoiro_deduction_upsert(body: DeductionBody):
    con = _con()
    try:
        did = upsert_deduction(
            con, id_=body.id, fiscal_year=body.fiscal_year,
            kind=body.kind.strip(), payee=body.payee.strip(),
            amount=body.amount, evidence_path=body.evidence_path.strip(),
            note=body.note.strip(),
        )
        return {"status": "ok", "id": did}
    finally:
        con.close()


@app.delete("/api/aoiro/deductions/{deduction_id}")
async def api_aoiro_deduction_delete(deduction_id: int):
    con = _con()
    try:
        if not delete_deduction(con, deduction_id):
            raise HTTPException(status_code=404, detail="not found")
        return {"status": "ok"}
    finally:
        con.close()


@app.get("/api/aoiro/deductions/summary")
async def api_aoiro_deductions_summary(year: int):
    con = _con()
    try:
        return deduction_summary(con, year)
    finally:
        con.close()


@app.get("/api/aoiro/deductions/medical-tx")
async def api_aoiro_medical_tx(year: int):
    con = _con()
    try:
        return {"transactions": list_medical_transactions(con, year)}
    finally:
        con.close()


@app.get("/api/aoiro/deductions/insurance-tx")
async def api_aoiro_insurance_tx(year: int):
    con = _con()
    try:
        return {"transactions": list_insurance_transactions(con, year)}
    finally:
        con.close()


class BulkDeductionBody(BaseModel):
    fiscal_year: int
    kind: str
    payee: str = ""
    tx_ids: list[int]
    note: str = ""


@app.post("/api/aoiro/deductions/bulk-from-tx")
async def api_aoiro_deduction_bulk_from_tx(body: BulkDeductionBody):
    con = _con()
    try:
        return bulk_add_from_transactions(
            con, fiscal_year=body.fiscal_year, kind=body.kind.strip(),
            payee=body.payee.strip(), tx_ids=body.tx_ids,
            note=body.note.strip(),
        )
    finally:
        con.close()


# ─────────────────────────────────────────────
# 申告書 B 第一表 集計 + snapshot
# ─────────────────────────────────────────────
class DeclarationParams(BaseModel):
    year: int
    salary_income: int = 0
    other_income: int = 0
    special_deduction_business: int = SPECIAL_DEDUCTION_BLUE_E_TAX
    special_deduction_real_estate: int = 0
    withholding_tax: int = 0
    estimated_tax: int = 0
    tax_credits: int = 0



@app.get("/api/aoiro/declaration/inputs")
async def api_aoiro_declaration_inputs_get(year: int):
    """指定年度の申告書 B 入力値 (給与・源泉・特別控除等) を取得。"""
    con = _con()
    try:
        return get_declaration_inputs(con, year)
    finally:
        con.close()


class DeclarationInputsBody(BaseModel):
    fiscal_year: int
    salary_income: int = 0
    other_income: int = 0
    special_deduction_business: int = SPECIAL_DEDUCTION_BLUE_E_TAX
    special_deduction_real_estate: int = 0
    withholding_tax: int = 0
    estimated_tax: int = 0
    tax_credits: int = 0
    business_raw_override: int | None = None


@app.post("/api/aoiro/declaration/inputs")
async def api_aoiro_declaration_inputs_save(body: DeclarationInputsBody):
    con = _con()
    try:
        save_declaration_inputs(con, body.fiscal_year, body.dict())
        return {"status": "ok"}
    finally:
        con.close()


@app.get("/api/aoiro/declaration/options")
async def api_aoiro_declaration_options():
    return {
        "special_deduction_options": [
            {"label": label, "value": value}
            for label, value in SPECIAL_DEDUCTION_OPTIONS
        ],
    }


@app.get("/api/aoiro/declaration/salary-from-tx")
async def api_aoiro_salary_from_tx(year: int):
    """transactions の category='給与' を集計して 給与収入 + 給与所得を返す。"""
    con = _con()
    try:
        return salary_revenue_from_transactions(con, year)
    finally:
        con.close()


@app.post("/api/aoiro/declaration/compute")
async def api_aoiro_declaration_compute(body: DeclarationParams):
    con = _con()
    try:
        return compute_declaration(
            con, body.year,
            salary_income=body.salary_income, other_income=body.other_income,
            special_deduction_business=body.special_deduction_business,
            special_deduction_real_estate=body.special_deduction_real_estate,
            withholding_tax=body.withholding_tax,
            estimated_tax=body.estimated_tax,
            tax_credits=body.tax_credits,
        )
    finally:
        con.close()


@app.get("/api/aoiro/declaration/snapshot")
async def api_aoiro_declaration_snapshot(year: int):
    con = _con()
    try:
        s = get_snapshot(con, year)
        if s is None:
            raise HTTPException(status_code=404, detail="no snapshot")
        return s
    finally:
        con.close()


class SnapshotBody(BaseModel):
    declaration: dict
    exports: dict | None = None
    lock: bool = False


@app.post("/api/aoiro/declaration/snapshot/{year}")
async def api_aoiro_declaration_snapshot_save(year: int, body: SnapshotBody):
    con = _con()
    try:
        try:
            return save_snapshot(con, year, declaration=body.declaration,
                                 exports=body.exports, lock=body.lock)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
    finally:
        con.close()


@app.post("/api/aoiro/declaration/snapshot/{year}/unlock")
async def api_aoiro_declaration_snapshot_unlock(year: int):
    con = _con()
    try:
        unlock_snapshot(con, year)
        return {"status": "ok"}
    finally:
        con.close()


@app.delete("/api/aoiro/declaration/snapshot/{year}")
async def api_aoiro_declaration_snapshot_delete(year: int):
    con = _con()
    try:
        try:
            delete_snapshot(con, year)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"status": "ok"}
    finally:
        con.close()


# ─────────────────────────────────────────────
# 年度別 全 section snapshot (履歴閲覧用)
# ─────────────────────────────────────────────
class FullSnapshotBody(BaseModel):
    declaration_params: dict | None = None
    lock: bool = False


@app.post("/api/aoiro/snapshot/save")
async def api_aoiro_snapshot_save(year: int, body: FullSnapshotBody | None = None):
    con = _con()
    try:
        try:
            return save_snapshot_full(
                con, year,
                declaration_params=(body.declaration_params if body else None),
                lock=(body.lock if body else False),
            )
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
    finally:
        con.close()


@app.get("/api/aoiro/snapshots")
async def api_aoiro_snapshots():
    con = _con()
    try:
        return {"snapshots": list_snapshots(con)}
    finally:
        con.close()


@app.get("/api/aoiro/snapshot/{year}")
async def api_aoiro_snapshot_get(year: int):
    con = _con()
    try:
        s = get_snapshot_full(con, year)
        if s is None or not s.get("snapshot"):
            raise HTTPException(status_code=404, detail="snapshot not found")
        return s
    finally:
        con.close()


@app.get("/api/aoiro/snapshot/{year}/section/{kind}")
async def api_aoiro_snapshot_section(year: int, kind: str):
    con = _con()
    try:
        s = snapshot_get_section(con, year, kind)
        if s is None:
            raise HTTPException(status_code=404, detail=f"section {kind} not found in snapshot {year}")
        return s
    finally:
        con.close()


@app.post("/api/aoiro/snapshot/{year}/lock")
async def api_aoiro_snapshot_lock(year: int):
    con = _con()
    try:
        if not lock_snapshot(con, year):
            raise HTTPException(status_code=404, detail="not found")
        return {"status": "ok"}
    finally:
        con.close()


@app.post("/api/aoiro/snapshot/{year}/unlock")
async def api_aoiro_snapshot_unlock_full(year: int):
    con = _con()
    try:
        unlock_snapshot_full(con, year)
        return {"status": "ok"}
    finally:
        con.close()


@app.delete("/api/aoiro/snapshot/{year}")
async def api_aoiro_snapshot_delete(year: int):
    con = _con()
    try:
        try:
            delete_snapshot_full(con, year)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"status": "ok"}
    finally:
        con.close()


@app.post("/api/aoiro/snapshot/{year}/reapply-engine")
async def api_aoiro_snapshot_reapply_engine(year: int):
    """新しい仕訳エンジン (個人支出含む + 期首開始仕訳付き) で当該年度を再ビルド。

    ステップ:
      1. snapshot がロック中ならアンロック
      2. rebuild_year (auto 仕訳 + opening 仕訳)
      3. rebuild_closing (事業主借/貸 → 元入金 振替)
      4. save_snapshot_full で再保存
      5. もとがロックされていたなら再ロック

    過去年度の主要数値 (経費合計・売上・還付額) は変わらず、BS の
    普通預金 / 未払金 の構成だけが正しい姿に再構築される。
    """
    con = _con()
    try:
        cur = con.execute(
            "SELECT locked_at FROM aoiro_declaration_runs WHERE fiscal_year=?", (year,)
        )
        row = cur.fetchone()
        was_locked = bool(row and row["locked_at"])
        if was_locked:
            unlock_snapshot_full(con, year)
        rebuild_result = rebuild_year(con, year=year)
        closing_result = rebuild_closing(con, year)
        save_result = save_snapshot_full(con, year, lock=was_locked)
        return {
            "status": "ok",
            "was_locked": was_locked,
            "rebuild": rebuild_result,
            "closing": closing_result,
            "snapshot_updated_at": save_result.get("updated_at"),
        }
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    finally:
        con.close()


@app.get("/api/aoiro/snapshots/legacy-years")
async def api_aoiro_snapshot_legacy_years():
    """tax_journal/tax_pl/tax_bs にデータがある年度。"""
    con = _con()
    try:
        return {"years": list_legacy_years(con)}
    finally:
        con.close()


@app.get("/api/aoiro/snapshots/legacy-preview")
async def api_aoiro_snapshot_legacy_preview(year: int):
    con = _con()
    try:
        return preview_legacy_year(con, year)
    finally:
        con.close()


@app.post("/api/aoiro/snapshots/legacy-import")
async def api_aoiro_snapshot_legacy_import(year: int):
    con = _con()
    try:
        try:
            return import_legacy_year(con, year)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
    finally:
        con.close()


@app.get("/api/aoiro/snapshots/dir-years")
async def api_aoiro_snapshot_dir_years():
    """過去の納税/<year>/ ディレクトリの一覧。"""
    return {"years": list_dir_years()}


@app.get("/api/aoiro/documents")
async def api_aoiro_documents(year: int):
    """過去の納税/<year>/ にあるファイル一覧 (live モード用)。
    snapshot を作らずに PDF/data ファイルを閲覧するため。
    """
    from src.aoiro.import_legacy import TAX_DIR, classify_document, collect_documents
    from pathlib import Path as _P
    base = TAX_DIR / str(year)
    if not base.exists():
        return {"year": year, "documents": []}
    return {"year": year, "documents": collect_documents(base)}


@app.get("/api/aoiro/document")
async def api_aoiro_document(year: int, filename: str):
    """過去の納税/<year>/<filename> を配信。
    パストラバーサル防止: basename のみ許可、ディレクトリ外の参照は 404。
    """
    from src.aoiro.import_legacy import TAX_DIR
    from fastapi.responses import FileResponse
    from pathlib import Path as _Path
    # basename のみ許可
    if "/" in filename or "\\" in filename or filename.startswith(".."):
        raise HTTPException(status_code=400, detail="invalid filename")
    base = TAX_DIR / str(year)
    target = (base / filename).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes base dir")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    media = "application/pdf" if filename.lower().endswith(".pdf") else "application/octet-stream"
    # filename の RFC 5987 エンコード (日本語ファイル名対応)
    from urllib.parse import quote as _q
    return FileResponse(
        str(target), media_type=media,
        headers={
            "content-disposition": f"inline; filename*=UTF-8''{_q(filename)}",
        },
    )


@app.post("/api/aoiro/snapshots/import-from-dir")
async def api_aoiro_snapshot_import_from_dir(year: int):
    """過去の納税/<year>/ → CSV 取込 → tax_* → snapshot を一気通貫で実行。"""
    con = _con()
    try:
        try:
            return import_from_dir(con, year)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
    finally:
        con.close()


@app.get("/api/aoiro/snapshots/diff")
async def api_aoiro_snapshot_diff(a: int, b: int):
    con = _con()
    try:
        try:
            return compare_snapshots(con, a, b)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
    finally:
        con.close()


# ─────────────────────────────────────────────
# CSV エクスポート
# ─────────────────────────────────────────────
def _csv_response(content: str, filename: str):
    return PlainTextResponse(
        content,
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/aoiro/export/journal.csv")
async def api_aoiro_export_journal(year: int, exclude_personal: bool = False):
    con = _con()
    try:
        suffix = "_business_only" if exclude_personal else ""
        return _csv_response(
            csv_journal(con, year, exclude_personal=exclude_personal),
            f"aoiro_journal_{year}{suffix}.csv",
        )
    finally:
        con.close()


@app.get("/api/aoiro/export/pl.csv")
async def api_aoiro_export_pl(year: int):
    con = _con()
    try:
        return _csv_response(csv_pl(con, year), f"aoiro_pl_{year}.csv")
    finally:
        con.close()


@app.get("/api/aoiro/export/bs.csv")
async def api_aoiro_export_bs(year: int):
    con = _con()
    try:
        return _csv_response(csv_bs(con, year), f"aoiro_bs_{year}.csv")
    finally:
        con.close()


@app.get("/api/aoiro/export/deductions.csv")
async def api_aoiro_export_deductions(year: int):
    con = _con()
    try:
        return _csv_response(csv_deductions(con, year), f"aoiro_deductions_{year}.csv")
    finally:
        con.close()


@app.get("/api/aoiro/export/real-estate.csv")
async def api_aoiro_export_real_estate(year: int):
    con = _con()
    try:
        return _csv_response(csv_real_estate(con, year), f"aoiro_real_estate_{year}.csv")
    finally:
        con.close()


@app.get("/api/aoiro/export/fixed-assets.csv")
async def api_aoiro_export_fixed_assets(year: int):
    con = _con()
    try:
        return _csv_response(csv_fixed_assets(con, year), f"aoiro_fixed_assets_{year}.csv")
    finally:
        con.close()


@app.get("/api/aoiro/export/trial-balance.csv")
async def api_aoiro_export_trial_balance(year: int):
    con = _con()
    try:
        return _csv_response(csv_trial_balance(con, year), f"aoiro_trial_balance_{year}.csv")
    finally:
        con.close()


@app.get("/api/aoiro/export/general-ledger.csv")
async def api_aoiro_export_general_ledger(year: int, account: str):
    con = _con()
    try:
        return _csv_response(csv_general_ledger(con, year, account),
                             f"aoiro_ledger_{year}_{account}.csv")
    finally:
        con.close()


@app.get("/api/aoiro/export/pl-monthly.csv")
async def api_aoiro_export_pl_monthly(year: int):
    con = _con()
    try:
        return _csv_response(csv_monthly_pl(con, year), f"aoiro_pl_monthly_{year}.csv")
    finally:
        con.close()


@app.get("/api/aoiro/export/pl-yoy.csv")
async def api_aoiro_export_pl_yoy(year: int):
    con = _con()
    try:
        return _csv_response(csv_pl_yoy(con, year), f"aoiro_pl_yoy_{year}_vs_{year-1}.csv")
    finally:
        con.close()


@app.get("/api/aoiro/export/yayoi.csv")
async def api_aoiro_export_yayoi(year: int, exclude_personal: bool = False):
    """弥生会計「一般仕訳」 互換 CSV (24列)."""
    con = _con()
    try:
        suffix = "_business_only" if exclude_personal else ""
        return _csv_response(
            csv_yayoi_journal(con, year, exclude_personal=exclude_personal),
            f"yayoi_journal_{year}{suffix}.csv",
        )
    finally:
        con.close()


@app.get("/api/aoiro/export/mfcloud.csv")
async def api_aoiro_export_mfcloud(year: int, exclude_personal: bool = False):
    """MF クラウド会計「仕訳帳取込」 互換 CSV."""
    con = _con()
    try:
        suffix = "_business_only" if exclude_personal else ""
        return _csv_response(
            csv_mf_journal(con, year, exclude_personal=exclude_personal),
            f"mfcloud_journal_{year}{suffix}.csv",
        )
    finally:
        con.close()


@app.get("/api/aoiro/export/rental-contracts.csv")
async def api_aoiro_export_rental_contracts(year: int | None = None):
    con = _con()
    try:
        suffix = f"_{year}" if year else ""
        return _csv_response(csv_rental_contracts(con, fiscal_year=year),
                              f"aoiro_rental_contracts{suffix}.csv")
    finally:
        con.close()


@app.get("/api/aoiro/export/card-monthly.csv")
async def api_aoiro_export_card_monthly(year: int):
    con = _con()
    try:
        return _csv_response(csv_card_monthly(con, year), f"aoiro_card_monthly_{year}.csv")
    finally:
        con.close()


# ─────────────────────────────────────────────
# サマリ (ホームに置く軽い概観)
# ─────────────────────────────────────────────
@app.get("/api/aoiro/summary")
async def api_aoiro_summary():
    """各 Phase の進捗 (テーブル件数) を返す。"""
    con = _con()
    try:
        def _count(t: str) -> int:
            return con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return {
            "accounts":    _count("aoiro_accounts"),
            "rules":       _count("aoiro_journal_rules"),
            "entries":     _count("aoiro_journal_entries"),
            "fixed_assets": _count("aoiro_fixed_assets"),
            "opening_balances": _count("aoiro_opening_balances"),
            "business_ratios": _count("aoiro_business_ratios"),
            "deductions":  _count("aoiro_deductions"),
            "properties":  _count("aoiro_real_estate_properties"),
            "rents":       _count("aoiro_real_estate_rents"),
            "declarations": _count("aoiro_declaration_runs"),
        }
    finally:
        con.close()
