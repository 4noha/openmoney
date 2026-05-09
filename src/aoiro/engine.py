"""仕訳エンジン: transactions → aoiro_journal_entries 自動生成。

設計:
- 入力: transactions テーブル (read-only)、aoiro_journal_rules、aoiro_payment_accounts
- 出力: aoiro_journal_entries (source='auto' のみ管理。手動仕訳は触らない)
- 冪等: rebuild_year(year) は year の auto 仕訳を全削除 → 再生成
- categorize.py の detect_tags() を read-only でのみ使用 (副作用なし)
"""
from __future__ import annotations

import sqlite3

from src.aoiro.business_ratios import load_account_ratios, resolve_ratio
from src.aoiro.rules import matches
# detect_tags は src.server を巻き込んで循環参照になるため、関数内で遅延 import する

# 家事按分の振替先科目 (事業からのオーナー引出し)
_OWNER_DRAW_ACCOUNT = "3002"  # 事業主貸


# ─────────────────────────────────────────────
# tx 1 件を仕訳に変換
# ─────────────────────────────────────────────
def _tx_amount(tx: dict, category: str) -> int:
    """category 別に取るべき金額を返す。
    - 借方系 (経費/個人支出/出金): tx.debit
    - 貸方系 (売上/給与/非課税/保険金/返金/家賃収入): tx.credit
    """
    if category in ("経費", "個人支出", "出金"):
        return int(tx.get("debit") or 0)
    if category in ("売上", "給与", "非課税", "保険金", "返金", "家賃収入"):
        return int(tx.get("credit") or 0)
    return 0


def _resolve_journal(rule: dict, payment: dict | None, *,
                     category: str, amount: int,
                     account_ratios: dict[str, int]) -> list[tuple[str, int, str, int]] | None:
    """rule + payment_account + 按分率から仕訳を解決。
    通常は 1 本、家事按分が 100 未満のときは経費仕訳 + 事業主貸振替の 2 本を返す。
    """
    debit = (rule.get("debit_account") or "").strip()
    credit = (rule.get("credit_account") or "").strip()
    if category == "経費":
        if not credit and payment:
            credit = payment["expense_credit_account"]
        if not debit or not credit:
            return None
        ratio = resolve_ratio(rule, account_ratios)
        biz_amount = amount * ratio // 100
        residual = amount - biz_amount
        out = []
        if biz_amount > 0:
            out.append((debit, biz_amount, credit, biz_amount))
        if residual > 0:
            # 家事按分の残額 → 事業主貸 (オーナーの私的支出)
            out.append((_OWNER_DRAW_ACCOUNT, residual, credit, residual))
        return out or None
    if category == "売上":
        if not debit and payment:
            debit = payment["income_debit_account"] or payment["expense_credit_account"]
        if not debit or not credit:
            return None
        return [(debit, amount, credit, amount)]
    if category == "個人支出":
        # 個人支出: 借方 = 事業主貸 (3002) / 貸方 = カードや銀行 (payment.expense_credit_account)
        # オーナーが事業のお金 (普通預金 / カード未払金) で個人支出を行った扱い。
        # 引落日に未払金が残らないよう、混合カードでも貸方残を生成する。
        if not debit:
            debit = _OWNER_DRAW_ACCOUNT
        if not credit and payment:
            credit = payment["expense_credit_account"]
        if not debit or not credit:
            return None
        return [(debit, amount, credit, amount)]
    if category in ("給与", "非課税", "保険金", "返金"):
        # 事業外入金: 借方 1002 普通預金 / 貸方 3003 事業主借
        # 事業口座が個人口座兼用の場合に、入金を事業帳簿に取り込む。
        # PL には影響しない (3003 は資本科目)。
        if not debit or not credit:
            return None
        return [(debit, amount, credit, amount)]
    if category == "出金":
        # 突合無し出金: 借方 3002 事業主貸 / 貸方 1002 普通預金
        # (突合済の引落は engine.py 側で別処理 = bank_settlement_inserted)
        if not debit or not credit:
            return None
        return [(debit, amount, credit, amount)]
    return None


