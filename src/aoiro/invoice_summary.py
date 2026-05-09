"""消費税申告用の課税仕入集計 (= /ui データを Pull する設計)。

設計ポリシー (= /tax は /ui を Pull のみ): 集計の元データは支出分類
(transactions.category in '経費','今回は経費') で、 /tax 側のテーブル
(tax_expense_items / invoice_vendors) には依存しない。

- 課税仕入 = 経費 / 今回は経費 の transactions
- with_invoice (= 仕入税額控除 100% 対応): receipts に invoice_number あり
  紐付け or invoice_vendors に description_normalized 一致で T 番号あり
- without_invoice (= 経過措置 80% / 50% / 0%): 上記以外の経費
- 経過措置期間 (令和5年10月〜): 〜2026/09/30=80% / 〜2029/09/30=50% / 以降=0%
- 標準税率 10% を仮定 (軽減税率 8% の判定は将来拡張)

確定申告で消費税申告 (簡易課税 or 一般課税) を行う際の参考データ。
"""
from __future__ import annotations

import sqlite3
from datetime import date

# 仕入税額控除 経過措置 (令和5年10月〜)
# 〜2026/09/30: インボイス無対応でも 80% 控除可
# 2026/10/01〜2029/09/30: 50% 控除可
# 2029/10/01〜: 控除不可
_TRANSITION_80_END = date(2026, 9, 30)
_TRANSITION_50_END = date(2029, 9, 30)


def _transition_rate(d: date) -> int:
    """経過措置の控除率 (%) — インボイス無対応の仕入用。"""
    if d <= _TRANSITION_80_END:
        return 80
    if d <= _TRANSITION_50_END:
        return 50
    return 0


def _collect_breakdowns(con: sqlite3.Connection, tx_id: int,
                          tx_amount: int) -> list[dict] | None:
    """tx に紐付く receipt_items を収集し、 内訳合計が tx_amount と一致すれば
    内訳リスト [{merchant, invoice_number, amount, label}, ...] を返す。
    一致しない / 内訳が無い場合は None (= 親 receipt ベースの集計に fallback)。

    1 枚のレシートで複数事業者 (= 上水道 / 下水道 等) を別々の T 番号で集計するため。
    """
    rows = con.execute(
        """
        SELECT i.label, i.merchant, i.invoice_number, i.amount
        FROM receipt_items i
        JOIN receipt_links rl ON rl.receipt_id = i.receipt_id
        WHERE rl.transaction_id = ?
        ORDER BY i.receipt_id, i.sort_order
        """,
        (tx_id,),
    ).fetchall()
    if not rows:
        return None
    items = [
        {
            "label": (r["label"] or "").strip(),
            "merchant": (r["merchant"] or "").strip(),
            "invoice_number": (r["invoice_number"] or "").strip(),
            "amount": int(r["amount"] or 0),
        }
        for r in rows
    ]
    items = [it for it in items if it["amount"] > 0]
    if not items:
        return None
    items_sum = sum(it["amount"] for it in items)
    if items_sum != tx_amount:
        return None
    return items


def _accumulate(amount: int, invoice_number: str, merchant: str, vendor_key_name: str,
                  with_invoice: dict, no_invoice: dict,
                  by_vendor: dict, tx_date: date) -> None:
    """1 行 (= tx 全体 or 内訳 1 件) を with_invoice / no_invoice に加算する共通処理。"""
    invoice_number = (invoice_number or "").strip()
    merchant = (merchant or "").strip()
    has_inv = bool(invoice_number)
    tax_excluded = amount * 100 // 110
    tax_amount = amount - tax_excluded
    if has_inv:
        with_invoice["count"] += 1
        with_invoice["total_tax_in"] += amount
        with_invoice["tax_excluded"] += tax_excluded
        with_invoice["tax_amount"] += tax_amount
        with_invoice["creditable_tax"] += tax_amount
    else:
        no_invoice["count"] += 1
        no_invoice["total_tax_in"] += amount
        no_invoice["tax_excluded"] += tax_excluded
        no_invoice["tax_amount"] += tax_amount
        rate = _transition_rate(tx_date)
        credit = tax_amount * rate // 100
        no_invoice["creditable_tax"] += credit
        if rate == 80:
            no_invoice["transition_80_count"] += 1
            no_invoice["transition_80_amount"] += amount
        elif rate == 50:
            no_invoice["transition_50_count"] += 1
            no_invoice["transition_50_amount"] += amount
        else:
            no_invoice["no_credit_count"] += 1
            no_invoice["no_credit_amount"] += amount
    # by_vendor の key: invoice_number あれば事業者の正式 ID として優先 (= 同 T 番号は
    # service_name 違い (= MUFG「水道 <description>」 vs レシート「草津市水道お客様
    # センター」) でも 1 行に集約する)。 invoice_number 無しは従来通り (service_name, merchant)。
    if invoice_number:
        key = ("__inv__", invoice_number)
    else:
        key = (vendor_key_name, merchant)
    if key not in by_vendor:
        by_vendor[key] = {
            "service_name": vendor_key_name, "company_name": merchant,
            "invoice_number": invoice_number,
            "count": 0, "total": 0, "has_invoice": has_inv,
        }
    by_vendor[key]["count"] += 1
    by_vendor[key]["total"] += amount
    if has_inv and not by_vendor[key]["has_invoice"]:
        by_vendor[key]["has_invoice"] = True
        by_vendor[key]["invoice_number"] = invoice_number


