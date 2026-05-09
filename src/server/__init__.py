"""
FastAPI サーバー: Android から FCM token 登録と Accept を受け取る
"""
import asyncio
import os
import re
import sqlite3
import time as _time
import unicodedata
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

load_dotenv()

_ROOT = Path(__file__).parent.parent.parent
_CFG: dict = {}
_cfg_path = _ROOT / "config.json"
if _cfg_path.exists():
    import json as _json
    _CFG = _json.loads(_cfg_path.read_text())

DB_PATH = _ROOT / "transactions.db"
_RECEIPTS_DIR = _ROOT / "invoices" / "receipts"
_AMAZON_INVOICE_DIR         = _ROOT / "invoices" / "amazon"
_MERCARI_INVOICE_DIR        = _ROOT / "invoices" / "mercari"
_RAKUTEN_INVOICE_DIR        = _ROOT / "invoices" / "rakuten"
_CAMPFIRE_INVOICE_DIR       = _ROOT / "invoices" / "campfire"
_ALIEXPRESS_INVOICE_DIR     = _ROOT / "invoices" / "aliexpress"
_YAHOO_SHOPPING_INVOICE_DIR = _ROOT / "invoices" / "yahoo_shopping"
PORT = int(os.environ.get("DAEMON_PORT") or _CFG.get("DAEMON_PORT", 8765))


# ─────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("""
        CREATE TABLE IF NOT EXISTS daemon_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    con.commit()
    return con


def _set(key: str, value: str) -> None:
    con = _db()
    con.execute("INSERT OR REPLACE INTO daemon_state(key, value) VALUES (?, ?)", (key, value))
    con.commit()
    con.close()


def _get(key: str) -> str | None:
    con = _db()
    row = con.execute("SELECT value FROM daemon_state WHERE key=?", (key,)).fetchone()
    con.close()
    return row[0] if row else None


# ─────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────

