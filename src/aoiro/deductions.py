"""所得控除 (aoiro_deductions) の CRUD + 集計。

申告書 B 第一表の「所得から差し引かれる金額」欄に集計される。

種類 (kind):
- 医療費控除         (年 10 万 or 所得5% を超えた額)
- 社会保険料控除     (国民年金 / 国民健康保険 / 介護保険等)
- 小規模企業共済等掛金控除 (小規模 / iDeCo)
- 生命保険料控除
- 地震保険料控除
- 寄附金控除         (ふるさと納税 等)
- 配偶者控除 / 配偶者特別控除
- 扶養控除
- 障害者控除
- ひとり親 / 寡婦控除
- 勤労学生控除
- 基礎控除           (合計所得2,400万以下なら48万)

medical タグ付き transactions からの自動取込みは別 API
(医療費明細書フォーマット用は Phase 6 後半 / 印刷フェーズで対応)。
"""
from __future__ import annotations

import sqlite3

# 標準的な kind の表示順 (申告書 B 第二表に準拠)
KIND_ORDER = [
    "医療費",
    "社保",
    "小規模",       # 小規模企業共済等掛金 (iDeCo を含む)
    "iDeCo",        # 小規模の内訳として分離管理 (UI で見やすくするため)
    "生命保険",
    "地震保険",
    "寄附金",
    "ふるさと納税",  # 寄附金の内訳を分けて記録
    "配偶者",
    "扶養",
    "障害者",
    "寡婦",
    "勤労学生",
    "基礎控除",
]


def list_deductions(con: sqlite3.Connection, year: int | None = None,
                    kind: str = "") -> list[dict]:
    sql = "SELECT * FROM aoiro_deductions"
    args: list = []
    where = []
    if year:
        where.append("fiscal_year=?"); args.append(year)
    if kind:
        where.append("kind=?"); args.append(kind)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY fiscal_year, kind, id"
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def upsert_deduction(con: sqlite3.Connection, *, id_: int | None,
                     fiscal_year: int, kind: str, payee: str = "",
                     amount: int = 0, evidence_path: str = "",
                     note: str = "") -> int:
    if id_:
        con.execute(
            "UPDATE aoiro_deductions SET fiscal_year=?, kind=?, payee=?, "
            "amount=?, evidence_path=?, note=? WHERE id=?",
            (fiscal_year, kind, payee, amount, evidence_path, note, id_),
        )
        con.commit()
        return id_
    cur = con.execute(
        "INSERT INTO aoiro_deductions "
        "(fiscal_year, kind, payee, amount, evidence_path, note) "
        "VALUES (?,?,?,?,?,?)",
        (fiscal_year, kind, payee, amount, evidence_path, note),
    )
    con.commit()
    return cur.lastrowid


def delete_deduction(con: sqlite3.Connection, id_: int) -> bool:
    cur = con.execute("DELETE FROM aoiro_deductions WHERE id=?", (id_,))
    con.commit()
    return cur.rowcount > 0


def yearly_summary(con: sqlite3.Connection, year: int) -> dict:
    """年度別 kind 集計。"""
    rows = list_deductions(con, year=year)
    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    summary: list[dict] = []
    for kind in KIND_ORDER:
        items = by_kind.get(kind, [])
        if items:
            summary.append({
                "kind": kind,
                "count": len(items),
                "total": sum(i["amount"] for i in items),
                "items": items,
            })
    # KIND_ORDER に無い kind も末尾に追加 (ユーザ独自)
    for kind, items in by_kind.items():
        if kind in KIND_ORDER:
            continue
        summary.append({
            "kind": kind, "count": len(items),
            "total": sum(i["amount"] for i in items), "items": items,
        })
    grand_total = sum(s["total"] for s in summary)
    return {"year": year, "kinds": summary, "grand_total": grand_total}


def list_medical_transactions(transactions_db: sqlite3.Connection, year: int) -> list[dict]:
    """tag='medical' な transactions を年度別に列挙 (医療費控除候補)。"""
    from src.server.categorize import detect_tags
    yyyy = f"{year:04d}"
    out = []
    for r in transactions_db.execute(
        "SELECT id, bank, date, debit, credit, description, description_normalized, category "
        "FROM transactions WHERE substr(date,1,4)=? "
        "ORDER BY date, id", (yyyy,)
    ).fetchall():
        d = dict(r)
        tags = detect_tags(d["description"] or "", d["bank"] or "",
                           d["description_normalized"] or "")
        if "medical" in tags:
            d["tags"] = tags
            out.append(d)
    return out


def list_insurance_transactions(transactions_db: sqlite3.Connection, year: int) -> list[dict]:
    """tag='insurance' な transactions を年度別に列挙 (生命/地震保険等の控除候補)。

    自動車保険は所得控除の対象外 (事業経費 6008 損害保険料 として
    PL に計上、または家事按分で 事業主貸 振替) なので除外する。
    """
    from src.server.categorize import detect_tags
    yyyy = f"{year:04d}"
    AUTO_INSURANCE_KEYWORDS = ("自動車保険", "ジドウシャホケン", "カーホケン",
                                "車両保険", "自賠責", "ジバイセキ")
    out = []
    for r in transactions_db.execute(
        "SELECT id, bank, date, debit, credit, description, description_normalized, category "
        "FROM transactions WHERE substr(date,1,4)=? AND debit > 0 "
        "ORDER BY date, id", (yyyy,)
    ).fetchall():
        d = dict(r)
        desc_blob = (d["description"] or "") + " " + (d["description_normalized"] or "")
        if any(k in desc_blob for k in AUTO_INSURANCE_KEYWORDS):
            continue
        tags = detect_tags(d["description"] or "", d["bank"] or "",
                           d["description_normalized"] or "")
        if "insurance" in tags:
            d["tags"] = tags
            out.append(d)
    return out


def bulk_add_from_transactions(con: sqlite3.Connection, *,
                               fiscal_year: int, kind: str, payee: str,
                               tx_ids: list[int], note: str = "") -> dict:
    """指定 tx_id 群の合計を 1 件の控除として登録、または個別に登録する。
    description は tx の概要を join して保存。
    """
    if not tx_ids:
        return {"status": "no_tx", "amount": 0, "id": None}
    placeholders = ",".join("?" * len(tx_ids))
    rows = con.execute(
        f"SELECT id, date, debit, description FROM transactions "
        f"WHERE id IN ({placeholders})",
        tx_ids,
    ).fetchall()
    total = sum(int(r["debit"] or 0) for r in rows)
    if total <= 0:
        return {"status": "zero", "amount": 0, "id": None}
    summary_note = note or f"transactions {len(rows)}件: " + \
        ", ".join(f"#{r['id']} {r['date']}" for r in rows[:5]) + \
        (f" ほか" if len(rows) > 5 else "")
    cur = con.execute(
        "INSERT INTO aoiro_deductions "
        "(fiscal_year, kind, payee, amount, evidence_path, note) "
        "VALUES (?,?,?,?,'',?)",
        (fiscal_year, kind, payee, total, summary_note),
    )
    con.commit()
    return {"status": "ok", "amount": total, "id": cur.lastrowid,
            "tx_count": len(rows)}
