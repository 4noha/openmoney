"""勘定科目マスタの seed + CRUD。

国税庁「青色申告決算書 (一般用)」と「不動産所得用 収支内訳書」で使われる
標準科目 + 個人事業主でよく使う科目を builtin として登録。ユーザは追加可能。
seed は冪等で、すでに存在する code は触らない (ユーザの編集を保持)。
"""
from __future__ import annotations

import sqlite3

from src.aoiro.schema import ensure_aoiro_schema, open_db

# (code, name, type, category, sort_order)
# code は 4桁数値文字列。1xxx=資産 / 2xxx=負債 / 3xxx=資本 / 4xxx=収益 / 5xxx-7xxx=費用
_BUILTIN: list[tuple[str, str, str, str, int]] = [
    # ─── 資産 ─────────────────────────────────
    ("1001", "現金",            "asset", "流動資産", 100),
    ("1002", "普通預金",        "asset", "流動資産", 110),
    ("1003", "定期預金",        "asset", "流動資産", 120),
    ("1010", "売掛金",          "asset", "流動資産", 200),
    ("1011", "受取手形",        "asset", "流動資産", 210),
    ("1020", "棚卸資産",        "asset", "流動資産", 300),
    ("1030", "前払金",          "asset", "流動資産", 400),
    ("1031", "立替金",          "asset", "流動資産", 410),
    ("1032", "仮払金",          "asset", "流動資産", 420),
    ("1040", "貸付金",          "asset", "流動資産", 500),
    ("1100", "建物",            "asset", "固定資産", 1000),
    ("1101", "建物附属設備",    "asset", "固定資産", 1010),
    ("1102", "構築物",          "asset", "固定資産", 1020),
    ("1110", "機械装置",        "asset", "固定資産", 1100),
    ("1120", "車両運搬具",      "asset", "固定資産", 1200),
    ("1130", "工具器具備品",    "asset", "固定資産", 1300),
    ("1140", "土地",            "asset", "固定資産", 1400),
    ("1150", "一括償却資産",    "asset", "固定資産", 1500),
    ("1151", "少額減価償却資産", "asset", "固定資産", 1510),
    ("1200", "敷金",            "asset", "投資その他", 2000),
    ("1201", "保証金",          "asset", "投資その他", 2010),
    ("1210", "投資有価証券",    "asset", "投資その他", 2100),
    ("1220", "出資金",          "asset", "投資その他", 2200),
    # ─── 負債 ─────────────────────────────────
    ("2001", "買掛金",          "liability", "流動負債", 100),
    ("2002", "支払手形",        "liability", "流動負債", 110),
    ("2010", "未払金",          "liability", "流動負債", 200),
    ("2011", "未払費用",        "liability", "流動負債", 210),
    ("2012", "未払消費税",      "liability", "流動負債", 220),
    ("2020", "預り金",          "liability", "流動負債", 300),
    ("2021", "前受金",          "liability", "流動負債", 310),
    ("2022", "仮受金",          "liability", "流動負債", 320),
    ("2030", "短期借入金",      "liability", "流動負債", 400),
    ("2100", "長期借入金",      "liability", "固定負債", 1000),
    # ─── 資本 ─────────────────────────────────
    ("3001", "元入金",          "equity", "資本", 100),
    ("3002", "事業主貸",        "equity", "資本", 200),
    ("3003", "事業主借",        "equity", "資本", 210),
    ("3010", "青色申告特別控除前所得金額", "equity", "資本", 300),
    # ─── 収益 ─────────────────────────────────
    ("4001", "売上高",          "revenue", "売上", 100),
    ("4002", "雑収入",          "revenue", "営業外", 200),
    ("4010", "受取利息",        "revenue", "営業外", 210),
    ("4011", "受取配当金",      "revenue", "営業外", 220),
    # 不動産所得
    ("4100", "不動産収入",      "revenue", "不動産", 1000),
    ("4101", "礼金・更新料",    "revenue", "不動産", 1010),
    # ─── 費用 (売上原価) ──────────────────────
    ("5001", "仕入高",                  "expense", "売上原価", 100),
    ("5002", "期首商品棚卸高",          "expense", "売上原価", 110),
    ("5003", "期末商品棚卸高",          "expense", "売上原価", 120),
    # ─── 費用 (経費 — 青色申告決算書 順) ────
    ("6001", "租税公課",                "expense", "経費", 100),
    ("6002", "荷造運賃",                "expense", "経費", 110),
    ("6003", "水道光熱費",              "expense", "経費", 120),
    ("6004", "旅費交通費",              "expense", "経費", 130),
    ("6005", "通信費",                  "expense", "経費", 140),
    ("6006", "広告宣伝費",              "expense", "経費", 150),
    ("6007", "接待交際費",              "expense", "経費", 160),
    ("6008", "損害保険料",              "expense", "経費", 170),
    ("6009", "修繕費",                  "expense", "経費", 180),
    ("6010", "消耗品費",                "expense", "経費", 190),
    ("6011", "減価償却費",              "expense", "経費", 200),
    ("6012", "福利厚生費",              "expense", "経費", 210),
    ("6013", "給料賃金",                "expense", "経費", 220),
    ("6014", "外注工賃",                "expense", "経費", 230),
    ("6015", "利子割引料",              "expense", "経費", 240),
    ("6016", "地代家賃",                "expense", "経費", 250),
    ("6017", "貸倒金",                  "expense", "経費", 260),
    ("6018", "専従者給与",              "expense", "経費", 270),
    # 任意科目 (空欄に手書きする想定の追加経費)
    ("7001", "支払手数料",              "expense", "経費", 300),
    ("7002", "新聞図書費",              "expense", "経費", 310),
    ("7003", "諸会費",                  "expense", "経費", 320),
    ("7004", "研修費",                  "expense", "経費", 330),
    ("7005", "事務用品費",              "expense", "経費", 340),
    ("7006", "車両費",                  "expense", "経費", 350),
    ("7007", "管理費",                  "expense", "経費", 360),
    ("7008", "雑費",                    "expense", "経費", 999),
]