async def _cache_refresh_loop() -> None:
    """起動後すぐに初回キャッシュ構築し、以後 _TX_CACHE_TTL 秒ごとに再構築する。"""
    import threading
    while True:
        threading.Thread(target=_rebuild_tx_cache, daemon=True).start()
        await asyncio.sleep(_TX_CACHE_TTL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[server] 起動 port={PORT}")
    # WAL モードを最初に設定（isolation_level=None で暗黙トランザクションを避ける）
    _wal_con = sqlite3.connect(DB_PATH, isolation_level=None)
    _wal_con.execute("PRAGMA journal_mode=WAL")
    _wal_con.close()
    # マイグレーションと書き込み系処理をキャッシュ構築スレッドより先に完了させる
    _sync_invoice_master_at_startup()
    refresh_categorization()
    task = asyncio.create_task(_cache_refresh_loop())
    yield
    task.cancel()
    print("[server] 停止")


def _sync_invoice_master_at_startup() -> None:
    """invoice_vendors テーブルに config/invoice_master.toml + .local.toml を
    取り込む (#35)。INSERT OR IGNORE 相当 — 既存編集は保護。"""
    try:
        from src.personal.invoice_master import sync_invoice_vendors
        # invoice_vendors テーブルは routes_tax._tax_db() の呼出で初期化される。
        # ここでは独立に同じテーブルを参照する接続を開く (CREATE は idempotent)。
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.execute(
            "CREATE TABLE IF NOT EXISTS invoice_vendors ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  service_name TEXT NOT NULL DEFAULT '',"
            "  company_name TEXT NOT NULL DEFAULT '',"
            "  invoice_number TEXT NOT NULL DEFAULT '')"
        )
        con.commit()
        n = sync_invoice_vendors(con)
        if n:
            print(f"[invoice_master] {n} 件の vendor を新規取込")
        con.close()
    except Exception as e:
        print(f"[invoice_master] 取込失敗 (server 続行): {e}")


def _auto_link_receipts_by_amount_date(con) -> int:
    """実物レシート (filename あり) を金額 + 日付近接 (±60 日) で 経費 tx に自動 link。

    レシート OCR で抽出した amount + receipt_date が、 銀行/カード明細の
    debit + date と一致するなら同一取引と判定して receipt_links を張る。
    例: 草津市水道お客様センター ¥6,175 (検針 5/01) ↔ MUFG 引落 ¥6,175 (8/04) を
    自動で紐付け。

    既存 link は INSERT OR IGNORE で保護。 また、 既にどこかに link 済みの receipt は
    スキップ (= 1 receipt は最初に link した 1 tx のみ)。 これにより同金額の他 tx に
    過剰マッチするのを防ぐ。
    """
    # upload-receipt は receipt と同時に bank='レシート' の独立 tx を作って既に link
    # 済みなので、 未 link 条件ではなく「レシート以外の bank の tx に link 済み」 で判定。
    # 1 receipt → (レシート tx + 元経費 tx) の 1:多 link を許容する。
    cur = con.execute(
        """
        INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id)
        SELECT DISTINCT r.id, t.id
        FROM receipts r
        JOIN transactions t
          ON t.debit = r.amount
         AND ABS(julianday(replace(t.date,'/','-')) - julianday(replace(r.receipt_date,'/','-'))) <= 60
        WHERE r.amount IS NOT NULL AND r.amount > 0
          AND r.receipt_date IS NOT NULL AND r.receipt_date != ''
          AND r.filename IS NOT NULL AND r.filename != ''
          AND t.category IN ('経費', '今回は経費')
          AND t.bank != 'レシート'
          AND NOT EXISTS (
            SELECT 1 FROM receipt_links rl
            JOIN transactions t2 ON t2.id = rl.transaction_id
            WHERE rl.receipt_id = r.id AND t2.bank != 'レシート'
          )
        """
    )
    n = cur.rowcount or 0
    if n:
        con.commit()
    return n


def _auto_fill_receipt_meta_from_siblings(con) -> int:
    """OCR 失敗で merchant/invoice_number が空の receipt を、 同 (元 tx の bank,
    description_normalized) グループ内の他 receipt から自動補完する。

    例: 草津市水道の receipt 5 枚のうち 4 枚は OCR で merchant + invoice_number が
    取れたが 1 枚 (= id=16) は失敗した場合、 同じ「水道 <description>」 グループの
    他 receipt から事業者情報を Pull して埋める。 将来 OCR が失敗しても自動的に
    回復するため、 ユーザは 1 度どこかの receipt に正しい情報が入っていれば
    手動修正は不要になる。

    補完対象: merchant が空 or filename そのまま (= "receipt_*.jpg" 形式) の receipt、
    または invoice_number が空の receipt。
    """
    from collections import defaultdict
    rows = con.execute(
        """
        SELECT r.id AS receipt_id,
               COALESCE(r.merchant, '') AS merchant,
               COALESCE(r.invoice_number, '') AS invoice_number,
               t.bank, t.description_normalized
        FROM receipts r
        JOIN receipt_links rl ON rl.receipt_id = r.id
        JOIN transactions t ON t.id = rl.transaction_id
        WHERE t.bank != 'レシート'
          AND t.description_normalized IS NOT NULL
          AND t.description_normalized != ''
        """
    ).fetchall()
    by_group: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        key = (r["bank"], r["description_normalized"])
        by_group[key].append(dict(r))

    updated = 0
    for items in by_group.values():
        # group 内で正しい merchant / invoice_number を持つ代表を探す
        good_m = next(
            (it["merchant"] for it in items
             if it["merchant"] and not it["merchant"].startswith("receipt_")),
            None,
        )
        good_i = next(
            (it["invoice_number"] for it in items if it["invoice_number"]),
            None,
        )
        if not good_m and not good_i:
            continue
        # 空 / OCR 失敗の receipt を補完
        seen = set()
        for it in items:
            if it["receipt_id"] in seen:
                continue
            seen.add(it["receipt_id"])
            need_m = (not it["merchant"] or it["merchant"].startswith("receipt_"))
            need_i = not it["invoice_number"]
            if (need_m and good_m) or (need_i and good_i):
                new_m = good_m if (need_m and good_m) else it["merchant"]
                new_i = good_i if (need_i and good_i) else it["invoice_number"]
                con.execute(
                    "UPDATE receipts SET merchant=?, invoice_number=? WHERE id=?",
                    (new_m, new_i, it["receipt_id"]),
                )
                updated += 1
    if updated:
        con.commit()
    return updated


def _normalize_invoice_numbers(con) -> int:
    """receipts と receipt_items の invoice_number を「T + 13 桁数字」 に正規化。

    例: ユーザが「5010001060203」 (= T 省略) で登録した値を「T5010001060203」 に
    自動補完。 ハイフン / 全角文字 / 余分な空白も除去。 不正値 (= 桁数違い等) は
    そのまま残す (= ユーザに気付いてもらう)。
    """
    from src.server.routes_receipts import normalize_invoice_number
    fixed = 0
    for table in ("receipts", "receipt_items", "invoice_vendors"):
        rows = con.execute(
            f"SELECT id, invoice_number FROM {table} "
            "WHERE invoice_number IS NOT NULL AND invoice_number != ''"
        ).fetchall()
        for r in rows:
            old = r["invoice_number"]
            new = normalize_invoice_number(old)
            if new is None or new == old:
                continue  # 不正 / 変更なし
            con.execute(
                f"UPDATE {table} SET invoice_number=? WHERE id=?",
                (new, r["id"]),
            )
            fixed += 1
    if fixed:
        con.commit()
    return fixed


def _consolidate_meta_receipts(con) -> int:
    """同 merchant + 同 transaction_id のメタ receipts を 1 つに集約 (invoice 番号あり優先)。

    同 transaction_id に同 merchant のメタが複数登録されている場合 (= 例: 私が
    /api/.../invoice-meta で作ったメタと、 ユーザが /tax で追加したメタ) のみ集約。
    別 transaction_id (= 別取引) は集約しない (= Discord 12 ヶ月分・ Google Play
    各注文等は個別の取引として保持)。

    安全策: 同 merchant で **異なる invoice_number が両方非空** だと別事業者の
    可能性があるので集約 skip。
    """
    rows = con.execute(
        """
        SELECT r.id AS rid, r.merchant, r.transaction_id,
               COALESCE(r.invoice_number, '') AS invoice_number
        FROM receipts r
        WHERE (r.filename IS NULL OR r.filename = '')
          AND r.merchant IS NOT NULL AND r.merchant != ''
          AND r.transaction_id IS NOT NULL
        """
    ).fetchall()
    from collections import defaultdict
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by_key[(r["merchant"], r["transaction_id"])].append(dict(r))
    deleted = 0
    for items in by_key.values():
        if len(items) <= 1:
            continue
        invs = {it["invoice_number"] for it in items if it["invoice_number"]}
        if len(invs) >= 2:
            # 同 merchant + 同 tx で異なる T 番号 = 安全のため skip
            continue
        items.sort(key=lambda x: (not bool(x["invoice_number"]), x["rid"]))
        keep = items[0]
        for dup in items[1:]:
            con.execute(
                "INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id) "
                "SELECT ?, transaction_id FROM receipt_links WHERE receipt_id=?",
                (keep["rid"], dup["rid"]),
            )
            con.execute("DELETE FROM receipt_links WHERE receipt_id=?", (dup["rid"],))
            con.execute("DELETE FROM receipts WHERE id=?", (dup["rid"],))
            deleted += 1
    if deleted:
        con.commit()
    return deleted


def _sync_used_merchants_to_vendors(con) -> int:
    """receipts/receipt_items で実際に使われている (merchant, invoice_number) を
    invoice_vendors に自動登録する。

    例: receipt_items に「○○市水道事業会計 / T...」 が存在 → invoice_vendors に未登録なら INSERT。
    集計表に出る事業者は vendor master にも登録されているべき (= ユーザの要望)。

    既存の同 invoice_number vendor は上書きしない (= ユーザの手入力データを保護)。
    """
    from src.server.constants import CARD_BANKS
    bad_names = set(CARD_BANKS) | {"レシート", "MUFG"}
    rows = con.execute(
        """
        SELECT DISTINCT merchant, invoice_number FROM receipts
        WHERE merchant IS NOT NULL AND merchant != ''
          AND invoice_number IS NOT NULL AND invoice_number != ''
        UNION
        SELECT DISTINCT merchant, invoice_number FROM receipt_items
        WHERE merchant IS NOT NULL AND merchant != ''
          AND invoice_number IS NOT NULL AND invoice_number != ''
        """
    ).fetchall()
    inserted = 0
    for r in rows:
        merchant, inv = r["merchant"], r["invoice_number"]
        if merchant in bad_names:
            continue  # bank 名は登録しない
        if con.execute(
            "SELECT 1 FROM invoice_vendors WHERE invoice_number=? LIMIT 1", (inv,)
        ).fetchone():
            continue  # 既存 vendor あり、 上書きしない
        con.execute(
            "INSERT INTO invoice_vendors (service_name, company_name, invoice_number) "
            "VALUES (?, ?, ?)",
            (merchant, merchant, inv),
        )
        inserted += 1
    if inserted:
        con.commit()
    return inserted


def _delete_bank_named_vendors(con) -> int:
    """invoice_vendors の service_name が決済手段の bank 名 (= 「レシート」 / VPASS /
    MUFG / カード系) になっている entry を削除。

    過去の自動 upsert 経路 (= /tax の「取引から追加」 で service_name=tx.bank を
    使っていた) で混入したもの。 bank は適格事業者ではないので vendor master に
    残す意味がない。
    """
    from src.server.constants import CARD_BANKS
    bad = list(CARD_BANKS) + ["レシート", "MUFG"]
    placeholders = ",".join("?" * len(bad))
    cur = con.execute(
        f"DELETE FROM invoice_vendors WHERE service_name IN ({placeholders})",
        bad,
    )
    n = cur.rowcount or 0
    if n:
        con.commit()
    return n


def _consolidate_parent_merchant_from_items(con) -> int:
    """receipts (filename あり = OCR 画像) の親 merchant を receipt_items から逆算。

    内訳 (receipt_items) が単一事業者 → 親 merchant をその事業者名に統一
    内訳が複数事業者 (= 上水/下水 等) → 親 merchant を空に (= 集計に親 merchant を
    使わない、 内訳のみ集計対象)

    効果: 物件名 / 受領者寄りの OCR 抽出 merchant (例: 「草津市水道お客様センター」)
    が親に残らなくなり、 自動 vendor 登録時に無関係な master が増殖しない。
    """
    rows = con.execute(
        """
        SELECT r.id, COALESCE(r.merchant, '') AS merchant
        FROM receipts r
        WHERE r.filename IS NOT NULL AND r.filename != ''
          AND EXISTS (SELECT 1 FROM receipt_items i WHERE i.receipt_id = r.id
                        AND i.merchant IS NOT NULL AND i.merchant != '')
        """
    ).fetchall()
    fixed = 0
    for r in rows:
        items = con.execute(
            "SELECT DISTINCT merchant FROM receipt_items WHERE receipt_id=? "
            "AND merchant IS NOT NULL AND merchant != ''",
            (r["id"],),
        ).fetchall()
        unique = {it[0] for it in items}
        if len(unique) == 1:
            new = next(iter(unique))
            if r["merchant"] != new:
                con.execute(
                    "UPDATE receipts SET merchant=? WHERE id=?", (new, r["id"]),
                )
                fixed += 1
        elif len(unique) >= 2:
            if r["merchant"]:
                con.execute(
                    "UPDATE receipts SET merchant='' WHERE id=?", (r["id"],),
                )
                fixed += 1
    if fixed:
        con.commit()
    return fixed


def _override_ocr_merchant_from_meta(con) -> int:
    """OCR receipt (= filename あり) の merchant を、 同 tx 群のメタ receipt
    (filename NULL かつ invoice_number あり) があれば その正式事業者名 + T 番号で
    上書きする。

    例: OCR receipt (merchant=物件名) を、 同 tx にリンクしているメタ receipt
    (merchant=適格事業者名 + T 番号) の内容で上書き。
    物件名 / 受領者 → 適格事業者名 への自動修正。

    receipt_items も同様に更新する (= OCR で取った内訳の merchant も統一)。
    既に invoice_number 一致の場合のみ上書き対象にして安全側に倒す。
    """
    rows = con.execute(
        """
        SELECT r1.id AS ocr_id, r1.merchant AS ocr_merchant,
               COALESCE(r1.invoice_number, '') AS ocr_invoice
        FROM receipts r1
        WHERE r1.filename IS NOT NULL AND r1.filename != ''
        """
    ).fetchall()
    updated = 0
    for r in rows:
        meta = con.execute(
            """
            SELECT r2.merchant, r2.invoice_number FROM receipts r2
            JOIN receipt_links rl2 ON rl2.receipt_id = r2.id
            WHERE rl2.transaction_id IN (
                SELECT transaction_id FROM receipt_links WHERE receipt_id = ?
            )
            AND (r2.filename IS NULL OR r2.filename = '')
            AND r2.invoice_number IS NOT NULL AND r2.invoice_number != ''
            LIMIT 1
            """,
            (r["ocr_id"],),
        ).fetchone()
        if not meta:
            continue
        new_m = (meta["merchant"] or "").strip()
        new_i = (meta["invoice_number"] or "").strip()
        if not new_m or not new_i:
            continue
        if r["ocr_merchant"] == new_m and r["ocr_invoice"] == new_i:
            continue
        con.execute(
            "UPDATE receipts SET merchant=?, invoice_number=? WHERE id=?",
            (new_m, new_i, r["ocr_id"]),
        )
        # receipt_items の merchant/invoice_number は OCR 由来 (= 物件名で
        # 揃っている) なら一括上書き、 内訳ごとに別事業者なら触らない。
        # 簡素化: 既存 receipt_items.merchant が ocr_merchant と一致するもののみ更新。
        con.execute(
            "UPDATE receipt_items SET merchant=?, invoice_number=? "
            "WHERE receipt_id=? AND (merchant IS NULL OR merchant='' OR merchant=?)",
            (new_m, new_i, r["ocr_id"], r["ocr_merchant"]),
        )
        updated += 1
    if updated:
        con.commit()
    return updated


def _propagate_category_to_receipt_tx(con) -> int:
    """receipt link 経由で「レシート tx」 の category を元経費 tx から継承する。

    upload-receipt は受信時に元経費 tx (MUFG 引落 / カード明細) が見つからないと
    bank='レシート' で独立した tx を作る。 後から auto-link で同 receipt が元経費
    にも link されたら、 category も同期しないとインボイス集計対象外のまま残る。

    継承条件: bank='レシート' tx で category が空、 かつ同 receipt が link する別 bank
    の tx に「経費」 「今回は経費」 のいずれかがあれば、 そのカテゴリを継承する。
    一度 4 件のレシート tx が「経費」 になれば、 後段の _auto_categorize_from_history
    が同 (bank, description_normalized) の他レシート tx (= MUFG 引落未到着の最新月分)
    にも経費分類を広げる連鎖が起きる。
    """
    cur = con.execute(
        """
        UPDATE transactions
        SET category = (
            SELECT t2.category FROM receipt_links rl1
            JOIN receipt_links rl2 ON rl1.receipt_id = rl2.receipt_id
                                   AND rl2.transaction_id != rl1.transaction_id
            JOIN transactions t2 ON t2.id = rl2.transaction_id
            WHERE rl1.transaction_id = transactions.id
              AND t2.bank != 'レシート'
              AND t2.category IN ('経費', '今回は経費')
            LIMIT 1
        )
        WHERE bank = 'レシート'
          AND (category IS NULL OR category = '')
          AND EXISTS (
            SELECT 1 FROM receipt_links rl1
            JOIN receipt_links rl2 ON rl1.receipt_id = rl2.receipt_id
                                   AND rl2.transaction_id != rl1.transaction_id
            JOIN transactions t2 ON t2.id = rl2.transaction_id
            WHERE rl1.transaction_id = transactions.id
              AND t2.bank != 'レシート'
              AND t2.category IN ('経費', '今回は経費')
          )
        """
    )
    n = cur.rowcount or 0
    if n:
        con.commit()
    return n


def _auto_link_invoice_meta_receipts(con) -> int:
    """メタレシート (filename=NULL かつ invoice_number あり) を、 同 (bank,
    description_normalized) を持つ経費 tx に自動 link する。

    1 度「適格事業者登録」 を行えば過去・未来の全該当取引に効くようにするための
    post-process。 新規 scrape 後の refresh_categorization で呼ばれることで、
    将来取り込まれる新規 tx も自動で invoice_number 解決される。
    """
    cur = con.execute(
        """
        INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id)
        SELECT DISTINCT r.id, t.id
        FROM receipts r
        JOIN receipt_links rl0 ON rl0.receipt_id = r.id
        JOIN transactions t0 ON t0.id = rl0.transaction_id
        JOIN transactions t ON t.bank = t0.bank
                            AND t.description_normalized = t0.description_normalized
        WHERE (r.filename IS NULL OR r.filename = '')
          AND r.invoice_number IS NOT NULL AND r.invoice_number != ''
          AND t.category IN ('経費', '今回は経費')
          AND t.id != t0.id
          AND t.description_normalized IS NOT NULL
          AND t.description_normalized != ''
        """
    )
    n = cur.rowcount or 0
    if n:
        con.commit()
    return n


def refresh_categorization() -> None:
    """全 auto-classify を回す (起動時 + 書込後の再分類用)。

    config/auto_rules.toml の 4 種ルール (apply_auto_rules で一括実行) を先に走らせ、
    その後 complex な Python 関数 (返品判定 / 履歴学習) を順に呼ぶ。
    最後にメタレシートの auto-link で「適格事業者登録の 1:N 適用」 を新規 tx にも反映。
    各ステップの分類件数をログに出すので何が変わったか可視化される。
    """
    con = _tx_db()
    try:
        rule_counts = _apply_auto_rules(con) or {}
        non_zero = {k: v for k, v in rule_counts.items() if v}
        if non_zero:
            summary = ", ".join(f"{k}:{v}" for k, v in non_zero.items())
            print(f"[refresh_categorization] auto_rules → {summary}")
        n_ret = _auto_categorize_returned_purchases(con) or 0
        if n_ret:
            print(f"[refresh_categorization] returned_purchases → {n_ret} 行")
        n_hist = _auto_categorize_from_history(con) or 0
        n_amt = _auto_link_receipts_by_amount_date(con)
        if n_amt:
            print(f"[refresh_categorization] receipt amount+date auto-link → {n_amt} 行")
            _invalidate_tx_cache()
        n_prop = _propagate_category_to_receipt_tx(con)
        if n_prop:
            print(f"[refresh_categorization] receipt tx category propagation → {n_prop} 行")
            _invalidate_tx_cache()
            # category が変わったので auto_history を再実行 (= 同 desc 他 tx 学習)
            n_hist2 = _auto_categorize_from_history(con) or 0
            if n_hist2:
                print(f"[refresh_categorization] auto_history (chain) → {n_hist2} 行")
        n_fill = _auto_fill_receipt_meta_from_siblings(con)
        if n_fill:
            print(f"[refresh_categorization] receipt meta sibling fill → {n_fill} 行")
        n_meta = _auto_link_invoice_meta_receipts(con)
        if n_meta:
            print(f"[refresh_categorization] invoice_meta auto-link → {n_meta} 行")
            _invalidate_tx_cache()
        n_norm = _normalize_invoice_numbers(con)
        if n_norm:
            print(f"[refresh_categorization] invoice_number normalized → {n_norm} 行")
        n_consol = _consolidate_meta_receipts(con)
        if n_consol:
            print(f"[refresh_categorization] meta receipts consolidated → {n_consol} 削除")
            _invalidate_tx_cache()
        n_ovr = _override_ocr_merchant_from_meta(con)
        if n_ovr:
            print(f"[refresh_categorization] OCR merchant overridden → {n_ovr} 行")
            _invalidate_tx_cache()
        n_par = _consolidate_parent_merchant_from_items(con)
        if n_par:
            print(f"[refresh_categorization] parent merchant ← items → {n_par} 行")
            _invalidate_tx_cache()
        n_bank = _delete_bank_named_vendors(con)
        if n_bank:
            print(f"[refresh_categorization] bank-named vendors deleted → {n_bank} 行")
        n_sync = _sync_used_merchants_to_vendors(con)
        if n_sync:
            print(f"[refresh_categorization] used merchants → vendors sync → {n_sync} 行")
        # auto_categorize_from_history 自体がログ出すので件数だけサマリ
        if (n_hist == 0 and not non_zero and n_ret == 0
                and n_amt == 0 and n_prop == 0 and n_fill == 0
                and n_meta == 0 and n_norm == 0 and n_consol == 0
                and n_ovr == 0 and n_par == 0 and n_bank == 0 and n_sync == 0):
            print("[refresh_categorization] 変更なし (全件分類済)")
    finally:
        con.close()


app = FastAPI(lifespan=lifespan)

# 静的アセット (ロゴ等) — _PUBLIC_PREFIXES で /static/ は LoginGate を通過する
from fastapi.staticfiles import StaticFiles  # noqa: E402
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


# ─────────────────────────────────────────────
# 支出分類 Web UI
# ─────────────────────────────────────────────

from src.server.constants import (
    CATEGORIES, EXPENSE_CATS, INCOME_CATS,
    CARD_BANKS as _CARD_BANKS,
    SITE_BANKS as _SITE_BANKS,
)

# detect_tags が付けるジャンル系タグの集合 (Blank タグ判定用)。
# matched / card:* / amazon_points_full / paypay / 返品 / receipt / store:* /
# stripe / blank はメタタグ (決済識別 / 紐付け状態) なのでジャンルに含めない。
_GENRE_TAGS = frozenset({
    "car", "book", "drink", "convenience", "supermarket", "restaurant",
    "homecenter", "landlord", "facility", "invest", "insurance", "medical",
    "service", "activity", "fantasy",
})


# ─────────────────────────────────────────────
# ユーザタグ設定 (使用/不使用 + カスタムタグ) のキャッシュ
# config/user_tags.toml が変更されたら _invalidate_user_tags_cache() を呼ぶ
# ─────────────────────────────────────────────

_user_tags_cache: dict = {"data": None}


def _get_user_tags():
    if _user_tags_cache["data"] is None:
        from src.personal.user_tags import load_user_tags
        cfg = load_user_tags()
        # custom keywords を NFKC 正規化キャッシュ
        from src.server.categorize import normalize_desc as _nd
        for c in cfg.get("custom", []):
            c["_nk"] = [_nd(k) for k in c.get("keywords", [])]
        _user_tags_cache["data"] = cfg
    return _user_tags_cache["data"]


def _invalidate_user_tags_cache():
    _user_tags_cache["data"] = None
    _invalidate_tx_cache()
from src.server.categorize import (
    normalize_desc as _normalize_desc,
    detect_tags as _detect_tags,
    apply_auto_rules as _apply_auto_rules_impl,
    auto_categorize_returned_purchases as _auto_categorize_returned_purchases_impl,
    auto_categorize_from_history as _auto_categorize_from_history_impl,
)


def _apply_auto_rules(con):
    return _apply_auto_rules_impl(con, _invalidate_tx_cache)


def _auto_categorize_returned_purchases(con):
    return _auto_categorize_returned_purchases_impl(con, _invalidate_tx_cache)


def _auto_categorize_from_history(con):
    return _auto_categorize_from_history_impl(con, _invalidate_tx_cache)


def _tx_db() -> sqlite3.Connection:
    """スキーマは src.db.ensure_schema() に集約。
    その上でレガシー receipts.transaction_id → receipt_links 移行と
    description_normalized のバックフィルを行う。"""
    global _db_migrated
    from src.db import connect as _db_connect_central
    con = _db_connect_central()
    if not _db_migrated:
        # 未正規化行をバックフィル
        if con.execute("SELECT COUNT(*) FROM transactions WHERE description_normalized IS NULL OR description_normalized = ''").fetchone()[0]:
            con.create_function("nfkc", 1, _normalize_desc)
            con.execute("UPDATE transactions SET description_normalized = nfkc(description) WHERE description_normalized IS NULL OR description_normalized = ''")
            con.commit()
        # Migration: receipts.transaction_id (non-null) → receipt_links
        con.execute("""
            INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id)
            SELECT id, transaction_id FROM receipts WHERE transaction_id IS NOT NULL
        """)
        con.commit()
        _db_migrated = True
    return con


def _summary_from_rows(rows: list, income: bool = False) -> dict:
    cats = INCOME_CATS if income else EXPENSE_CATS
    amount_key = "credit" if income else "debit"
    result: dict = {c: {"cnt": 0, "total": 0} for c in cats}
    for r in rows:
        key = r.get("category") or ""
        if key not in result:
            result[key] = {"cnt": 0, "total": 0}
        result[key]["cnt"] += 1
        result[key]["total"] += r.get(amount_key) or 0
    return result


# ─────────────────────────────────────────────
# トランザクションキャッシュ
# ─────────────────────────────────────────────

_TX_CACHE_TTL = 86400  # 1日（値の操作時は即時無効化）
_tx_cache: dict[tuple, tuple[float, list]] = {}
_db_migrated = False


def _invalidate_tx_cache() -> None:
    _tx_cache.clear()


def _update_cache_row_categories(updates: dict[int, str]) -> None:
    """キャッシュ内の指定 tx id の category だけインプレース更新。
    全消し → cold rebuild (~6s) を回避し、サマリ・取引一覧の再計算を即時化する。
    キャッシュに無い行 (別年など) はスキップ。
    """
    if not updates:
        return
    for ts, rows in _tx_cache.values():
        for r in rows:
            new = updates.get(r.get("id"))
            if new is not None:
                r["category"] = new


def _refresh_all_conflict_tags(con) -> None:
    """全 conflict キーを SQL で再計算し、 cache 内の全 row の conflict tag を最新化。
    「⚠ ブレ」 タグ click 時に呼ぶことで、 直前まで in-place 同期されていなかった
    変更 (例: 別端末からの API、 cache TTL 切れ前後) も含めて最新の状態を保証する。

    cache key は (year, income) なので、 各 cache entry について該当 year の
    conflict_keys を再計算する (= 表示中の年度と判定の期間を一致させる)。
    軽量: cache entry 数 × SQL 1 発 + entry 内 row 巡回。
    """
    from src.server.categorize import detect_category_conflict_keys
    _CONFLICT_ELIGIBLE_CATS = ("", "経費", "個人支出")
    for cache_key, (ts, rows) in _tx_cache.items():
        year = cache_key[0] if cache_key else None
        keys = detect_category_conflict_keys(con, year=year or None)
        for r in rows:
            ck = (r.get("bank") or "", r.get("description_normalized") or "")
            tags = r.get("tags") or []
            eligible = r.get("category") in _CONFLICT_ELIGIBLE_CATS
            want = (ck in keys) and eligible
            has = "conflict" in tags
            if want and not has:
                tags.append("conflict")
                r["tags"] = tags
            elif (not want) and has:
                r["tags"] = [t for t in tags if t != "conflict"]


def _update_cache_conflict_for_keys(con, keys: set[tuple[str, str]]) -> None:
    """category 変更後、 該当 (bank, description_normalized) グループの conflict
    タグだけを再計算して全 cache 内の該当 row を in-place 更新。

    cache key は (year, income) で year ごとに分かれているため、 各 cache entry
    について該当 year の DB データだけで conflict 判定する (= UI で見ている年度と
    判定の期間を一致させる)。
    軽量: cache entry × keys 数の SQL (= ~50ms 程度)。 cold rebuild は不要。
    """
    if not keys:
        return
    _CONFLICT_ELIGIBLE_CATS = ("", "経費", "個人支出")
    for cache_key, (ts, rows) in _tx_cache.items():
        year = cache_key[0] if cache_key else None
        # 該当 year 内で各 key の conflict 状態を判定
        is_conflict_map: dict[tuple[str, str], bool] = {}
        for bank, desc_norm in keys:
            if not desc_norm:
                continue
            sql = (
                "SELECT "
                "  SUM(CASE WHEN category='経費' THEN 1 ELSE 0 END), "
                "  SUM(CASE WHEN category='個人支出' THEN 1 ELSE 0 END) "
                "FROM transactions "
                "WHERE bank=? AND description_normalized=?"
            )
            params: list = [bank, desc_norm]
            if year:
                sql += " AND substr(date,1,4)=?"
                params.append(year)
            row = con.execute(sql, params).fetchone()
            is_conflict_map[(bank, desc_norm)] = bool(row and row[0] and row[1])
        # cache entry 内の該当 row を巡回して tags を更新
        for r in rows:
            ck = (r.get("bank") or "", r.get("description_normalized") or "")
            if ck not in is_conflict_map:
                continue
            tags = r.get("tags") or []
            eligible = r.get("category") in _CONFLICT_ELIGIBLE_CATS
            want = is_conflict_map[ck] and eligible
            has = "conflict" in tags
            if want and not has:
                tags.append("conflict")
                r["tags"] = tags
            elif (not want) and has:
                r["tags"] = [t for t in tags if t != "conflict"]


def _rebuild_tx_cache() -> None:
    """スクレイピング完了後など、既存キャッシュと同じ条件で再構築する。"""
    con = _tx_db()
    try:
        years = [r[0] for r in con.execute(
            "SELECT DISTINCT substr(date,1,4) FROM transactions ORDER BY 1 DESC"
        ).fetchall()]
        _tx_cache.clear()
        for year in years:
            for income in (False, True):
                _fetch_base_rows(con, year, income)
        print(f"[cache] 再構築完了: {len(years)} 年分 × 2モード")
    except Exception as e:
        print(f"[cache] 再構築失敗: {e}")
    finally:
        con.close()


def _fetch_base_rows(con, year: str, income: bool) -> list[dict]:
    """SQL実行・タグ付け・分割処理済みの全行を返す。結果は1日キャッシュ。"""
    key = (year, income)
    cached = _tx_cache.get(key)
    if cached and _time.time() - cached[0] < _TX_CACHE_TTL:
        return cached[1]

    conds = ["t.credit>0"] if income else ["(t.debit>0 OR (t.bank='Amazon' AND t.debit=0))"]
    params: list = []
    if year:
        conds.append("substr(t.date,1,4)=?")
        params.append(year)
    where = "WHERE " + " AND ".join(conds)

    site_list = "','".join(_SITE_BANKS)
    card_list = "','".join(_CARD_BANKS)
    # site_matched 用: カード銀行自身は除外（メルカードの自己マッチ防止）
    site_list_match = "','".join(_SITE_BANKS - _CARD_BANKS)

    rows = con.execute(f"""
        SELECT t.id, t.date, t.bank, t.description, t.description_normalized, t.debit, t.credit, t.category,
               COALESCE(m.card_bank,
                        (SELECT 'メルカード' FROM tx_links
                         WHERE tx_a_id = t.id AND link_type='mercard_mufg' LIMIT 1))
                 as card_bank,
               (CASE WHEN t.bank IN ('{site_list}') THEN
                 COALESCE(
                   -- 通常の1:1突合
                   (SELECT c.bank FROM transactions c
                    WHERE c.bank IN ('{card_list}')
                    AND c.debit = (CASE
                      WHEN t.description LIKE '[%/%]%' THEN
                        COALESCE(
                          (SELECT aod2.order_total FROM amazon_order_details aod2
                           WHERE aod2.order_id = substr(t.description, 2, instr(t.description, '/') - 2)
                           AND aod2.order_total > 0
                           LIMIT 1),
                          (SELECT p.debit FROM transactions p
                           WHERE p.bank = t.bank
                           AND p.description LIKE ('[' || substr(t.description, 2, instr(t.description, '/') - 2) || ']%')
                           AND p.description NOT LIKE '[%/%]%'
                           LIMIT 1),
                          t.debit)
                      WHEN t.bank = 'Amazon' AND aod.order_total > 0 THEN aod.order_total
                      ELSE t.debit END)
                    AND abs(julianday(replace(c.date,'/','-')) - julianday(replace(t.date,'/','-'))) <= 5
                    LIMIT 1),
                   -- サブ行自身の金額で直接照合（order_totalが合わない複数配送向け）
                   (SELECT c1.bank FROM transactions c1
                    WHERE t.description LIKE '[%/%]%'
                    AND t.debit > 0
                    AND c1.bank IN ('{card_list}')
                    AND c1.debit = t.debit
                    AND (c1.description_normalized LIKE '%AMAZON%' OR c1.description_normalized LIKE '%アマゾン%')
                    AND abs(julianday(replace(c1.date,'/','-')) - julianday(replace(t.date,'/','-'))) <= 5
                    LIMIT 1),
                   -- 同一オーダーのサブ行からカードを継承（複数配送親行向け）
                   (SELECT c_s.bank FROM transactions sibling
                    JOIN transactions c_s ON c_s.bank IN ('{card_list}')
                      AND c_s.debit = sibling.debit
                      AND (c_s.description_normalized LIKE '%AMAZON%' OR c_s.description_normalized LIKE '%アマゾン%')
                      AND abs(julianday(replace(c_s.date,'/','-')) - julianday(replace(sibling.date,'/','-'))) <= 5
                    WHERE t.description NOT LIKE '[%/%]%'
                      AND t.bank = 'Amazon'
                      AND t.description LIKE '[%]%'
                      AND sibling.bank = 'Amazon'
                      AND sibling.description LIKE ('[' || substr(t.description, 2, instr(t.description, ']') - 2) || '/%]%')
                      AND sibling.debit > 0
                    LIMIT 1),
                   -- サブ行が兄弟サブ行のカードを継承（部分突合済み注文向け）
                   -- 例: ¥3,518 注文で /1 ¥2,052 が VPASS と突合済み、
                   -- /2 ¥1,466 が同 VPASS の charge と一致しなくても兄弟経由で VPASS バッジ表示
                   (SELECT c_sib.bank FROM transactions sibling2
                    JOIN transactions c_sib ON c_sib.bank IN ('{card_list}')
                      AND c_sib.debit = sibling2.debit
                      AND (c_sib.description_normalized LIKE '%AMAZON%' OR c_sib.description_normalized LIKE '%アマゾン%')
                      AND abs(julianday(replace(c_sib.date,'/','-')) - julianday(replace(sibling2.date,'/','-'))) <= 5
                    WHERE t.description LIKE '[%/%]%'
                      AND t.bank = 'Amazon'
                      AND sibling2.bank = 'Amazon'
                      AND sibling2.id != t.id
                      AND sibling2.description LIKE ('[' || substr(t.description, 2, instr(t.description, '/') - 2) || '/%]%')
                      AND sibling2.debit > 0
                    LIMIT 1),
                   -- Amazon 複数配送突合（tx_links link_type='amazon_split'）
                   -- tx_a = Amazon 親 transaction, tx_b = カード明細
                   (SELECT c2.bank FROM tx_links tl
                    JOIN transactions parent_t ON parent_t.id = tl.tx_a_id
                    JOIN transactions c2 ON c2.id = tl.tx_b_id
                    WHERE tl.link_type='amazon_split'
                    AND parent_t.bank='Amazon'
                    AND parent_t.description LIKE ('[' || (CASE
                      WHEN t.description LIKE '[%/%]%'
                        THEN substr(t.description, 2, instr(t.description, '/') - 2)
                      ELSE substr(t.description, 2, instr(t.description, ']') - 2)
                    END) || ']%')
                    LIMIT 1),
                   -- Kindle D01 突合（invoice_receipts）
                   (SELECT c3.bank FROM invoice_receipts ir
                    JOIN transactions c3 ON c3.id = ir.card_tx_id
                    WHERE ir.bank='Amazon'
                      AND ir.order_id = substr(t.description, 2, instr(t.description, ']') - 2)
                    LIMIT 1),
                   -- shop_card 突合（CAMPFIRE 等、tx_links link_type='shop_card'）
                   -- tx_a = ショップ購入, tx_b = カード明細
                   (SELECT c.bank FROM tx_links tl
                    JOIN transactions c ON c.id = tl.tx_b_id
                    WHERE tl.link_type='shop_card' AND tl.tx_a_id = t.id
                    LIMIT 1),
                   -- メルカード行 → 同月の MUFG 引き落とし突合（tx_links link_type='mercard_mufg'）
                   (SELECT CASE WHEN t.bank = 'メルカード' THEN 'MUFG' ELSE NULL END
                    FROM tx_links
                    WHERE link_type='mercard_mufg'
                    AND json_extract(extra_json, '$.use_month') = substr(t.date, 1, 7)
                    LIMIT 1),
                   -- Mercari購入 → メルカード翌月払い突合（日付＋タイトル一致）
                   (SELECT 'メルカード' FROM transactions mc
                    WHERE t.bank = 'Mercari'
                    AND mc.bank = 'メルカード'
                    AND mc.date = t.date
                    AND (
                      mc.description = t.description
                      OR mc.description = CASE
                        WHEN t.description LIKE '[%] %'
                        THEN substr(t.description, instr(t.description, '] ') + 2)
                        ELSE t.description
                      END
                    )
                    LIMIT 1)
                 )
                ELSE NULL END) as paid_via,
               (CASE WHEN t.bank = 'メルカード' THEN
                 (SELECT COUNT(*) FROM transactions merc
                  WHERE merc.bank = 'Mercari'
                  AND merc.date = t.date
                  AND (
                    merc.description = t.description
                    OR (merc.description LIKE '[%] %'
                        AND substr(merc.description, instr(merc.description, '] ') + 2) = t.description)
                  ))
                WHEN t.bank IN ('{card_list}') THEN
                 (SELECT COUNT(*) FROM transactions s
                  WHERE s.bank IN ('{site_list_match}')
                  AND s.debit = t.debit
                  AND abs(julianday(replace(s.date,'/','-')) - julianday(replace(t.date,'/','-'))) <= 2)
                 + (SELECT COUNT(*) FROM tx_links WHERE tx_b_id = t.id AND link_type='amazon_split')
                 + (SELECT COUNT(*) FROM tx_links WHERE tx_b_id = t.id AND link_type='shop_card')
                ELSE 0 END) as site_matched,
               aod.item_subtotal, aod.shipping, aod.discount,
               aod.points_used, aod.gift_card, aod.order_total,
               (SELECT c.debit FROM tx_links tl
                JOIN transactions c ON c.id = tl.tx_b_id
                WHERE tl.link_type='shop_card' AND tl.tx_a_id = t.id
                LIMIT 1) as matched_card_amount,
               -- receipt_ids = 実物 PDF/画像が残っているレシートのみ (= filename あり)。
               -- filename=NULL の「メタのみエントリ」 (= 事業者+invoice_number 後付け) は
               -- ここに含めず、 invoice_meta_ids 側に分離 → 📭 レシートなし 判定で除外しない。
               (SELECT GROUP_CONCAT(rl.receipt_id) FROM receipt_links rl
                JOIN receipts r ON r.id = rl.receipt_id
                WHERE rl.transaction_id = t.id
                  AND r.filename IS NOT NULL AND r.filename != '') as receipt_ids,
               (SELECT GROUP_CONCAT(rl.receipt_id) FROM receipt_links rl
                JOIN receipts r ON r.id = rl.receipt_id
                WHERE rl.transaction_id = t.id
                  AND (r.filename IS NULL OR r.filename = '')
                  AND r.invoice_number IS NOT NULL AND r.invoice_number != '') as invoice_meta_ids,
               -- locked_by: tx_meta.lock_source の値 (例: 'povo')。 UI でカテゴリ
               -- 変更ボタンを disabled 表示するための情報
               (SELECT value FROM tx_meta
                WHERE transaction_id = t.id AND key='lock_source' LIMIT 1) AS locked_by,
               -- bank='レシート' tx で、 同 receipt が別 bank (= MUFG/VPASS 等) の経費 tx にも
               -- link 済の場合 > 0。 merge mode で重複表示を隠すためのフラグ
               (CASE WHEN t.bank = 'レシート' THEN (
                 SELECT COUNT(*) FROM receipt_links rl1
                 JOIN receipt_links rl2 ON rl1.receipt_id = rl2.receipt_id
                                        AND rl2.transaction_id != rl1.transaction_id
                 JOIN transactions t2 ON t2.id = rl2.transaction_id
                 WHERE rl1.transaction_id = t.id
                   AND t2.bank != 'レシート'
               ) ELSE 0 END) AS receipt_alt_link_count,
               (SELECT MAX(is_kindle) FROM amazon_order_items
                WHERE order_id = CASE
                  WHEN t.description LIKE '[%/%]%' THEN substr(t.description, 2, instr(t.description, '/') - 2)
                  ELSE substr(t.description, 2, instr(t.description, ']') - 2)
                END) as is_kindle
        FROM transactions t
        LEFT JOIN (
          -- card_bank_billing link は tx_a=MUFG引落, extra_json.card_bank で対応カード名
          SELECT tx_a_id, json_extract(extra_json, '$.card_bank') AS card_bank
          FROM tx_links WHERE link_type='card_bank_billing'
        ) m ON m.tx_a_id = t.id
        LEFT JOIN amazon_order_details aod
          ON t.bank='Amazon'
          AND length(t.description) > 2
          AND substr(t.description, 1, 1) = '['
          AND substr(t.description, 2, instr(t.description, ']') - 2) = aod.order_id
        {where} ORDER BY t.date DESC, t.id DESC
    """, params).fetchall()

    amazon_invoice_ids     = _amazon_invoice_order_ids()
    mercari_invoice_ids    = _mercari_invoice_tx_ids()
    rakuten_invoice_ids    = _generic_invoice_ids(_RAKUTEN_INVOICE_DIR)
    campfire_invoice_ids   = _campfire_invoice_ids()
    aliexpress_invoice_ids = _generic_invoice_ids(_ALIEXPRESS_INVOICE_DIR)
    return_tx_ids          = _get_return_tx_ids(con)

    # 仕訳ブレ (= 同 desc が「経費」 と「個人支出」 両方に分類されている tx group)。
    # year 指定時はその年度内のみで判定 (= 表示中の表とブレ判定の期間を一致させる)。
    from src.server.categorize import detect_category_conflict_keys
    conflict_keys = detect_category_conflict_keys(con, year=year or None)

    result = []
    user_tags = _get_user_tags()
    user_disabled = set(user_tags.get("disabled", []))
    custom_tags = user_tags.get("custom", [])
    for r in rows:
        d = dict(r)
        d["tags"] = _detect_tags(d["description"], d["bank"], d.get("description_normalized") or "")
        if d.get("is_kindle"):
            if "book" not in d["tags"]:
                d["tags"].append("book")
        # 「事業」 タグは廃止 (= 経費フィルタは category タブで足りる、
        # ツールタグの整理時にユーザ要望で削除)
        # 「ブレ」 タグ: 同 (bank, description_normalized) が「経費」 と「個人支出」
        # 両方に分類されている = 仕訳ブレ発見用。
        # 安定カテゴリ (経費 / 個人支出) と 未分類 のみ判定対象。 以下は除外:
        # - 「今回は経費」 「今回は個人支出」 = 既に意図的な一時分類 (= 正しい運用)
        # - 「出金」 = 為替手数料 等が親振込に追従して付く (= 仕様通り)
        # - 収入系 (給与/家賃収入/売上等) = 別軸 (= ブレ議論の対象外)
        ck = (d.get("bank") or "", d.get("description_normalized") or "")
        if ck in conflict_keys and d.get("category") in ("", "経費", "個人支出"):
            d["tags"].append("conflict")
        # 「レシートなし」 タグ: 経費 (税務調査エビデンス必要) で receipt 未紐付けの取引。
        # 確定申告の証憑保存義務に対する警告として支出分類でフィルタ可能にする。
        # ショッピングサイト明細 (Amazon / 楽天 / Yahoo! / ヤフオク / Mercari / AliExpress
        # / Makuake / CAMPFIRE 等) は注文番号で領収書 PDF を取得可能 = レシートあり扱い。
        if (d.get("category") in ("経費", "今回は経費")
                and not d.get("receipt_ids")
                and d.get("bank") not in _SITE_BANKS):
            d["tags"].append("no_receipt")
        # ユーザカスタムタグ (config/user_tags.toml の [[custom]])
        if custom_tags:
            desc_n = d.get("description_normalized") or d["description"] or ""
            for c in custom_tags:
                if c["id"] in d["tags"]:
                    continue
                if any(k and k in desc_n for k in c.get("_nk", [])):
                    d["tags"].append(c["id"])
        # 使用不使用設定で disabled な builtin タグは tags から除去
        if user_disabled:
            d["tags"] = [t for t in d["tags"] if t not in user_disabled]
        # card:{brand} タグ - そのカード自身の明細 + 当該カードに紐づくショップ行
        # _CARD_BANKS には AmazonPay も含むので card:AmazonPay も自動付与される
        if d.get("bank") in _CARD_BANKS:
            d["tags"].append(f"card:{d['bank']}")
        if d.get("card_bank"):
            d["tags"].extend(["matched", f"matched:{d['card_bank']}", f"card:{d['card_bank']}"])
        if d.get("paid_via"):
            d["tags"].extend(["matched", f"matched:{d['paid_via']}", f"card:{d['paid_via']}"])
        if d.get("receipt_ids"):
            d["tags"].append("receipt")
        if d["id"] in return_tx_ids:
            d["tags"].append("返品")
        # PayPay残高決済: ヤフオク購入 / Yahoo!ショッピングでカード突合がない、
        # またはカード金額 < 落札額（差額を PayPay 残高で補填）の行。
        # PayPay は実費なので経費計上可、ただし「突合済み」扱いで未突合フィルタには出さない。
        if d.get("bank") in ("ヤフオク購入", "Yahoo!ショッピング") and (d.get("debit") or 0) > 0:
            mc = d.get("matched_card_amount") or 0
            paypay = (mc and mc < d["debit"]) or (not mc and not d.get("card_bank") and not d.get("paid_via"))
            if paypay:
                d["tags"].append("paypay")
                if "matched" not in d["tags"]:
                    d["tags"].append("matched")

        # Amazon ポイント全額決済: cash 0 + ポイントで賄われた注文。
        # 親 debit=0 はもちろん、その子サブ行（debit=0）も同タグを継承する。
        # 旧データで一覧由来の debit が残っている D01 単品も order_total=0
        # AND points_used>0 で同等扱い（カード突合は発生しないが
        # 「突合済み」扱いにして未突合フィルタに出さない）。
        is_points_full = False
        if d.get("bank") == "Amazon":
            if (d.get("debit") or 0) == 0 and (d.get("credit") or 0) == 0:
                is_points_full = True
            elif (d.get("order_total") is not None
                  and d.get("order_total") == 0
                  and (d.get("points_used") or 0) > 0):
                is_points_full = True
        if is_points_full:
            d["tags"].append("amazon_points_full")
            if "matched" not in d["tags"]:
                d["tags"].append("matched")
        # PDF 領収書（Amazon / Mercari / 楽天市場 etc.）
        desc = d.get("description") or ""
        m = re.match(r'\[([^/\]]+)', desc)
        tid = m.group(1) if m else ""
        _INVOICE_MAP = {
            "Amazon":     (amazon_invoice_ids,     "amazon"),
            "Mercari":    (mercari_invoice_ids,    "mercari"),
            "Mercari売上": (mercari_invoice_ids,    "mercari"),
            "楽天市場":   (rakuten_invoice_ids,    "rakuten"),
            "CAMPFIRE":   (campfire_invoice_ids,   "campfire"),
            "AliExpress": (aliexpress_invoice_ids, "aliexpress"),
        }
        if d["bank"] in _INVOICE_MAP:
            inv_ids, inv_type = _INVOICE_MAP[d["bank"]]
            d["has_invoice"] = bool(tid and tid in inv_ids)
            d["invoice_type"] = inv_type
        else:
            d["has_invoice"] = False
            d["invoice_type"] = ""
        # ジャンル系タグも store:* (ショッピングサイト識別タグ) も無い行に 'blank'。
        # store:Amazon / Yahoo! / メルカリ / AliExpress / CAMPFIRE / Makuake /
        # ヤフオク / 楽天 等が付いている取引は分類済みとみなす (要望)。
        if not any(t in _GENRE_TAGS or t.startswith("store:") for t in d["tags"]):
            d["tags"].append("blank")
        result.append(d)

    # 各 tx に紐付く receipts の receipt_items を埋める (= /ui の支出分類で内訳
    # 折りたたみ表示するため)。 全 receipt_id を集めて一括 SELECT で N+1 回避。
    _all_rids: set[int] = set()
    for d in result:
        rids = d.get("receipt_ids") or ""
        for rid in str(rids).split(","):
            if rid:
                try:
                    _all_rids.add(int(rid))
                except ValueError:
                    pass
    _items_by_receipt: dict[int, list[dict]] = {}
    if _all_rids:
        placeholders = ",".join("?" * len(_all_rids))
        for ri in con.execute(
            f"SELECT receipt_id, label, amount, merchant, invoice_number, sort_order "
            f"FROM receipt_items WHERE receipt_id IN ({placeholders}) "
            f"ORDER BY receipt_id, sort_order",
            list(_all_rids),
        ).fetchall():
            _items_by_receipt.setdefault(ri[0], []).append({
                "label": ri[1] or "",
                "amount": int(ri[2] or 0),
                "merchant": ri[3] or "",
                "invoice_number": ri[4] or "",
            })
    for d in result:
        items: list[dict] = []
        rids = d.get("receipt_ids") or ""
        for rid in str(rids).split(","):
            if not rid:
                continue
            try:
                items.extend(_items_by_receipt.get(int(rid), []))
            except ValueError:
                pass
        d["receipt_items"] = items

    # 同じ事業者 (= receipts.merchant 一致) を共有する tx 同士でジャンルタグを union。
    # 「草津市水道お客様センター」 の receipt が link する全 tx (= レシート tx 5 件 +
    # MUFG 引落 4 件) は同じ取引先なので landlord 等のジャンルタグが共通であるべき。
    # 表記違い (= MUFG「水道 <description>」 vs レシート「草津市水道お客様センター」)
    # で keyword マッチが片方だけになる事象を吸収する。
    _merchant_to_txids: dict[str, set[int]] = {}
    try:
        for r in con.execute(
            "SELECT DISTINCT r.merchant, rl.transaction_id "
            "FROM receipts r JOIN receipt_links rl ON rl.receipt_id = r.id "
            "WHERE r.merchant IS NOT NULL AND r.merchant != ''"
        ).fetchall():
            _merchant_to_txids.setdefault(r[0], set()).add(r[1])
    except Exception:
        pass
    if _merchant_to_txids:
        _id_to_row = {d.get("id"): d for d in result}
        for tx_ids in _merchant_to_txids.values():
            if len(tx_ids) <= 1:
                continue
            rows_in_group = [_id_to_row[t] for t in tx_ids if t in _id_to_row]
            if len(rows_in_group) <= 1:
                continue
            union_genre = set()
            for d in rows_in_group:
                for t in d.get("tags") or []:
                    if t in _GENRE_TAGS:
                        union_genre.add(t)
            if not union_genre:
                continue
            for d in rows_in_group:
                existing = set(d.get("tags") or [])
                for t in union_genre:
                    if t not in existing:
                        d["tags"].append(t)
                        existing.add(t)
                # blank は ジャンルタグが付いたら外す
                if "blank" in d["tags"] and any(t in _GENRE_TAGS for t in d["tags"]):
                    d["tags"] = [t for t in d["tags"] if t != "blank"]

    # 手数料系の行は直前の同行同日 debit 行のタグを継承する
    # （例: ATM 引出 ¥30,000 → 手数料 ¥220 のとき、ATM 行のタグを手数料にも付ける）
    _FEE_DESC_RE = re.compile(
        r"^(手数料|為替手数料|振込手数料)$|フリコミ\s*テスウリヨウ"
    )
    # 結果は date DESC, id DESC でソートされているので、各 fee 行から
    # 後続要素を走査して同行同日かつ id が小さい debit > 0 行を最初に見つける。
    _FEE_INHERIT_SKIP = {"matched"}
    for i, d in enumerate(result):
        desc = d.get("description") or ""
        if not _FEE_DESC_RE.search(desc):
            continue
        for nxt in result[i + 1:]:
            if (nxt.get("bank") == d.get("bank")
                    and nxt.get("date") == d.get("date")
                    and nxt.get("id", 0) < d.get("id", 0)
                    and (nxt.get("debit") or 0) > 0):
                for t in nxt.get("tags", []) or []:
                    if t in _FEE_INHERIT_SKIP or t.startswith("matched:"):
                        continue
                    if t not in d["tags"]:
                        d["tags"].append(t)
                break

    # PayPal 経由決済の重複ペア検出（汎用）: 同金額同日のショップ行と
    # PayPal 行は同一取引と見なす。ショップ → PayPal で決済 → PayPal が
    # VPASS 等から引き落とし、というチェーン。二重計上を防ぐため
    # PayPal 側を merge 表示で隠す印 _paypal_paired を付与。
    # 対象は SITE_BANKS 全般（AliExpress/Amazon/楽天市場/Yahoo!ショッピング/
    # Mercari 等）。AmazonPay は元々 Amazon Pay 自体が決済手段なので除外。
    _paypal_by_key: dict[tuple, list] = {}
    for d in result:
        if d.get("bank") == "PayPal" and (d.get("debit") or 0) > 0:
            _pp_key = (d["date"], d["debit"])
            _paypal_by_key.setdefault(_pp_key, []).append(d)
    _paypal_pair_targets = _SITE_BANKS - {"PayPal", "AmazonPay", "メルカード"}
    for d in result:
        if d.get("bank") in _paypal_pair_targets and (d.get("debit") or 0) > 0:
            _pp_key = (d["date"], d["debit"])
            partners = _paypal_by_key.get(_pp_key) or []
            if partners:
                partner = partners.pop(0)
                partner["_paypal_paired"] = True
                if "paypal" not in d["tags"]:
                    d["tags"].append("paypal")
                if "matched" not in d["tags"]:
                    d["tags"].append("matched")

    _SPLIT_BANKS = {"Amazon", "Yahoo!ショッピング"}
    split_ids: set[str] = set()
    for r in result:
        if r["bank"] in _SPLIT_BANKS:
            # [oid/N] / [oid/送料] / [oid/任意] のサブ行を識別
            m = re.match(r'\[([^/\]]+)/[^\]]+\]', r.get("description") or "")
            if m:
                split_ids.add(m.group(1))
    if split_ids:
        # サブ行 [oid/N] と送料行 [oid/送料] が DB 上で必ず親.debit と一致するよう
        # split_orders 側で保証されているので、親行は単純に隠すだけ。
        def _is_parent(r: dict) -> bool:
            if r["bank"] not in _SPLIT_BANKS:
                return False
            m = re.match(r'\[([^/\]]+)\]', r.get("description") or "")
            return bool(m and m.group(1) in split_ids)
        result = [r for r in result if not _is_parent(r)]

    # シリーズ Kindle を折り畳み表示するためのグループキー付与。
    # 詳細仕様 + 巻号マーカー一覧は src.server.series.extract_series_base を参照。
    from src.server.series import extract_series_base as _extract_series_base
    _DESC_SUB = re.compile(r'^\[(D01-[^/\]]+)/[^\]]+\]\s*(.+)$')

    from collections import defaultdict as _dd
    _order_groups: dict[str, dict[str, list]] = _dd(lambda: _dd(list))
    for d in result:
        d["series_group"] = None
        if d.get("bank") != "Amazon":
            continue
        m = _DESC_SUB.match(d.get("description") or "")
        if not m:
            continue
        oid, title = m.group(1), m.group(2)
        base = _extract_series_base(title)
        if base:
            _order_groups[oid][base].append(d)

    for _oid, _by_base in _order_groups.items():
        for _base, _items in _by_base.items():
            if len(_items) >= 2:
                for _d in _items:
                    _d["series_group"] = f"{_oid}::{_base}"

    _tx_cache[key] = (_time.time(), result)
    return result


def _matchable_cutoff() -> str:
    """突合可能な最新月（当月・先月を除いた先々月）を 'YYYY/MM' で返す。"""
    from datetime import date as _date
    yr, mo = _date.today().year, _date.today().month
    mo -= 2
    if mo <= 0:
        mo += 12
        yr -= 1
    return f"{yr}/{mo:02d}"


def _fetch_tx_rows(con, year: str, category: str, merge: bool, tags: str = "", exclude_tags: str = "", income: bool = False) -> list[dict]:
    # auto_categorize_* 系は読取毎に走らせる必要なし（書込・スクレイプ・起動時に集約）。
    # 旧実装では毎リクエスト約 1.7秒（returned_purchases 1.2s + from_history 0.5s）を
    # 浪費していた。POST 系で必要なら明示的に refresh_categorization() を呼ぶ。
    pass

    result = list(_fetch_base_rows(con, year, income))

    if category != "__all__":
        if category == "":
            result = [r for r in result if not r.get("category")]
        elif category == "__one_time__":
            result = [r for r in result if r.get("category") in ("今回は経費", "今回は個人支出")]
        elif category == "__経費_all__":
            result = [r for r in result if r.get("category") in ("経費", "今回は経費")]
        elif category == "__個人支出_all__":
            result = [r for r in result if r.get("category") in ("個人支出", "今回は個人支出")]
        else:
            result = [r for r in result if r.get("category") == category]
    if merge:
        result = [
            r for r in result
            if not (r["bank"] in _CARD_BANKS and r["site_matched"] > 0)
            and not r.get("_paypal_paired")
            # bank='レシート' tx は同 receipt が元経費 tx (MUFG/VPASS 等) にも
            # link 済なら重複行として隠す。 元経費 tx は receipt 紐付け表示で
            # レシート情報を見られるので、 一覧では元経費だけ残す。
            and not (r["bank"] == "レシート"
                     and (r.get("receipt_alt_link_count") or 0) > 0)
        ]

    tag_list = [t for t in tags.split(",") if t]
    excl_list = [t for t in exclude_tags.split(",") if t]

    # 突合完了月フィルタ（当月・先月を除き、先々月以前のみ）
    if "matched_months" in tag_list:
        cutoff = _matchable_cutoff()
        result = [r for r in result if r["date"][:7] <= cutoff]
        tag_list = [t for t in tag_list if t != "matched_months"]
    if "matched_months" in excl_list:
        cutoff = _matchable_cutoff()
        result = [r for r in result if r["date"][:7] > cutoff]
        excl_list = [t for t in excl_list if t != "matched_months"]

    # card:* (BANKタグ) はグループ内 OR、それ以外は AND。
    # 例) [car, card:VPASS, card:Orico] → "car" を持ち、かつ (VPASS または Orico) を持つ行
    card_tags = [t for t in tag_list if t.startswith("card:")]
    other_tags = [t for t in tag_list if not t.startswith("card:")]
    for t in other_tags:
        result = [r for r in result if t in r["tags"]]
    if card_tags:
        result = [r for r in result if any(t in r["tags"] for t in card_tags)]
    # 除外側は元から OR-NOT 動作 (どれかを含めば除外)。card:* も同じ意味で 1 件ずつ AND-NOT すれば等価。
    for t in excl_list:
        result = [r for r in result if t not in r["tags"]]
    return result


def _amazon_invoice_path(order_id: str, date_str: str) -> Path | None:
    """order_id と日付文字列（YYYY/MM/DD）から PDF ファイルパスを返す。なければ None。"""
    year = date_str[:4]
    date_file = date_str.replace("/", "-")
    p = _AMAZON_INVOICE_DIR / year / f"{date_file}_{order_id}.pdf"
    return p if p.exists() else None


def _amazon_invoice_order_ids() -> set[str]:
    return _generic_invoice_ids(_AMAZON_INVOICE_DIR)


# ショップ名 → カード明細の返金キーワード
_REFUND_KEYWORDS: dict[str, list[str]] = {
    "Amazon":           ["AMAZON"],
    "楽天市場":         ["RAKUTEN", "ラクテン"],
    "Yahoo!ショッピング": ["YAHOO"],
    "Mercari":          ["メルカリ", "MERCARI"],
}

def _get_return_tx_ids(con: sqlite3.Connection) -> set[int]:
    """
    ショップ購入 debit と同額のカード credit が 60日以内に存在し、
    かつカード明細にショップ名キーワードが含まれる場合に返品と判定する。
    購入側・返金側両方の transaction id を返す。
    """
    ids: set[int] = set()
    for bank, keywords in _REFUND_KEYWORDS.items():
        kw_cond = " OR ".join(f"r.description_normalized LIKE '%{kw}%'" for kw in keywords)
        rows = con.execute(f"""
            SELECT s.id AS shop_id, r.id AS refund_id
            FROM transactions s
            JOIN transactions r
              ON r.bank IN ('VPASS','MUFGAmex','Orico')
             AND r.credit = s.debit
             AND r.credit > 0
             AND ({kw_cond})
             AND julianday(replace(r.date,'/','-'))
               - julianday(replace(s.date,'/','-')) BETWEEN -3 AND 60
            WHERE s.bank = ? AND s.debit > 0
        """, (bank,)).fetchall()
        for row in rows:
            ids.add(row["shop_id"])
            ids.add(row["refund_id"])
    return ids


def _generic_invoice_ids(directory: Path) -> set[str]:
    """PDF ファイル名 `{date}_{id}.pdf` から id セットを返す汎用ヘルパー。"""
    ids: set[str] = set()
    if not directory.exists():
        return ids
    for pdf in directory.rglob("*.pdf"):
        name = pdf.stem
        idx = name.find("_")
        if idx > 0:
            ids.add(name[idx + 1:])
    return ids


def _campfire_invoice_ids() -> set[str]:
    """CAMPFIRE PDF ({date}_{backer_id}_{detail|fee}.pdf) から backer_id セットを返す。"""
    ids: set[str] = set()
    if not _CAMPFIRE_INVOICE_DIR.exists():
        return ids
    for pdf in _CAMPFIRE_INVOICE_DIR.rglob("*.pdf"):
        parts = pdf.stem.split("_")
        # "{YYYY-MM-DD}_{backer_id}_{suffix}" または "{YYYY-MM-DD}_{backer_id}"
        if len(parts) >= 2 and parts[1].isdigit():
            ids.add(parts[1])
    return ids


def _mercari_invoice_tx_ids() -> set[str]:
    return _generic_invoice_ids(_MERCARI_INVOICE_DIR)


# ─────────────────────────────────────────────
# 確定申告用インボイス管理
# ─────────────────────────────────────────────

def _build_tax_html() -> str:
    from src.server.templates_helper import render
    return render("tax.html")


from fastapi.responses import HTMLResponse as _HTMLResponse, RedirectResponse as _RedirectResponse

@app.get("/")
async def root():
    return _RedirectResponse(url="/ui", status_code=302)

@app.get("/ui", response_class=_HTMLResponse)
async def ui():
    return _HTMLResponse(_build_ui_html())


def _build_ui_html() -> str:
    from src.server.templates_helper import render
    return render("ui.html")


# ─────────────────────────────────────────────
# 分割されたルートモジュールの登録
# ─────────────────────────────────────────────
from src.server import routes_tax  # noqa: F401,E402
from src.server import routes_receipts  # noqa: F401,E402
from src.server import routes_tx  # noqa: F401,E402
from src.server import routes_auth  # noqa: F401,E402
from src.server import routes_health  # noqa: F401,E402
from src.server import routes_security  # noqa: F401,E402
from src.server import routes_services  # noqa: F401,E402
from src.server import routes_raw  # noqa: F401,E402
from src.server import routes_user_tags  # noqa: F401,E402
from src.server import routes_aoiro  # noqa: F401,E402
from src.server import routes_scrape  # noqa: F401,E402
from src.server import routes_tailscale  # noqa: F401,E402
from src.server import routes_users  # noqa: F401,E402


def run_server():
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