# ─────────────────────────────────────────────
# rebuild
# ─────────────────────────────────────────────
def rebuild_year(con: sqlite3.Connection, year: int,
                 transactions_db: sqlite3.Connection | None = None) -> dict:
    """指定年度の auto 仕訳を全削除 → tx を読んで再生成。
    transactions_db は別 connection でもよいが、同じ db ファイルなら con を流用可能。
    """
    if transactions_db is None:
        transactions_db = con

    # 遅延 import (server を巻き込まないように)
    from src.server.categorize import detect_tags

    # 期首開始仕訳を再生成 (前年 BS から繰越)
    # auto 仕訳生成より先に行うことで、当期 BS 計算時に正しい期首が乗る。
    from src.aoiro.opening_entries import rebuild_opening
    opening_result = rebuild_opening(con, year)

    # rules / payment_accounts を全部読込
    rules = [dict(r) for r in con.execute(
        "SELECT * FROM aoiro_journal_rules WHERE is_active=1 ORDER BY priority, id"
    ).fetchall()]
    payments = {r["bank"]: dict(r) for r in con.execute(
        "SELECT * FROM aoiro_payment_accounts"
    ).fetchall()}
    account_ratios = load_account_ratios(con, year)

    # ── 複式簿記対応: tx_links を参照して二重計上を排除 ──
    # shop_card / amazon_split: ストア注文 (tx_a) ↔ カード明細 (tx_b)
    #   → カード側 tx_b は注文側で経費計上済なのでスキップ
    redundant_card_ids: set[int] = set()
    for r in transactions_db.execute(
        "SELECT tx_b_id FROM tx_links "
        "WHERE link_type IN ('shop_card', 'amazon_split') AND tx_b_id IS NOT NULL"
    ).fetchall():
        redundant_card_ids.add(int(r[0]))
    # card_bank_billing / mercard_mufg: 銀行引落 (tx_a=MUFG) がカード月次集計とリンク
    #   → 銀行引落 tx は経費仕訳ではなく「未払金消去」仕訳に変換
    bank_settlement_ids: set[int] = set()
    for r in transactions_db.execute(
        "SELECT tx_a_id FROM tx_links "
        "WHERE link_type IN ('card_bank_billing', 'mercard_mufg') AND tx_a_id IS NOT NULL"
    ).fetchall():
        bank_settlement_ids.add(int(r[0]))

    # 既存 auto 仕訳を削除
    con.execute("DELETE FROM aoiro_journal_entries WHERE fiscal_year=? AND source='auto'",
                (year,))

    # transactions を読む (経費 / 売上 のみ。給与・個人支出・出金・返金 は事業仕訳しない)
    # 事業口座が個人口座兼用の場合、普通預金の動きを実態に合わせるため
    # 給与 / 非課税 / 保険金 / 返金 (事業外入金) も借方 1002 / 貸方 3003 で取込む。
    # 個人支出はカード払いを未払金経由で記録、出金は引落 or 個人引出で記録。
    target_categories = [
        "経費", "売上", "出金", "個人支出",
        "給与", "非課税", "保険金", "返金",
    ]
    yyyy = f"{year:04d}"
    placeholders = ",".join("?" * len(target_categories))
    cur = transactions_db.execute(
        f"SELECT id, bank, date, debit, credit, description, "
        f"       description_normalized, category "
        f"FROM transactions WHERE substr(date,1,4)=? AND category IN ({placeholders}) "
        f"ORDER BY date, id",
        (yyyy, *target_categories),
    )
    txs = [dict(r) for r in cur.fetchall()]

    inserted = 0
    skipped_no_rule = 0
    skipped_no_payment = 0
    skipped_redundant_card = 0
    bank_settlement_inserted = 0
    for tx in txs:
        category = tx["category"]
        bank = tx.get("bank") or ""

        # ── 複式簿記: shop_card 突合済みのカード明細はスキップ (注文側で計上済) ──
        if tx["id"] in redundant_card_ids:
            skipped_redundant_card += 1
            continue

        # ── 複式簿記: 銀行引落 (card_bank_billing/mercard_mufg) は
        #     借方 2010 未払金 / 貸方 1002 普通預金 の決済仕訳に変換 ──
        if tx["id"] in bank_settlement_ids:
            amount = int(tx.get("debit") or 0)
            if amount > 0:
                con.execute(
                    "INSERT INTO aoiro_journal_entries "
                    "(fiscal_year, date, debit_account, debit_amount, credit_account, credit_amount, "
                    " description, source, source_tx_id, source_category) "
                    "VALUES (?,?,?,?,?,?,?,'auto',?,?)",
                    (year, tx["date"], "2010", amount, "1002", amount,
                     f"[{bank}] {tx.get('description') or ''}"[:300], tx["id"],
                     tx.get("category")),
                )
                bank_settlement_inserted += 1
            continue

        # 突合無し「出金」 は下のルールマッチングで「出金: 個人引出」に当たり
        # 借方 3002 事業主貸 / 貸方 1002 普通預金 として仕訳化される。

        amount = _tx_amount(tx, category)
        if amount <= 0:
            continue
        # tags 判定 (read-only)
        tags = set(detect_tags(
            tx.get("description") or "",
            bank,
            tx.get("description_normalized") or "",
        ))
        # rule マッチング
        matched = None
        for r in rules:
            if matches(r, category=category, tags=tags, bank=bank,
                       description=tx.get("description") or "", amount=amount):
                matched = r
                break
        if matched is None:
            skipped_no_rule += 1
            continue
        payment = payments.get(bank)
        resolved = _resolve_journal(matched, payment, category=category, amount=amount,
                                     account_ratios=account_ratios)
        if not resolved:
            skipped_no_payment += 1
            continue
        for debit_acc, debit_amt, credit_acc, credit_amt in resolved:
            label = f"[{bank}] {tx.get('description') or ''}"
            if debit_acc == _OWNER_DRAW_ACCOUNT:
                label = f"[家事按分] {label}"
            con.execute(
                "INSERT INTO aoiro_journal_entries "
                "(fiscal_year, date, debit_account, debit_amount, credit_account, credit_amount, "
                " description, source, source_tx_id, source_rule_id, source_category) "
                "VALUES (?,?,?,?,?,?,?,'auto',?,?,?)",
                (year, tx["date"], debit_acc, debit_amt, credit_acc, credit_amt,
                 label[:300], tx["id"], matched["id"], tx.get("category")),
            )
            inserted += 1
    con.commit()
    return {
        "year": year,
        "inserted": inserted,
        "bank_settlement_inserted": bank_settlement_inserted,
        "skipped_redundant_card": skipped_redundant_card,
        "skipped_no_rule": skipped_no_rule,
        "skipped_no_payment": skipped_no_payment,
        "candidate_tx": len(txs),
        "opening": opening_result,
    }