def seed_builtin_accounts(con: sqlite3.Connection) -> int:
    """builtin 科目を冪等に upsert。既存 code は name/type/category/sort_order を上書き、
    ユーザ追加 (is_builtin=0) は触らない。is_active はユーザ編集を尊重する。
    """
    inserted = 0
    for code, name, typ, cat, order in _BUILTIN:
        cur = con.execute("SELECT 1 FROM aoiro_accounts WHERE code=?", (code,))
        if cur.fetchone():
            con.execute(
                "UPDATE aoiro_accounts SET name=?, type=?, category=?, sort_order=?, is_builtin=1 "
                "WHERE code=? AND is_builtin=1",
                (name, typ, cat, order, code),
            )
        else:
            con.execute(
                "INSERT INTO aoiro_accounts (code, name, type, category, sort_order, is_builtin, is_active) "
                "VALUES (?,?,?,?,?,1,1)",
                (code, name, typ, cat, order),
            )
            inserted += 1
    con.commit()
    return inserted


def list_accounts(con: sqlite3.Connection, type_: str | None = None,
                  active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM aoiro_accounts"
    args: list = []
    where = []
    if type_:
        where.append("type = ?")
        args.append(type_)
    if active_only:
        where.append("is_active = 1")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY sort_order, code"
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def upsert_account(con: sqlite3.Connection, code: str, name: str, type_: str,
                   category: str = "", sort_order: int = 0, note: str = "",
                   is_active: bool = True) -> None:
    if type_ not in ("asset", "liability", "equity", "revenue", "expense"):
        raise ValueError(f"invalid type: {type_}")
    cur = con.execute("SELECT is_builtin FROM aoiro_accounts WHERE code=?", (code,))
    row = cur.fetchone()
    if row is None:
        con.execute(
            "INSERT INTO aoiro_accounts (code,name,type,category,sort_order,is_builtin,is_active,note) "
            "VALUES (?,?,?,?,?,0,?,?)",
            (code, name, type_, category, sort_order, 1 if is_active else 0, note),
        )
    else:
        # builtin の name/type/category/sort_order は seed 経由でしか変えない
        if row["is_builtin"]:
            con.execute(
                "UPDATE aoiro_accounts SET is_active=?, note=? WHERE code=?",
                (1 if is_active else 0, note, code),
            )
        else:
            con.execute(
                "UPDATE aoiro_accounts SET name=?, type=?, category=?, sort_order=?, "
                "is_active=?, note=? WHERE code=?",
                (name, type_, category, sort_order, 1 if is_active else 0, note, code),
            )
    con.commit()


def delete_account(con: sqlite3.Connection, code: str) -> bool:
    """ユーザ追加科目のみ削除可能。builtin は False を返す。"""
    cur = con.execute("SELECT is_builtin FROM aoiro_accounts WHERE code=?", (code,))
    row = cur.fetchone()
    if row is None or row["is_builtin"]:
        return False
    con.execute("DELETE FROM aoiro_accounts WHERE code=?", (code,))
    con.commit()
    return True


def init_with_seed() -> sqlite3.Connection:
    """schema → seed の起動シーケンス (科目 + ルール + 支払いマップ)。"""
    from src.aoiro.rules import seed_builtin_payments, seed_builtin_rules
    con = open_db()
    ensure_aoiro_schema(con)
    seed_builtin_accounts(con)
    seed_builtin_rules(con)
    seed_builtin_payments(con)
    return con
