"""データ整合性ヘルスチェック。"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.server import app, _tx_db


@app.get("/api/health")
async def api_health():
    """各種データ整合性チェックの結果を返す。
    各チェック項目は count（異常件数）と sample（先頭5件）を含む。
    すべて 0 なら問題なし。
    """
    con = _tx_db()
    out: dict = {}

    # 1. 親 ≠ サブ和（split注文の合計不整合）
    # Amazon の points_used / gift_card / discount で sub_sum > parent.debit に
    # なるのは想定内（cash でない支払分・割引が parent.debit に反映済みのため）。
    rows = con.execute("""
        SELECT t.id, t.bank, t.debit, t.description,
               (SELECT SUM(s.debit) FROM transactions s
                WHERE s.bank=t.bank
                  AND s.description LIKE
                      ('[' || substr(t.description, 2, instr(t.description,']')-2) || '/%')) AS sub_sum,
               od.points_used, od.gift_card, od.discount, od.shipping
        FROM transactions t
        LEFT JOIN amazon_order_details od
          ON t.bank='Amazon'
          AND substr(t.description, 2, instr(t.description,']')-2) = od.order_id
        WHERE t.bank IN ('Amazon','Yahoo!ショッピング')
          AND t.description LIKE '[%]%' AND t.description NOT LIKE '[%/%]%'
    """).fetchall()
    bad = []
    for r in rows:
        if r["sub_sum"] is None or r["sub_sum"] == r["debit"]:
            continue
        # parent.debit (cash) + P + GC + disc - ship = sub_sum (items 合計) が
        # 成立すれば正当。shipping は cash に上乗せされるので符号反転で引く。
        diff = r["sub_sum"] - r["debit"]
        adjustment = (
            (r["points_used"] or 0)
            + (r["gift_card"] or 0)
            + (r["discount"] or 0)
            - (r["shipping"] or 0)
        )
        if abs(diff - adjustment) <= 5:
            continue
        bad.append(r)
    out["split_total_mismatch"] = {
        "count": len(bad),
        "sample": [
            {"tx_id": r["id"], "bank": r["bank"],
             "parent": r["debit"], "subs": r["sub_sum"],
             "description": r["description"][:60]}
            for r in bad[:5]
        ],
    }

    # 2. 古い items_fetched=0（最終取得試行 fetched_at から1週間経過しても
    # まだ取れていない＝諦め時の order）。fetched_at が NULL のものは
    # order_date を採用。最近 reset したものは fetched_at が更新されないので
    # 「週次リトライ前」として除外される。
    cutoff_iso = (datetime.now() - timedelta(days=7)).isoformat()
    cutoff_date = (datetime.now() - timedelta(days=7)).strftime("%Y/%m/%d")
    stale = con.execute(
        "SELECT order_id, order_date FROM amazon_order_details "
        "WHERE items_fetched = 0 "
        "  AND ((fetched_at IS NOT NULL AND fetched_at < ?) "
        "       OR (fetched_at IS NULL AND order_date < ?)) "
        "ORDER BY order_date DESC LIMIT 50",
        (cutoff_iso, cutoff_date),
    ).fetchall()
    out["amazon_items_unfetched_old"] = {
        "count": len(stale),
        "sample": [{"order_id": r["order_id"], "order_date": r["order_date"]} for r in stale[:5]],
    }

    # 3. 紙レシート 未分類
    uncat = con.execute(
        "SELECT id, date, description, debit FROM transactions "
        "WHERE bank='レシート' AND debit > 0 AND (category IS NULL OR category='') "
        "ORDER BY date DESC LIMIT 50"
    ).fetchall()
    out["receipts_uncategorized"] = {
        "count": len(uncat),
        "sample": [
            {"tx_id": r["id"], "date": r["date"], "amount": r["debit"],
             "description": r["description"][:60]}
            for r in uncat[:5]
        ],
    }

    # 4. orphan receipt_links（参照先 transaction が消えた）
    orphan = con.execute("""
        SELECT rl.receipt_id, rl.transaction_id FROM receipt_links rl
        LEFT JOIN transactions t ON t.id = rl.transaction_id
        WHERE t.id IS NULL
    """).fetchall()
    out["orphan_receipt_links"] = {
        "count": len(orphan),
        "sample": [{"receipt_id": r["receipt_id"], "transaction_id": r["transaction_id"]}
                   for r in orphan[:5]],
    }

    # 5. shop_card_matches ghost 監視は #19 Phase 5 で legacy DROP につき廃止

    # 6. 突合テーブルの dangling card_tx_id（参照先が削除）
    dangling_card = con.execute("""
        SELECT ir.id, ir.bank, ir.order_id, ir.card_tx_id
        FROM invoice_receipts ir
        LEFT JOIN transactions t ON t.id = ir.card_tx_id
        WHERE ir.card_tx_id IS NOT NULL AND t.id IS NULL
    """).fetchall()
    out["dangling_invoice_card_tx"] = {
        "count": len(dangling_card),
        "sample": [
            {"id": r["id"], "bank": r["bank"], "order_id": r["order_id"],
             "card_tx_id": r["card_tx_id"]}
            for r in dangling_card[:5]
        ],
    }

    # 7b. tax_expense_items の orphan source_tx (transactions 削除済み)
    tax_orphan = con.execute("""
        SELECT i.id, i.amount, i.source_tx_id, i.invoice_number
        FROM tax_expense_items i
        LEFT JOIN transactions t ON t.id = i.source_tx_id
        WHERE i.source_tx_id IS NOT NULL AND t.id IS NULL
    """).fetchall()
    out["tax_orphan_source_tx"] = {
        "count": len(tax_orphan),
        "sample": [
            {"item_id": r["id"], "amount": r["amount"],
             "source_tx_id": r["source_tx_id"], "invoice_number": r["invoice_number"] or ""}
            for r in tax_orphan[:5]
        ],
    }

    # 7c. tax_expense_items の source.category が経費系でない (出金・個人支出 等)
    tax_non_exp = con.execute("""
        SELECT i.id, i.amount, t.category, t.bank, t.description
        FROM tax_expense_items i
        JOIN transactions t ON t.id = i.source_tx_id
        WHERE t.category NOT IN ('経費', '今回は経費')
    """).fetchall()
    out["tax_source_not_expense"] = {
        "count": len(tax_non_exp),
        "sample": [
            {"item_id": r["id"], "amount": r["amount"], "category": r["category"] or "",
             "bank": r["bank"], "description": (r["description"] or "")[:60]}
            for r in tax_non_exp[:5]
        ],
    }

    # 8. tax_expense_items.amount が source_tx.debit と乖離
    tax_drift = con.execute("""
        SELECT i.id, i.amount, t.debit, t.description
        FROM tax_expense_items i
        JOIN transactions t ON t.id = i.source_tx_id
        WHERE i.amount != t.debit
    """).fetchall()
    out["tax_amount_drift"] = {
        "count": len(tax_drift),
        "sample": [
            {"item_id": r["id"], "item_amount": r["amount"], "tx_debit": r["debit"],
             "description": r["description"][:60]}
            for r in tax_drift[:5]
        ],
    }

    # 9. 親≠子サブ和（5以下なら send 完璧、ただし amazon_items_unfetched_old と相関）
    # 既に #1 で見ているのでスキップ

    # 9.5 tx_links 統一表 (#19 Phase 5 完了) — legacy テーブル DROP 済なので drift 監視不要
    # 代わりに tx_links 自体の参照整合性を監視する。SQLite は FK を default 強制
    # しないので、transaction 削除や手動編集で残骸が出る可能性がある。
    orphans = con.execute("""
        SELECT id, tx_a_id, tx_b_id, link_type FROM tx_links
        WHERE (tx_a_id IS NOT NULL AND tx_a_id NOT IN (SELECT id FROM transactions))
           OR (tx_b_id IS NOT NULL AND tx_b_id NOT IN (SELECT id FROM transactions))
           OR (tx_a_id IS NULL AND tx_b_id IS NULL)
        ORDER BY id LIMIT 50
    """).fetchall()
    out["tx_links_orphans"] = {
        "count": len(orphans),
        "sample": [
            {"id": r["id"], "tx_a_id": r["tx_a_id"], "tx_b_id": r["tx_b_id"],
             "link_type": r["link_type"]}
            for r in orphans[:5]
        ],
    }

    # 10. データの最新性
    # last_data_date: 取引最新日付（データ自体の鮮度）
    # last_scrape_at: スクレイパー最終実行時刻（scrape_state.mark_scrape_done で更新）
    # 「データ最新は90日前でも昨日スクレイプ済」のような状況を区別する
    fresh_rows = con.execute("""
        SELECT bank, MAX(date) latest, COUNT(*) total
        FROM transactions GROUP BY bank
    """).fetchall()
    scrape_states = con.execute(
        "SELECT key, value FROM daemon_state WHERE key LIKE 'scrape:%:last_done_at'"
    ).fetchall()
    last_scrape: dict[str, str] = {}
    for r in scrape_states:
        # key: scrape:<name>:last_done_at
        parts = r["key"].split(":", 2)
        if len(parts) == 3:
            last_scrape[parts[1]] = r["value"]

    # 銀行名 → scraper 名マッピング（多くは小文字化で一致）
    bank_to_scraper = {
        "Amazon": "amazon", "AmazonPay": "amazon",
        "VPASS": "vpass", "MUFG": "mufg",
        "メルカード": "mercari", "Mercari": "mercari",
        "Mercari売上": "mercari", "Makuake": "makuake",
        "CAMPFIRE": "campfire", "楽天市場": "rakuten",
        "PayPal": "paypal",
        "AliExpress": "aliexpress",
    }

    today = datetime.now().date()
    freshness = []
    for r in fresh_rows:
        scraper = bank_to_scraper.get(r["bank"])
        last_scrape_iso = last_scrape.get(scraper) if scraper else None
        scrape_days_ago: int | None = None
        if last_scrape_iso:
            try:
                ts = datetime.fromisoformat(last_scrape_iso)
                scrape_days_ago = (today - ts.date()).days
            except (ValueError, TypeError):
                pass
        try:
            latest = datetime.strptime(r["latest"], "%Y/%m/%d").date()
            data_days_ago = (today - latest).days
        except (ValueError, TypeError):
            data_days_ago = None
        freshness.append({
            "bank": r["bank"],
            "latest_data": r["latest"],
            "data_days_ago": data_days_ago,
            "last_scrape_at": last_scrape_iso,
            "scrape_days_ago": scrape_days_ago,
            "total": r["total"],
        })
    # スクレイプが古いものを抜粋（実行されてない/失敗してるサイン）
    freshness.sort(key=lambda x: -(x["scrape_days_ago"] or 9999))
    stale_scrape = [
        f for f in freshness
        if (f["scrape_days_ago"] is None or f["scrape_days_ago"] > 14)
        and f["bank"] not in ("レシート",)
    ]
    out["data_freshness"] = {
        "stale_scrape_14d": stale_scrape,
        "all": freshness,
    }

    con.close()
    out["ok"] = all(v["count"] == 0 for v in out.values() if isinstance(v, dict) and "count" in v)
    return out
