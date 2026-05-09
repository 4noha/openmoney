"""取引一覧・サマリー・カテゴリ更新の API ルート群。"""
from __future__ import annotations

import threading

from fastapi import HTTPException
from pydantic import BaseModel

from src.server import (
    app,
    _tx_db,
    _fetch_tx_rows,
    _summary_from_rows,
    _invalidate_tx_cache,
    _update_cache_row_categories,
    _update_cache_conflict_for_keys,
    _refresh_all_conflict_tags,
    refresh_categorization,
)
from src.server.constants import CATEGORIES


# カテゴリ POST 後の再分類を debounce する（連続書込中はタイマーリセット）
_refresh_timer: threading.Timer | None = None
_refresh_lock = threading.Lock()


def _schedule_refresh_categorization(delay: float = 5.0) -> None:
    global _refresh_timer
    with _refresh_lock:
        if _refresh_timer is not None:
            _refresh_timer.cancel()
        _refresh_timer = threading.Timer(delay, _do_refresh)
        _refresh_timer.daemon = True
        _refresh_timer.start()


def _do_refresh() -> None:
    global _refresh_timer
    try:
        refresh_categorization()
    finally:
        with _refresh_lock:
            _refresh_timer = None


class CategoryBody(BaseModel):
    category: str


@app.get("/api/years")
async def api_years():
    con = _tx_db()
    rows = con.execute(
        "SELECT DISTINCT substr(date,1,4) as year FROM transactions WHERE debit>0 ORDER BY year DESC"
    ).fetchall()
    con.close()
    return [r["year"] for r in rows]


def _maybe_refresh_conflict(con, tags: str, exclude_tags: str) -> None:
    """tags フィルタに conflict が含まれていたら conflict タグを最新化する。
    cache を全消ししないので軽量 (SQL 1 発 + cache row 巡回 ~10ms)。"""
    if "conflict" in tags.split(",") or "conflict" in exclude_tags.split(","):
        _refresh_all_conflict_tags(con)


@app.get("/api/summary")
async def api_summary(year: str = "", merge: bool = False, tags: str = "", exclude_tags: str = "", income: bool = False):
    con = _tx_db()
    _maybe_refresh_conflict(con, tags, exclude_tags)
    rows = _fetch_tx_rows(con, year, "__all__", merge, tags, exclude_tags, income)
    con.close()
    return _summary_from_rows(rows, income)


@app.get("/api/transactions")
async def api_transactions(year: str = "", category: str = "__all__",
                           merge: bool = False, tags: str = "", exclude_tags: str = "", income: bool = False):
    con = _tx_db()
    _maybe_refresh_conflict(con, tags, exclude_tags)
    rows = _fetch_tx_rows(con, year, category, merge, tags, exclude_tags, income)
    con.close()
    return rows


@app.get("/api/no-receipt/vendors")
async def api_no_receipt_vendors(year: str = ""):
    """レシート未紐付けの「経費」 「今回は経費」 取引を (bank, description_normalized)
    で集計し、 頻出 (件数 2 件以上) の vendor 候補を返す。

    各 vendor について既存 invoice_vendors との一致を解決し、 適格事業者として
    未登録 (= matched_invoice が空) のものを優先的に表示することで、 ユーザが
    「この vendor が頻出 → 適格事業者登録すれば仕入税額控除できる」 という気付きに
    つなげる。 集計対象は category='経費' or '今回は経費'。
    """
    from src.server.constants import SITE_BANKS as _SITE_BANKS
    con = _tx_db()
    site_placeholders = ",".join("?" * len(_SITE_BANKS))
    where = [
        "t.category IN ('経費', '今回は経費')",
        "(t.description_normalized IS NOT NULL AND t.description_normalized != '')",
        "NOT EXISTS (SELECT 1 FROM receipt_links WHERE transaction_id = t.id)",
        # ショッピングサイト明細は注文番号で領収書 PDF を取得可能なのでレシートあり扱い
        f"t.bank NOT IN ({site_placeholders})",
        # 為替手数料 / 振込手数料 は親振込に category 追従する仕様 (= conflict
        # タグ判定でも除外している、 fee_inherit_prev ルール)。 メタ付与/集計
        # の対象としてもノイズなので頻出 vendor から除外する。
        "t.description_normalized NOT LIKE '%為替手数料%'",
        "t.description_normalized NOT LIKE '%振込手数料%'",
        "t.description_normalized NOT LIKE '%テスウリヨウ%'",
    ]
    params: list = list(_SITE_BANKS)
    if year:
        where.append("substr(t.date, 1, 4) = ?")
        params.append(year)
    sql = f"""
        SELECT
          t.bank,
          t.description_normalized AS desc_norm,
          COUNT(*) AS cnt,
          SUM(t.debit) AS total,
          MIN(t.date) AS earliest,
          MAX(t.date) AS latest,
          (SELECT iv.invoice_number FROM invoice_vendors iv
           WHERE iv.service_name = t.description_normalized AND iv.invoice_number != ''
           LIMIT 1) AS matched_invoice,
          (SELECT iv.id FROM invoice_vendors iv
           WHERE iv.service_name = t.description_normalized
           LIMIT 1) AS vendor_id,
          -- 代表 tx_id (= /ui からメタレシート登録 (1:N 適用) する起点)。 最新日付を採用。
          (SELECT MAX(t2.id) FROM transactions t2
           WHERE t2.bank = t.bank AND t2.description_normalized = t.description_normalized
             AND t2.category IN ('経費', '今回は経費')) AS sample_tx_id
        FROM transactions t
        WHERE {' AND '.join(where)}
        GROUP BY t.bank, t.description_normalized
        HAVING cnt >= 2
        ORDER BY cnt DESC, total DESC
    """
    rows = con.execute(sql, params).fetchall()
    con.close()
    return [
        {
            "bank": r[0],
            "description_normalized": r[1],
            "count": r[2],
            "total": r[3] or 0,
            "earliest": r[4],
            "latest": r[5],
            "matched_invoice": r[6] or "",
            "vendor_id": r[7],
            "sample_tx_id": r[8],
        }
        for r in rows
    ]


