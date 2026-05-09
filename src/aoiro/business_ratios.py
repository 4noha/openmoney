"""家事按分 (aoiro_business_ratios) の CRUD + 解決。

**年度別記録**: 同じ scope でも年度違えば別レコード。毎年見直すケースに対応。

scope の形式:
- 'account:6016'  ← 地代家賃の標準按分率
- 'tx:123'        ← 取引単位の override (将来用、Phase 4 では未使用)

優先順位 (engine から呼ぶ):
1. rule.business_ratio が 100 でない (= rule で明示) → rule のものを使う
2. それ以外 → 'account:<debit_account>' の ratio (当該年度)
3. それも無ければ 100 (全額事業)

按分時の仕訳分割:
- 元額 ¥A、ratio=R% → 経費仕訳 ¥A*R/100、事業主貸 (3002) 振替仕訳 ¥A*(100-R)/100
- ratio=0 (家事費全額) → 経費計上ゼロ、全額 3002 振替
"""
from __future__ import annotations

import sqlite3


def list_business_ratios(con: sqlite3.Connection,
                          fiscal_year: int | None = None) -> list[dict]:
    sql = "SELECT * FROM aoiro_business_ratios"
    args: list = []
    if fiscal_year:
        sql += " WHERE fiscal_year=?"
        args.append(fiscal_year)
    sql += " ORDER BY fiscal_year DESC, scope"
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def upsert_business_ratio(con: sqlite3.Connection, fiscal_year: int, scope: str,
                          ratio_pct: int, note: str = "") -> None:
    if not 0 <= ratio_pct <= 100:
        raise ValueError(f"ratio_pct out of range: {ratio_pct}")
    cur = con.execute(
        "SELECT id FROM aoiro_business_ratios WHERE fiscal_year=? AND scope=?",
        (fiscal_year, scope),
    )
    row = cur.fetchone()
    if row:
        con.execute(
            "UPDATE aoiro_business_ratios SET ratio_pct=?, note=? WHERE id=?",
            (ratio_pct, note, row["id"]),
        )
    else:
        con.execute(
            "INSERT INTO aoiro_business_ratios "
            "(fiscal_year, scope, ratio_pct, note) VALUES (?,?,?,?)",
            (fiscal_year, scope, ratio_pct, note),
        )
    con.commit()


def delete_business_ratio(con: sqlite3.Connection, fiscal_year: int,
                           scope: str) -> bool:
    cur = con.execute(
        "DELETE FROM aoiro_business_ratios WHERE fiscal_year=? AND scope=?",
        (fiscal_year, scope),
    )
    con.commit()
    return cur.rowcount > 0


def load_account_ratios(con: sqlite3.Connection,
                         fiscal_year: int) -> dict[str, int]:
    """指定年度の account_code → ratio_pct dict。engine が事前 load して引く。"""
    out: dict[str, int] = {}
    for r in con.execute(
        "SELECT scope, ratio_pct FROM aoiro_business_ratios "
        "WHERE fiscal_year=? AND scope LIKE 'account:%'",
        (fiscal_year,),
    ).fetchall():
        code = r["scope"].split(":", 1)[1]
        out[code] = int(r["ratio_pct"])
    return out


def copy_to_year(con: sqlite3.Connection, source_year: int,
                  target_year: int) -> int:
    """source_year の按分を target_year にコピー。既存行はスキップ。"""
    rows = list_business_ratios(con, fiscal_year=source_year)
    n = 0
    for r in rows:
        try:
            upsert_business_ratio(con, target_year, r["scope"],
                                    int(r["ratio_pct"]), r.get("note") or "")
            n += 1
        except Exception:
            continue
    return n


# ─────────────────────────────────────────────
# 雛形 (自宅兼事務所の代表的な家事按分対象 + 推奨初期値)
# ─────────────────────────────────────────────
_TEMPLATE: list[tuple[str, int, str]] = [
    ("6003", 30, "水道光熱費 (自宅兼事務所の電気/水道/ガス、目安)"),
    ("6005", 80, "通信費 (個人/業務 共有のネット・携帯、目安)"),
    ("6016", 30, "地代家賃 (自宅兼事務所の家賃、業務専有床面積比目安)"),
    ("6004", 70, "旅費交通費 (取材/打合せが多い場合の目安)"),
    ("7006", 50, "車両費 (営業用と私用の併用、目安)"),
    ("6008", 50, "損害保険料 (火災保険・自動車保険の事業用部分目安)"),
]


def seed_template(con: sqlite3.Connection, fiscal_year: int) -> dict:
    """雛形を一括投入 (指定年度)。既存の (year, scope) はスキップ。"""
    inserted: list[dict] = []
    skipped: list[str] = []
    for code, pct, note in _TEMPLATE:
        scope = f"account:{code}"
        cur = con.execute(
            "SELECT 1 FROM aoiro_business_ratios WHERE fiscal_year=? AND scope=?",
            (fiscal_year, scope),
        )
        if cur.fetchone():
            skipped.append(scope)
            continue
        con.execute(
            "INSERT INTO aoiro_business_ratios "
            "(fiscal_year, scope, ratio_pct, note) VALUES (?,?,?,?)",
            (fiscal_year, scope, pct, note),
        )
        inserted.append({"scope": scope, "ratio_pct": pct, "note": note})
    con.commit()
    return {"fiscal_year": fiscal_year, "inserted": inserted,
            "skipped": skipped, "template_size": len(_TEMPLATE)}


def resolve_ratio(rule: dict, account_ratios: dict[str, int]) -> int:
    """rule + account_ratios から最終的な業務按分率を返す。"""
    rule_ratio = rule.get("business_ratio")
    if rule_ratio is not None and rule_ratio != 100:
        return max(0, min(100, int(rule_ratio)))
    code = (rule.get("debit_account") or "").strip()
    if code in account_ratios:
        return max(0, min(100, account_ratios[code]))
    return 100
