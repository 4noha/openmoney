"""サービスごとの生データ閲覧 API + /raw ページ。

- 各プラグインが provided_banks に宣言した bank 名で transactions を抽出
- 補助テーブル (amazon_order_details/items 等) があれば一緒に返す
- フィルタ・マッチング・タグ等を通さない素のデータを見るための画面
"""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import HTMLResponse

from src.server import app, _tx_db
from src.plugins_loader import get_registry


_TEMPLATES = Path(__file__).parent / "templates"

# bank → 補助テーブル名 (LIKE クエリで紐付け列)
# {table_name: {key_col, where_col?, where_val?}}
AUX_TABLES: dict[str, list[dict]] = {
    "Amazon": [
        {"table": "amazon_order_details", "join_col": "order_id"},
        {"table": "amazon_order_items", "join_col": "order_id"},
    ],
}


@app.get("/raw", response_class=HTMLResponse)
async def raw_page():
    from src.server.templates_helper import render
    return render("raw.html")


@app.get("/api/raw/services")
async def api_raw_services():
    """利用可能なサービス一覧 (provided_banks ごとの行数付き)。"""
    reg = get_registry()
    con = _tx_db()
    out = []
    for name, spec in sorted(reg.items()):
        if not spec.provided_banks:
            continue
        placeholders = ",".join("?" * len(spec.provided_banks))
        row = con.execute(
            f"SELECT COUNT(*), MAX(date), MIN(date) FROM transactions "
            f"WHERE bank IN ({placeholders})",
            spec.provided_banks,
        ).fetchone()
        out.append({
            "name": spec.name,
            "display_name": spec.display_name,
            "banks": list(spec.provided_banks),
            "row_count": row[0] or 0,
            "latest": row[1],
            "oldest": row[2],
            "history_url": spec.history_url,
            "detail_url_template": spec.detail_url_template,
        })
    con.close()
    # 登録されていないが transactions に存在する bank も補完
    con = _tx_db()
    known = {b for s in out for b in s["banks"]}
    rows = con.execute(
        "SELECT bank, COUNT(*), MAX(date), MIN(date) FROM transactions GROUP BY bank"
    ).fetchall()
    for r in rows:
        if r[0] in known:
            continue
        out.append({
            "name": r[0],
            "display_name": r[0],
            "banks": [r[0]],
            "row_count": r[1] or 0,
            "latest": r[2],
            "oldest": r[3],
            "orphan": True,
        })
    con.close()
    return {"services": out}


@app.get("/api/raw/transactions")
async def api_raw_transactions(
    bank: str = "",
    limit: int = 200,
    offset: int = 0,
):
    """指定 bank の transactions を limit/offset で返す。
    各行に order_id (description の '[oid]' or '[oid/N]' から抽出) と
    detail_url (該当プラグインのテンプレで展開) を追加して返す。"""
    if not bank:
        raise HTTPException(status_code=400, detail="bank required")
    banks = [b.strip() for b in bank.split(",") if b.strip()]
    placeholders = ",".join("?" * len(banks))
    con = _tx_db()
    con.row_factory = __import__("sqlite3").Row
    total = con.execute(
        f"SELECT COUNT(*) FROM transactions WHERE bank IN ({placeholders})", banks
    ).fetchone()[0]
    rows = con.execute(
        f"SELECT * FROM transactions WHERE bank IN ({placeholders}) "
        f"ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
        (*banks, limit, offset),
    ).fetchall()
    con.close()
    cols = list(rows[0].keys()) if rows else []

    # bank → detail_url_template の解決
    import re
    reg = get_registry()
    bank_template: dict[str, str] = {}
    for spec in reg.values():
        for b in spec.provided_banks:
            if spec.detail_url_template:
                bank_template[b] = spec.detail_url_template

    out_rows = []
    for r in rows:
        d = dict(r)
        m = re.match(r"^\[([^/\]]+)", d.get("description") or "")
        oid = m.group(1) if m else None
        d["order_id"] = oid
        tpl = bank_template.get(d.get("bank", ""), "")
        d["detail_url"] = tpl.format(order_id=oid) if (tpl and oid) else None
        out_rows.append(d)

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "columns": [*cols, "order_id", "detail_url"],
        "rows": out_rows,
    }


@app.get("/api/raw/aux/{table}")
async def api_raw_aux_table(table: str, order_id: str = "", limit: int = 100):
    """補助テーブルの内容。order_id が指定されれば絞り込む。"""
    # SQL injection 対策: テーブル名はホワイトリスト
    allowed = {
        "amazon_order_details", "amazon_order_items",
        "shop_card_matches", "card_bank_matches",
        "amazon_order_card_matches", "invoice_receipts",
        "receipts", "receipt_links",
    }
    if table not in allowed:
        raise HTTPException(status_code=400, detail="unknown table")
    con = _tx_db()
    con.row_factory = __import__("sqlite3").Row
    sql = f"SELECT * FROM {table}"
    params: list = []
    if order_id and "order_id" in [r[1] for r in con.execute(f"PRAGMA table_info({table})")]:
        sql += " WHERE order_id = ?"
        params.append(order_id)
    sql += f" LIMIT {limit}"
    rows = con.execute(sql, params).fetchall()
    cols = list(rows[0].keys()) if rows else []
    con.close()
    return {"columns": cols, "rows": [dict(r) for r in rows]}