@app.post("/api/auto-classify-from-history")
async def api_auto_classify_from_history():
    """過去の手動分類から (bank, description) → category ルールを抽出して
    未分類行に自動適用する。経費 / 個人支出 / 出金 のみ学習対象。"""
    from src.server import _tx_db, _auto_categorize_from_history
    con = _tx_db()
    _auto_categorize_from_history(con)
    con.close()
    return {"status": "ok"}


def _affected_conflict_keys(con, tx_ids: list[int]) -> set[tuple[str, str]]:
    """指定 tx 群の (bank, description_normalized) を集めて返す (= conflict 再計算対象)。"""
    if not tx_ids:
        return set()
    placeholders = ",".join("?" * len(tx_ids))
    rows = con.execute(
        f"SELECT DISTINCT bank, description_normalized FROM transactions "
        f"WHERE id IN ({placeholders}) "
        f"  AND description_normalized IS NOT NULL AND description_normalized != ''",
        tx_ids,
    ).fetchall()
    return {(r[0], r[1]) for r in rows}


_EXPENSE_CATS_FOR_TAX = ("経費", "今回は経費")


def _sync_tax_expense_items(con, tx_ids: list[int], new_category: str) -> int:
    """tx の category が「経費」 / 「今回は経費」 から外れたら、
    対応する tax_expense_items 行を自動削除する。

    過去にユーザが手動で「経費 → 個人支出」 等に直しても tax_expense_items に
    経費時代のエントリが残り、 health check の tax_source_not_expense 警告が
    drift として残る事象があった。 category 変更時にコード側で恒久同期する。
    """
    if not tx_ids or new_category in _EXPENSE_CATS_FOR_TAX:
        return 0
    placeholders = ",".join("?" * len(tx_ids))
    cur = con.execute(
        f"DELETE FROM tax_expense_items WHERE source_tx_id IN ({placeholders})",
        tx_ids,
    )
    return cur.rowcount


def _check_category_lock(con, tx_ids: list[int]) -> None:
    """tx_meta.lock_source が入っている tx は category 変更を拒否 (= ハードロック)。

    例: povo plugin の POVO_LOCK_TOPPING_PERSONAL=ON は VPASS のトッピング行を
    「今回は個人支出」 で固定する。 ユーザが UI から動かそうとしても 423 で拒否。
    解除は plugin のトグル OFF で行う (= /settings)。
    """
    if not tx_ids:
        return
    placeholders = ",".join("?" * len(tx_ids))
    locked = con.execute(
        f"SELECT transaction_id, value FROM tx_meta "
        f"WHERE key='lock_source' AND transaction_id IN ({placeholders})",
        list(tx_ids),
    ).fetchall()
    if not locked:
        return
    sources = sorted({r[1] for r in locked})
    raise HTTPException(
        status_code=423,
        detail={
            "locked_tx_ids": [r[0] for r in locked],
            "lock_sources": sources,
            "message": (
                f"{', '.join(sources)} plugin がカテゴリをロック中。 解除は "
                f"/settings の plugin トグルで行ってください"
            ),
        },
    )


@app.post("/api/transactions/{tx_id}/category")
async def api_set_category(tx_id: int, body: CategoryBody):
    if body.category not in CATEGORIES:
        raise HTTPException(status_code=400, detail="invalid category")
    con = _tx_db()
    _check_category_lock(con, [tx_id])
    keys = _affected_conflict_keys(con, [tx_id])
    con.execute("UPDATE transactions SET category=? WHERE id=?", (body.category, tx_id))
    # 経費系から外れた場合は tax_expense_items も自動削除 (drift 防止)
    _sync_tax_expense_items(con, [tx_id], body.category)
    con.commit()
    # キャッシュ全消し（→ cold rebuild ~6s）はやらず、該当行のみ in-place 更新。
    # auto-categorize の再分類は debounced で 5s 後にバックグラウンド実行。
    _update_cache_row_categories({tx_id: body.category})
    # conflict タグも該当 desc グループだけ即時再計算 (cache 全消し回避)
    _update_cache_conflict_for_keys(con, keys)
    con.close()
    _schedule_refresh_categorization()
    return {"status": "ok"}


@app.post("/api/transactions/bulk-category")
async def api_bulk_category(body: dict):
    ids = body.get("ids", [])
    category = body.get("category", "")
    if category not in CATEGORIES:
        raise HTTPException(status_code=400, detail="invalid category")
    if not ids:
        return {"updated": 0}
    con = _tx_db()
    _check_category_lock(con, list(ids))
    keys = _affected_conflict_keys(con, ids)
    placeholders = ",".join("?" * len(ids))
    con.execute(
        f"UPDATE transactions SET category=? WHERE id IN ({placeholders})",
        [category, *ids],
    )
    updated = con.total_changes
    _sync_tax_expense_items(con, ids, category)
    con.commit()
    # 一括更新もキャッシュは消さず in-place で書き換える（cold rebuild 回避）
    _update_cache_row_categories({tid: category for tid in ids})
    _update_cache_conflict_for_keys(con, keys)
    con.close()
    _schedule_refresh_categorization()
    return {"updated": updated}


@app.post("/api/cache/refresh")
async def api_cache_refresh():
    _invalidate_tx_cache()
    return {"ok": True}