def compute_invoice_summary(con: sqlite3.Connection, year: int) -> dict:
    """指定年度の課税仕入をインボイス対応 / 非対応別に集計。

    /ui (transactions) から Pull する: category in ('経費', '今回は経費') の
    全 tx を対象にし、 紐付く receipts → receipt_items の内訳 → invoice_vendors
    補完の優先順で適格請求書番号を解決。

    内訳 (receipt_items) があり合計が tx.debit と完全一致するなら内訳ごとに
    別事業者として集計 (= 上水道 T78... と 下水道 T88... を別々に控除計算)。

    Returns:
        {
          "year": YYYY,
          "total_count", "total_amount_tax_in" (税込合計),
          "with_invoice": {count, total_tax_in, tax_excluded, tax_amount,
                           creditable_tax (= 控除対象仕入税額)},
          "without_invoice": {count, total_tax_in, tax_excluded, tax_amount,
                               creditable_tax (経過措置 80% or 50% 適用後)},
          "by_vendor": [{
              service_name, company_name, invoice_number,
              count, total, has_invoice
          }],
        }
    """
    rows = con.execute("""
        SELECT
          t.id, t.bank, t.date, t.debit AS amount,
          t.description, t.description_normalized,
          -- 紐付くレシート (PDF 実物 or メタのみ) から invoice_number を取る
          COALESCE(
            (SELECT r.invoice_number FROM receipt_links rl
             JOIN receipts r ON r.id = rl.receipt_id
             WHERE rl.transaction_id = t.id
               AND r.invoice_number IS NOT NULL AND r.invoice_number != ''
             LIMIT 1),
            -- 補完: vendor マスタ (description_normalized 一致)
            NULLIF((SELECT iv.invoice_number FROM invoice_vendors iv
                    WHERE iv.service_name != ''
                      AND iv.service_name = t.description_normalized
                    LIMIT 1), ''),
            ''
          ) AS resolved_invoice_number,
          COALESCE(
            (SELECT r.merchant FROM receipt_links rl
             JOIN receipts r ON r.id = rl.receipt_id
             WHERE rl.transaction_id = t.id
               AND r.merchant IS NOT NULL AND r.merchant != ''
             LIMIT 1),
            (SELECT iv.company_name FROM invoice_vendors iv
             WHERE iv.service_name = t.description_normalized
             LIMIT 1),
            ''
          ) AS resolved_merchant
        FROM transactions t
        WHERE t.category IN ('経費', '今回は経費')
          AND substr(t.date, 1, 4) = ?
          -- 為替手数料 / 振込手数料 は親振込に category 追従、 適格請求書発行
          -- 対象外なのでインボイス集計から除外。
          AND t.description_normalized NOT LIKE '%為替手数料%'
          AND t.description_normalized NOT LIKE '%振込手数料%'
          AND t.description_normalized NOT LIKE '%テスウリヨウ%'
          -- レシート tx で同 receipt が元経費 tx (MUFG/VPASS 等) にも link されている
          -- 場合は除外 (= 元経費 tx 側で集計するので二重カウントを防ぐ)。
          AND NOT (
            t.bank = 'レシート'
            AND EXISTS (
              SELECT 1 FROM receipt_links rl1
              JOIN receipt_links rl2 ON rl1.receipt_id = rl2.receipt_id
                                     AND rl2.transaction_id != rl1.transaction_id
              JOIN transactions t2 ON t2.id = rl2.transaction_id
              WHERE rl1.transaction_id = t.id
                AND t2.bank != 'レシート'
                AND t2.category IN ('経費', '今回は経費')
            )
          )
    """, (str(year),)).fetchall()

    with_invoice = {"count": 0, "total_tax_in": 0,
                     "tax_excluded": 0, "tax_amount": 0, "creditable_tax": 0}
    no_invoice = {"count": 0, "total_tax_in": 0,
                   "tax_excluded": 0, "tax_amount": 0, "creditable_tax": 0,
                   "transition_80_count": 0, "transition_80_amount": 0,
                   "transition_50_count": 0, "transition_50_amount": 0,
                   "no_credit_count": 0, "no_credit_amount": 0}

    by_vendor: dict[tuple[str, str], dict] = {}
    for r in rows:
        amount = int(r["amount"] or 0)
        if amount <= 0:
            continue
        desc_norm = (r["description_normalized"] or "").strip()
        # date 計算は経過措置で再利用するため事前に
        d_str = (r["date"] or "").replace("/", "-")
        try:
            yy, mm, dd = (int(p) for p in d_str.split("-")[:3])
            tx_date = date(yy, mm, dd)
        except Exception:
            tx_date = date(year, 12, 31)

        # 内訳が tx.debit と一致する場合は内訳ベースで分割集計
        breakdowns = _collect_breakdowns(con, r["id"], amount)
        if breakdowns:
            for bd in breakdowns:
                _accumulate(
                    bd["amount"], bd["invoice_number"], bd["merchant"],
                    desc_norm or bd["label"],
                    with_invoice, no_invoice, by_vendor, tx_date,
                )
            continue

        # 内訳なし or 不一致 → tx 全体を 1 行として _accumulate
        invoice_number = (r["resolved_invoice_number"] or "").strip()
        merchant = (r["resolved_merchant"] or "").strip()
        _accumulate(amount, invoice_number, merchant, desc_norm,
                     with_invoice, no_invoice, by_vendor, tx_date)

    vendors_sorted = sorted(by_vendor.values(), key=lambda x: -x["total"])

    return {
        "year": year,
        "total_count": with_invoice["count"] + no_invoice["count"],
        "total_amount_tax_in":
            with_invoice["total_tax_in"] + no_invoice["total_tax_in"],
        "with_invoice": with_invoice,
        "without_invoice": no_invoice,
        "by_vendor": vendors_sorted,
        "creditable_total":
            with_invoice["creditable_tax"] + no_invoice["creditable_tax"],
        "note": "標準税率 10% で計算。軽減税率 8% は未対応 (将来拡張)。",
    }
