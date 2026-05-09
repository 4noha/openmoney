"""DB スキーマと初期化の単一ソース。

すべてのスクレイパー / サーバーは sqlite3.connect() の直後に
ensure_schema(con) を呼ぶことで、必要なテーブル・カラム・インデックスが
存在することを保証する。

Idempotent。既存DBに対しては足りないカラムだけを ALTER で追加する。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional


# プロジェクト直下の transactions.db を一意に解決する
_ROOT = Path(__file__).parent.parent
DB_PATH = _ROOT / "transactions.db"


# ─────────────────────────────────────────────
# CREATE TABLE 群（IF NOT EXISTS なので idempotent）
# ─────────────────────────────────────────────

_CREATE = [
    # 全取引の中央テーブル
    """
    CREATE TABLE IF NOT EXISTS transactions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        bank        TEXT NOT NULL,
        date        TEXT NOT NULL,
        description TEXT,
        debit       INTEGER DEFAULT 0,
        credit      INTEGER DEFAULT 0,
        balance     INTEGER DEFAULT 0,
        fetched_at  TEXT,
        category    TEXT NOT NULL DEFAULT '',
        description_normalized TEXT DEFAULT '',
        auction_id  TEXT,
        UNIQUE(bank, date, description, debit, credit)
    )
    """,
    # PDF領収書（Yahoo!かんたん決済 / 取引ナビ / Amazon Kindle 等）
    """
    CREATE TABLE IF NOT EXISTS invoice_receipts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        bank            TEXT NOT NULL,
        order_id        TEXT NOT NULL,
        invoice_date    TEXT,
        order_total     INTEGER NOT NULL DEFAULT 0,
        pdf_path        TEXT,
        charged_amount  INTEGER,
        charged_card    TEXT,
        shop_tx_id      INTEGER REFERENCES transactions(id),
        card_tx_id      INTEGER REFERENCES transactions(id),
        matched_at      TEXT,
        UNIQUE(bank, order_id)
    )
    """,
    # 紙レシート OCR
    """
    CREATE TABLE IF NOT EXISTS receipts (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        filename       TEXT,
        receipt_date   TEXT,
        merchant       TEXT,
        invoice_number TEXT,
        amount         INTEGER,
        transaction_id INTEGER,
        raw_json       TEXT,
        saved_at       TEXT DEFAULT (datetime('now','localtime'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS receipt_links (
        receipt_id     INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
        transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
        PRIMARY KEY (receipt_id, transaction_id)
    )
    """,
    # 1 receipt 内の内訳行 (= 上水道料金 + 下水道料金 のように 1 枚レシートが
    # 複数の事業者 + 適格請求書番号を持つケース用)。
    # invoice_number はここに保持し、 compute_invoice_summary は receipt_items
    # があれば内訳ベース、 無ければ親 receipts から集計する。
    """
    CREATE TABLE IF NOT EXISTS receipt_items (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        receipt_id      INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
        label           TEXT,
        merchant        TEXT,
        invoice_number  TEXT,
        amount          INTEGER,
        sort_order      INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_receipt_items_receipt ON receipt_items(receipt_id)",
    # 全突合の統一テーブル (#19)
    # link_type で「どの種類の突合か」を分類:
    #   shop_card           : ショップ購入 ↔ カード明細 1 行 (1:1)
    #   card_bank_billing   : MUFG 引き落とし ↔ カード月次集計 (1:カード月)
    #   amazon_split        : Amazon 親注文 ↔ カード複数行 (1:N、各行 1 link)
    #   mercard_mufg        : メルカード月次請求 ↔ MUFG メルペイ引き落とし
    # tx_a_id / tx_b_id はどちらが NULL でも可 (月次集計は片側 NULL)。
    # 移行期間中は旧テーブル (card_bank_matches / shop_card_matches /
    # amazon_order_card_matches / mercard_mufg_matches / mercard_mufg_tx_links)
    # と二重書き込みする (Phase 2)。読取は #19 Phase 4 で切替予定。
    """
    CREATE TABLE IF NOT EXISTS tx_links (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        tx_a_id     INTEGER REFERENCES transactions(id),
        tx_b_id     INTEGER REFERENCES transactions(id),
        link_type   TEXT NOT NULL,
        diff        INTEGER DEFAULT 0,
        extra_json  TEXT,
        matched_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        UNIQUE(tx_a_id, tx_b_id, link_type)
    )
    """,
    # daemon の状態保管
    """
    CREATE TABLE IF NOT EXISTS daemon_state (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    # サービス (プラグイン) ごとの有効化状態
    """
    CREATE TABLE IF NOT EXISTS service_enabled (
        name       TEXT PRIMARY KEY,
        enabled    INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT
    )
    """,
    # transaction にメタ情報を付ける汎用 key-value テーブル。
    # 用途例: lock_source = 'povo' (= povo plugin によるカテゴリロック、 ユーザも
    # /api/transactions/{id}/category から変更不可)。 source は「誰がこのメタを
    # 書いたか」 (= povo / 手動 / matching engine 等) を識別する。
    """
    CREATE TABLE IF NOT EXISTS tx_meta (
        transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
        key            TEXT NOT NULL,
        value          TEXT,
        source         TEXT,
        created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        UNIQUE(transaction_id, key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tx_meta_tx ON tx_meta(transaction_id)",
    "CREATE INDEX IF NOT EXISTS idx_tx_meta_key ON tx_meta(key)",
    # インデックス
    "CREATE INDEX IF NOT EXISTS idx_rl_tx ON receipt_links(transaction_id)",
    "CREATE INDEX IF NOT EXISTS idx_tx_bank_debit ON transactions(bank, debit)",
    "CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date)",
    "CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category)",
    "CREATE INDEX IF NOT EXISTS idx_tx_links_a ON tx_links(tx_a_id)",
    "CREATE INDEX IF NOT EXISTS idx_tx_links_b ON tx_links(tx_b_id)",
    "CREATE INDEX IF NOT EXISTS idx_tx_links_type ON tx_links(link_type)",
]


# 既存DBへの後方互換 ALTER（カラム追加のみ）。プラグイン専用テーブルの ALTER は
# 各 plugin の ServiceSpec.extra_alters で宣言する。
_ALTERS = [
    'ALTER TABLE transactions ADD COLUMN category TEXT NOT NULL DEFAULT ""',
    'ALTER TABLE transactions ADD COLUMN description_normalized TEXT DEFAULT ""',
    "ALTER TABLE transactions ADD COLUMN auction_id TEXT",
]

# #19 Phase 5: 旧突合テーブルを完全廃止 (tx_links 統一)。
# 既存 DB で残っているテーブルがあれば DROP する (idempotent: 無ければ無視)。
_DROPS = [
    "DROP TABLE IF EXISTS card_bank_matches",
    "DROP TABLE IF EXISTS shop_card_matches",
    "DROP TABLE IF EXISTS amazon_order_card_matches",
    "DROP TABLE IF EXISTS mercard_mufg_matches",
    "DROP TABLE IF EXISTS mercard_mufg_tx_links",
]


_initialized: dict[str, bool] = {}


def ensure_schema(con: sqlite3.Connection) -> None:
    """接続単位ではなく DB ファイル単位で 1 回だけ走らせる初期化。
    sqlite3.connect() 直後に呼んで idempotent に欠けているテーブル / カラムを作る。

    コアスキーマ (`_CREATE` / `_ALTERS`) を適用したあと、プラグインレジストリを
    走査して各 ServiceSpec.extra_schema / extra_alters を適用する。
    新規プラグインは plugin.py に SQL を書くだけで自動的にテーブルが作られる。
    """
    db_path = _connection_db_path(con)
    if _initialized.get(db_path):
        return
    for ddl in _CREATE:
        con.execute(ddl)
    for alter in _ALTERS:
        try:
            con.execute(alter)
        except sqlite3.OperationalError:
            # カラムが既存 / テーブルが未作成 → 無視
            pass
    # #19 Phase 5: 旧突合テーブルを DROP (IF EXISTS なので idempotent)
    for drop in _DROPS:
        try:
            con.execute(drop)
        except sqlite3.OperationalError:
            pass
    _migrate_receipts_filename_nullable(con)
    # plugin の extra_schema / extra_alters を適用 (registry 取得は遅延 import で
    # 循環参照を回避)
    try:
        from src.plugins_loader import get_registry
        for spec in get_registry().values():
            for ddl in spec.extra_schema:
                con.execute(ddl)
            for alter in spec.extra_alters:
                try:
                    con.execute(alter)
                except sqlite3.OperationalError:
                    pass
    except Exception as e:
        # plugin 読み込み失敗時はコアスキーマだけで続行 (ログ出力)
        import sys
        print(f"[db] plugin schema 適用に失敗 (core schema は適用済み): {e}", file=sys.stderr)
    con.commit()
    _initialized[db_path] = True


def _migrate_receipts_filename_nullable(con: sqlite3.Connection) -> None:
    """receipts.filename を NULLABLE 化する一度限りの migration。

    旧 schema は filename TEXT NOT NULL で、 PDF ファイルがある「実物レシート」 のみ
    扱う想定だった。 設計ポリシー (= /tax は /ui を Pull のみ、 インボイス情報は明細に
    付ける) に従い、 PDF ファイル無しでも merchant + invoice_number だけのメタ
    レシートを receipts に登録できるようにする (= /ui で「事業者情報を付ける」 UI 用)。
    """
    cols = con.execute("PRAGMA table_info(receipts)").fetchall()
    fname_col = next((c for c in cols if c[1] == "filename"), None)
    # cols は (cid, name, type, notnull, dflt_value, pk) の tuple
    if fname_col is None or fname_col[3] == 0:
        return  # 既に NULLABLE or テーブル未作成
    print("[migrate] receipts.filename を NULLABLE 化")
    con.executescript("""
        BEGIN;
        CREATE TABLE receipts_v2 (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            filename       TEXT,
            receipt_date   TEXT,
            merchant       TEXT,
            invoice_number TEXT,
            amount         INTEGER,
            transaction_id INTEGER,
            raw_json       TEXT,
            saved_at       TEXT DEFAULT (datetime('now','localtime'))
        );
        INSERT INTO receipts_v2 (id, filename, receipt_date, merchant,
                                   invoice_number, amount, transaction_id,
                                   raw_json, saved_at)
        SELECT id, filename, receipt_date, merchant, invoice_number, amount,
               transaction_id, raw_json, saved_at FROM receipts;
        DROP TABLE receipts;
        ALTER TABLE receipts_v2 RENAME TO receipts;
        COMMIT;
    """)


def _connection_db_path(con: sqlite3.Connection) -> str:
    """同じDBファイルへの接続なら同じキーになるよう絶対パスを返す。"""
    try:
        row = con.execute("PRAGMA database_list").fetchone()
        if row:
            # PRAGMA database_list returns (seq, name, file)
            return str(row[2] or "")
    except Exception:
        pass
    return ""


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    """ensure_schema 済みの sqlite3.Connection を返す。row_factory も設定。"""
    target = Path(path) if path else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(target, timeout=30)
    con.row_factory = sqlite3.Row
    ensure_schema(con)
    return con
