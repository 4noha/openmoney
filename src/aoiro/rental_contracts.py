"""賃借物件 (地代家賃) — 個人事業主がオフィス/倉庫を借りる側の管理。

確定申告書 (青色申告決算書 3 枚目)「地代家賃の内訳」 用。
**年度別記録 + 年額主体**: 申告書には年額しか書かないので、annual_rent をメインに持つ。
月額 (monthly_rent) は補助参考値 (実費 median 等)。

不動産所得 (大家業) 用の `aoiro_real_estate_properties` とは別軸。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime


def list_contracts(con: sqlite3.Connection,
                    fiscal_year: int | None = None) -> list[dict]:
    sql = "SELECT * FROM aoiro_rental_contracts"
    args: list = []
    if fiscal_year:
        sql += " WHERE fiscal_year=?"
        args.append(fiscal_year)
    sql += " ORDER BY fiscal_year DESC, id DESC"
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def get_contract(con: sqlite3.Connection, id_: int) -> dict | None:
    r = con.execute(
        "SELECT * FROM aoiro_rental_contracts WHERE id=?", (id_,)
    ).fetchone()
    return dict(r) if r else None


def upsert_contract(con: sqlite3.Connection, *, id_: int | None = None,
                     fiscal_year: int,
                     property_name: str, property_address: str = "",
                     area_sqm: float = 0, usage: str = "",
                     business_ratio_pct: int = 100,
                     annual_rent: int = 0,
                     monthly_rent: int = 0, deposit: int = 0,
                     management_fee: int = 0,
                     landlord_name: str = "", landlord_address: str = "",
                     landlord_phone: str = "", note: str = "") -> int:
    now = datetime.now().isoformat()
    if id_:
        cur = con.execute("SELECT 1 FROM aoiro_rental_contracts WHERE id=?", (id_,))
        if cur.fetchone() is None:
            raise ValueError(f"contract id {id_} not found")
        con.execute(
            "UPDATE aoiro_rental_contracts SET "
            "fiscal_year=?, property_name=?, property_address=?, area_sqm=?, usage=?, "
            "business_ratio_pct=?, annual_rent=?, monthly_rent=?, deposit=?, management_fee=?, "
            "landlord_name=?, landlord_address=?, landlord_phone=?, "
            "note=?, updated_at=? WHERE id=?",
            (fiscal_year, property_name, property_address, area_sqm, usage,
             business_ratio_pct, annual_rent, monthly_rent, deposit, management_fee,
             landlord_name, landlord_address, landlord_phone, note, now, id_),
        )
        con.commit()
        return id_
    cur = con.execute(
        "INSERT INTO aoiro_rental_contracts "
        "(fiscal_year, property_name, property_address, area_sqm, usage, "
        " business_ratio_pct, annual_rent, monthly_rent, deposit, management_fee, "
        " landlord_name, landlord_address, landlord_phone, note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fiscal_year, property_name, property_address, area_sqm, usage,
         business_ratio_pct, annual_rent, monthly_rent, deposit, management_fee,
         landlord_name, landlord_address, landlord_phone, note),
    )
    con.commit()
    return cur.lastrowid


def delete_contract(con: sqlite3.Connection, id_: int) -> bool:
    cur = con.execute("DELETE FROM aoiro_rental_contracts WHERE id=?", (id_,))
    con.commit()
    return cur.rowcount > 0


def copy_to_year(con: sqlite3.Connection, source_year: int,
                  target_year: int) -> int:
    rows = list_contracts(con, fiscal_year=source_year)
    n = 0
    for r in rows:
        upsert_contract(
            con, fiscal_year=target_year,
            property_name=r["property_name"], property_address=r["property_address"] or "",
            area_sqm=r["area_sqm"] or 0, usage=r["usage"] or "",
            business_ratio_pct=r["business_ratio_pct"] or 100,
            annual_rent=r["annual_rent"] or 0,
            monthly_rent=r["monthly_rent"] or 0,
            deposit=r["deposit"] or 0, management_fee=r["management_fee"] or 0,
            landlord_name=r["landlord_name"] or "",
            landlord_address=r["landlord_address"] or "",
            landlord_phone=r["landlord_phone"] or "",
            note=f"{source_year}年からコピー" + (f" / {r['note']}" if r.get("note") else ""),
        )
        n += 1
    return n


def annual_rent_total(con: sqlite3.Connection,
                      fiscal_year: int) -> dict:
    """指定年度の年間賃料合計 (青色申告決算書 3 枚目用)。
    annual_rent を直接合算 (申告書はこの値を採用)。
    """
    rows = list_contracts(con, fiscal_year=fiscal_year)
    total_rent = 0
    total_business = 0
    for r in rows:
        annual = int(r["annual_rent"] or 0)
        total_rent += annual
        total_business += annual * (r["business_ratio_pct"] or 100) // 100
    return {
        "fiscal_year": fiscal_year,
        "contract_count": len(rows),
        "annual_rent_gross": total_rent,
        "annual_rent_business": total_business,
    }