# ─────────────────────────────────────────────
# 一覧取得
# ─────────────────────────────────────────────
def list_journal(con: sqlite3.Connection, year: int | None = None,
                 source: str | None = None, limit: int = 5000) -> list[dict]:
    # 添付ファイル件数も同時に取得 (LEFT JOIN + GROUP BY)
    sql = (
        "SELECT j.*, COALESCE(a.cnt, 0) AS attachment_count "
        "FROM aoiro_journal_entries j "
        "LEFT JOIN (SELECT journal_entry_id, COUNT(*) AS cnt "
        "           FROM aoiro_attachments WHERE journal_entry_id IS NOT NULL "
        "           GROUP BY journal_entry_id) a "
        "ON a.journal_entry_id = j.id"
    )
    args: list = []
    where = []
    if year:
        where.append("j.fiscal_year=?"); args.append(year)
    if source:
        where.append("j.source=?"); args.append(source)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY j.date, j.id LIMIT ?"
    args.append(limit)
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def insert_manual_entry(con: sqlite3.Connection, *, fiscal_year: int, date: str,
                        debit_account: str, debit_amount: int,
                        credit_account: str, credit_amount: int,
                        description: str = "") -> int:
    cur = con.execute(
        "INSERT INTO aoiro_journal_entries "
        "(fiscal_year,date,debit_account,debit_amount,credit_account,credit_amount,"
        " description,source) VALUES (?,?,?,?,?,?,?,'manual')",
        (fiscal_year, date, debit_account, debit_amount, credit_account,
         credit_amount, description),
    )
    con.commit()
    return cur.lastrowid


def delete_entry(con: sqlite3.Connection, id_: int) -> bool:
    cur = con.execute("SELECT source FROM aoiro_journal_entries WHERE id=?", (id_,))
    row = cur.fetchone()
    if row is None:
        return False
    if row["source"] == "auto":
        # auto は rebuild で消えるので個別削除は禁止 (rebuild 待ち)
        return False
    con.execute("DELETE FROM aoiro_journal_entries WHERE id=?", (id_,))
    con.commit()
    return True
