"""不動産所得 — 物件マスタ + 月次収支 + 集計。

青色申告決算書 (不動産所得用) の収支内訳書に必要な
物件単位の収入・経費を管理する。

データ構造:
- aoiro_real_estate_properties: 物件マスタ (name/address/取得日/取得価額)
- aoiro_real_estate_rents: 物件×月の収支 (gross_rent/vacancy/expenses)

事業所得との区別:
- 仕訳エンジンの builtin ルール「経費: 地代家賃 (大家業)」で landlord tag
  付きの 6016 仕訳を生成 (これは「自分が借りている店舗の家賃」想定)
- 大家業の「家賃収入」は本ファイルの aoiro_real_estate_rents に手入力 +
  ユーザが設定した自動仕訳ルールで売上計上 (任意)
"""
from __future__ import annotations

import sqlite3


# ─────────────────────────────────────────────
# 物件マスタ
# ─────────────────────────────────────────────
def list_properties(con: sqlite3.Connection, active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM aoiro_real_estate_properties"
    if active_only:
        sql += " WHERE is_active=1"
    sql += " ORDER BY id"
    return [dict(r) for r in con.execute(sql).fetchall()]


def upsert_property(con: sqlite3.Connection, *, id_: int | None, name: str,
                    address: str = "", acquired_date: str = "",
                    acquired_cost: int = 0, is_active: bool = True,
                    match_keywords: str = "", note: str = "") -> int:
    if id_:
        con.execute(
            "UPDATE aoiro_real_estate_properties SET name=?, address=?, "
            "acquired_date=?, acquired_cost=?, is_active=?, "
            "match_keywords=?, note=? WHERE id=?",
            (name, address, acquired_date or None, acquired_cost,
             1 if is_active else 0, match_keywords, note, id_),
        )
        con.commit()
        return id_
    cur = con.execute(
        "INSERT INTO aoiro_real_estate_properties "
        "(name, address, acquired_date, acquired_cost, is_active, "
        " match_keywords, note) VALUES (?,?,?,?,?,?,?)",
        (name, address, acquired_date or None, acquired_cost,
         1 if is_active else 0, match_keywords, note),
    )
    con.commit()
    return cur.lastrowid


def delete_property(con: sqlite3.Connection, id_: int) -> bool:
    cur = con.execute("DELETE FROM aoiro_real_estate_properties WHERE id=?", (id_,))
    con.commit()
    if cur.rowcount > 0:
        con.execute("DELETE FROM aoiro_real_estate_rents WHERE property_id=?", (id_,))
        con.commit()
        return True
    return False


# ─────────────────────────────────────────────
# 月次収支
# ─────────────────────────────────────────────
def list_rents(con: sqlite3.Connection, property_id: int | None = None,
               year: int | None = None) -> list[dict]:
    sql = "SELECT * FROM aoiro_real_estate_rents"
    args: list = []
    where = []
    if property_id:
        where.append("property_id=?"); args.append(property_id)
    if year:
        where.append("substr(month,1,4)=?"); args.append(f"{year:04d}")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY month, property_id"
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def upsert_rent(con: sqlite3.Connection, *, property_id: int, month: str,
                gross_rent: int = 0, vacancy: int = 0, expenses: int = 0,
                note: str = "") -> None:
    cur = con.execute(
        "SELECT id FROM aoiro_real_estate_rents WHERE property_id=? AND month=?",
        (property_id, month),
    )
    row = cur.fetchone()
    if row:
        con.execute(
            "UPDATE aoiro_real_estate_rents SET gross_rent=?, vacancy=?, "
            "expenses=?, note=? WHERE id=?",
            (gross_rent, vacancy, expenses, note, row["id"]),
        )
    else:
        con.execute(
            "INSERT INTO aoiro_real_estate_rents "
            "(property_id, month, gross_rent, vacancy, expenses, note) "
            "VALUES (?,?,?,?,?,?)",
            (property_id, month, gross_rent, vacancy, expenses, note),
        )
    con.commit()


def delete_rent(con: sqlite3.Connection, property_id: int, month: str) -> None:
    con.execute(
        "DELETE FROM aoiro_real_estate_rents WHERE property_id=? AND month=?",
        (property_id, month),
    )
    con.commit()


# ─────────────────────────────────────────────
# 集計
# ─────────────────────────────────────────────
def yearly_summary(con: sqlite3.Connection, year: int) -> dict:
    """各物件の年間収支 + 物件横断合計を返す (収支内訳書ベース)。"""
    yyyy = f"{year:04d}"
    properties = list_properties(con)
    rows = []
    for p in properties:
        rents = [dict(r) for r in con.execute(
            "SELECT * FROM aoiro_real_estate_rents WHERE property_id=? AND substr(month,1,4)=? "
            "ORDER BY month",
            (p["id"], yyyy),
        ).fetchall()]
        gross = sum(r["gross_rent"] for r in rents)
        vacancy = sum(r["vacancy"] for r in rents)
        expenses = sum(r["expenses"] for r in rents)
        net = gross - vacancy - expenses
        rows.append({
            "property": p,
            "monthly": rents,
            "gross_rent": gross,
            "vacancy": vacancy,
            "expenses": expenses,
            "net_income": net,
        })
    total_gross = sum(r["gross_rent"] for r in rows)
    total_vacancy = sum(r["vacancy"] for r in rows)
    total_expenses = sum(r["expenses"] for r in rows)
    total_net = total_gross - total_vacancy - total_expenses
    return {
        "year": year,
        "rows": rows,
        "total_gross": total_gross,
        "total_vacancy": total_vacancy,
        "total_expenses": total_expenses,
        "total_net_income": total_net,
    }


def _csv_keywords(s: str) -> list[str]:
    return [k.strip() for k in (s or "").replace("\n", ",").split(",") if k.strip()]


def reflect_from_transactions(con: sqlite3.Connection, year: int, *,
                              dry_run: bool = False,
                              overwrite: bool = False) -> dict:
    """transactions の家賃収入を物件マスタの match_keywords でマッチして
    aoiro_real_estate_rents.gross_rent に集計反映する。

    対象: category='家賃収入' OR ('売上' AND landlord tag) の credit > 0 行。
    マッチング: description (NFKC 前処理済) に match_keywords のいずれかが含まれる。
    複数物件にマッチする場合は最初の物件 (id 昇順) のみ採用。

    overwrite=False (default): 既存の月次行 (gross_rent>0) は上書きしない。
    overwrite=True: 全部上書き (集計し直し)。
    dry_run=True: DB 更新しない。preview のみ返す。
    """
    from src.server.categorize import detect_tags  # 純関数

    yyyy = f"{year:04d}"
    properties = list_properties(con, active_only=True)
    # キーワード正規化済みで前計算
    prop_keywords: list[tuple[dict, list[str]]] = []
    for p in properties:
        kws = _csv_keywords(p.get("match_keywords") or "")
        if kws:
            prop_keywords.append((p, kws))

    # 物件×月 → 集計バケット
    buckets: dict[tuple[int, str], int] = {}
    matched_tx_ids: dict[tuple[int, str], list[int]] = {}
    unmatched: list[dict] = []

    cur = con.execute(
        "SELECT id, bank, date, debit, credit, description, "
        "       description_normalized, category "
        "FROM transactions WHERE substr(date,1,4)=? AND credit > 0 "
        "AND (category='家賃収入' OR category='売上') "
        "ORDER BY date, id", (yyyy,),
    )
    for r in cur.fetchall():
        d = dict(r)
        # 売上の場合は landlord tag が付くもののみ対象
        if d["category"] == "売上":
            tags = set(detect_tags(d["description"] or "", d["bank"] or "",
                                    d["description_normalized"] or ""))
            if "landlord" not in tags:
                continue
        desc = (d["description"] or "") + " " + (d["description_normalized"] or "")
        # マッチング (id 昇順、最初にマッチしたもの)
        matched_pid: int | None = None
        for p, kws in prop_keywords:
            if any(k in desc for k in kws):
                matched_pid = p["id"]
                break
        if matched_pid is None:
            unmatched.append({
                "id": d["id"], "date": d["date"], "credit": d["credit"],
                "description": d["description"], "bank": d["bank"],
            })
            continue
        # YYYY/MM/DD → YYYY-MM
        month = d["date"][:7].replace("/", "-")
        key = (matched_pid, month)
        buckets[key] = buckets.get(key, 0) + int(d["credit"])
        matched_tx_ids.setdefault(key, []).append(d["id"])

    # 物件別 preview/結果
    by_prop: dict[int, list[dict]] = {}
    inserted = 0
    skipped_existing = 0
    for (pid, month), amount in sorted(buckets.items()):
        # 既存 row 確認 (vacancy/expenses も取得して保持する)
        existing = con.execute(
            "SELECT gross_rent, vacancy, expenses, note FROM aoiro_real_estate_rents "
            "WHERE property_id=? AND month=?", (pid, month),
        ).fetchone()
        existing_amount = int(existing["gross_rent"]) if existing else 0
        will_skip = bool(existing) and existing_amount > 0 and not overwrite
        action = "skip" if will_skip else ("insert" if not existing else "update")
        if not dry_run and not will_skip:
            upsert_rent(
                con, property_id=pid, month=month, gross_rent=amount,
                vacancy=int(existing["vacancy"] or 0) if existing else 0,
                expenses=int(existing["expenses"] or 0) if existing else 0,
                note=(existing["note"] or "") if existing else "",
            )
            inserted += 1
        elif will_skip:
            skipped_existing += 1
        by_prop.setdefault(pid, []).append({
            "month": month, "amount": amount,
            "tx_ids": matched_tx_ids[(pid, month)],
            "existing_gross_rent": existing_amount,
            "action": action,
        })

    return {
        "year": year,
        "dry_run": dry_run,
        "overwrite": overwrite,
        "by_property": [
            {"property": p, "rows": by_prop.get(p["id"], []),
             "match_keywords": p.get("match_keywords") or ""}
            for p in properties
        ],
        "unmatched": unmatched,
        "applied": 0 if dry_run else inserted,
        "skipped_existing": skipped_existing,
    }


def list_landlord_transactions(transactions_db: sqlite3.Connection, year: int) -> list[dict]:
    """tag='landlord' な transactions を年度別に取得 (参考表示用)。"""
    from src.server.categorize import detect_tags  # 純関数
    yyyy = f"{year:04d}"
    out = []
    cur = transactions_db.execute(
        "SELECT id, bank, date, debit, credit, description, description_normalized, category "
        "FROM transactions WHERE substr(date,1,4)=? AND category IN ('売上','経費') "
        "ORDER BY date, id",
        (yyyy,),
    )
    for r in cur.fetchall():
        d = dict(r)
        tags = detect_tags(d["description"] or "", d["bank"] or "",
                           d["description_normalized"] or "")
        if "landlord" in tags:
            d["tags"] = tags
            out.append(d)
    return out
