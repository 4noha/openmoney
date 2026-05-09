"""aoiro_* テーブル群の DDL。冪等。

呼び出し: ``ensure_aoiro_schema(con)`` を起動時 / API 入口で 1 回。
Phase 1 でテーブルを全部作っておき、未使用テーブルは触らないだけにする。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "transactions.db"


_DDL = """
-- ─── 勘定科目マスタ ─────────────────────────────
CREATE TABLE IF NOT EXISTS aoiro_accounts (
  code        TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  type        TEXT NOT NULL CHECK(type IN ('asset','liability','equity','revenue','expense')),
  category    TEXT,
  sort_order  INTEGER DEFAULT 0,
  is_builtin  INTEGER DEFAULT 0,
  is_active   INTEGER DEFAULT 1,
  note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_aoiro_acc_type ON aoiro_accounts(type, sort_order);

-- ─── 仕訳 ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS aoiro_journal_entries (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  fiscal_year     INTEGER NOT NULL,
  date            TEXT NOT NULL,
  debit_account   TEXT NOT NULL,
  debit_amount    INTEGER NOT NULL DEFAULT 0,
  credit_account  TEXT NOT NULL,
  credit_amount   INTEGER NOT NULL DEFAULT 0,
  description     TEXT,
  source          TEXT NOT NULL DEFAULT 'manual',
  source_tx_id    INTEGER,
  source_rule_id  INTEGER,
  source_category TEXT,    -- 元 tx の category (経費/個人支出/給与/etc) — 誤判定発見用
  created_at      TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_aoiro_je_year ON aoiro_journal_entries(fiscal_year, date);
CREATE INDEX IF NOT EXISTS idx_aoiro_je_tx   ON aoiro_journal_entries(source_tx_id);
-- 集計クエリ高速化 (compute_pl/compute_bs/trial_balance/general_ledger)
CREATE INDEX IF NOT EXISTS idx_aoiro_je_year_source ON aoiro_journal_entries(fiscal_year, source);
CREATE INDEX IF NOT EXISTS idx_aoiro_je_year_debit  ON aoiro_journal_entries(fiscal_year, debit_account);
CREATE INDEX IF NOT EXISTS idx_aoiro_je_year_credit ON aoiro_journal_entries(fiscal_year, credit_account);

-- ─── 自動仕訳ルール ─────────────────────────────
-- 取引 (transactions) の category / tags / bank / keyword / 金額 を判定して
-- 借方科目 (経費区分など) を決定する。貸方科目は credit_account が
-- 空なら aoiro_payment_accounts (bank → 科目) で自動解決する。
CREATE TABLE IF NOT EXISTS aoiro_journal_rules (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT NOT NULL,
  category_match  TEXT DEFAULT '',
  tag_match       TEXT DEFAULT '',
  bank_match      TEXT DEFAULT '',
  keyword_match   TEXT DEFAULT '',
  min_amount      INTEGER DEFAULT 0,
  max_amount      INTEGER DEFAULT 0,
  debit_account   TEXT NOT NULL,
  credit_account  TEXT DEFAULT '',
  priority        INTEGER DEFAULT 100,
  is_active       INTEGER DEFAULT 1,
  business_ratio  INTEGER DEFAULT 100,
  is_builtin      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_aoiro_rules_priority ON aoiro_journal_rules(is_active, priority);

-- ─── bank → 支払/受取の既定科目マップ ───────────
-- transactions.bank ごとに「経費の貸方 (= 支払元)」と
-- 「売上の借方 (= 入金先)」の既定科目を登録。rule で credit_account が
-- 空のときの fallback に使う。
CREATE TABLE IF NOT EXISTS aoiro_payment_accounts (
  bank                  TEXT PRIMARY KEY,
  expense_credit_account TEXT NOT NULL,
  income_debit_account  TEXT,
  note                  TEXT
);

-- ─── 固定資産台帳 ───────────────────────────────
CREATE TABLE IF NOT EXISTS aoiro_fixed_assets (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  name           TEXT NOT NULL,
  acquired_date  TEXT NOT NULL,
  acquired_cost  INTEGER NOT NULL,
  useful_years   INTEGER NOT NULL DEFAULT 0,
  method         TEXT NOT NULL DEFAULT '定額',
  business_ratio INTEGER DEFAULT 100,
  salvage_value  INTEGER DEFAULT 0,
  retired_date   TEXT,
  account        TEXT NOT NULL,
  note           TEXT
);

-- ─── 期首残高 ───────────────────────────────────
CREATE TABLE IF NOT EXISTS aoiro_opening_balances (
  fiscal_year    INTEGER NOT NULL,
  account        TEXT NOT NULL,
  debit_balance  INTEGER DEFAULT 0,
  credit_balance INTEGER DEFAULT 0,
  PRIMARY KEY (fiscal_year, account)
);

-- ─── 家事按分 ───────────────────────────────────
-- 家事按分: 年度別 (毎年見直すケースに対応)
CREATE TABLE IF NOT EXISTS aoiro_business_ratios (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  fiscal_year INTEGER NOT NULL,
  scope       TEXT NOT NULL,
  ratio_pct   INTEGER NOT NULL CHECK(ratio_pct BETWEEN 0 AND 100),
  note        TEXT,
  UNIQUE (fiscal_year, scope)
);
-- INDEX は migration で作成 (fiscal_year ALTER 後)

-- ─── 所得控除 ───────────────────────────────────
CREATE TABLE IF NOT EXISTS aoiro_deductions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  fiscal_year   INTEGER NOT NULL,
  kind          TEXT NOT NULL,
  payee         TEXT,
  amount        INTEGER NOT NULL DEFAULT 0,
  evidence_path TEXT,
  note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_aoiro_ded_year ON aoiro_deductions(fiscal_year, kind);

-- ─── 不動産物件 ─────────────────────────────────
CREATE TABLE IF NOT EXISTS aoiro_real_estate_properties (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT NOT NULL,
  address         TEXT,
  acquired_date   TEXT,
  acquired_cost   INTEGER DEFAULT 0,
  is_active       INTEGER DEFAULT 1,
  match_keywords  TEXT DEFAULT '',
  note            TEXT
);
CREATE TABLE IF NOT EXISTS aoiro_real_estate_rents (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  property_id INTEGER NOT NULL,
  month       TEXT NOT NULL,
  gross_rent  INTEGER DEFAULT 0,
  vacancy     INTEGER DEFAULT 0,
  expenses    INTEGER DEFAULT 0,
  note        TEXT,
  UNIQUE(property_id, month)
);

-- ─── 申告書 B 第一表 入力値 (年度別、手入力 + 過去年度保管) ────
-- compute_declaration() に渡す parameters を年度別に永続化。
-- 現在年度: 集計実行時に保存、再起動後も維持される。
-- 過去年度: PDF (申告書等送信票) を見ながら手入力 → snapshot 化時に
--           compute_declaration がこの値で集計する。
CREATE TABLE IF NOT EXISTS aoiro_declaration_inputs (
  fiscal_year                   INTEGER PRIMARY KEY,
  salary_income                 INTEGER DEFAULT 0,
  other_income                  INTEGER DEFAULT 0,
  special_deduction_business    INTEGER DEFAULT 650000,
  special_deduction_real_estate INTEGER DEFAULT 0,
  withholding_tax               INTEGER DEFAULT 0,
  estimated_tax                 INTEGER DEFAULT 0,
  tax_credits                   INTEGER DEFAULT 0,
  business_raw_override         INTEGER,
  updated_at                    TEXT
);

-- ─── 申告 snapshot (年度ごと freeze) ────────────
-- snapshot_json: 全 section (pl/bs/journal/assets/re/deductions/declaration/
--   user_settings/opening_balances/business_ratios/rules_used) を含む JSON。
-- 旧フィールド (pl_json/bs_json/declaration_json/exports_json) は互換のため残置。
-- ─── 賃借物件 (地代家賃 = 経費側) ───────────────
-- 個人事業主がオフィス/倉庫を借りている場合の貸主・物件・契約情報。
-- 確定申告書 (青色申告決算書 3 枚目)「地代家賃の内訳」に転記する用。
-- 年度別記録: 同じ物件でも年度違えば別レコード (月額が年度で変わっても整合性を保つ)
-- 不動産所得 (大家業) 用の aoiro_real_estate_properties とは別軸。
CREATE TABLE IF NOT EXISTS aoiro_rental_contracts (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  fiscal_year          INTEGER NOT NULL,    -- 年度別 (青色申告決算書 3 枚目「本年分」)
  -- 物件情報
  property_name        TEXT NOT NULL,
  property_address     TEXT,
  area_sqm             REAL,                -- 使用面積 (m²)
  usage                TEXT,                -- 用途 (事務所/倉庫/住居/兼用 等)
  -- 業務按分 (兼用住宅の場合に何%を経費に計上するか)
  business_ratio_pct   INTEGER DEFAULT 100,
  -- 賃料・敷金 (申告書「地代家賃の内訳」 は年額のみ)
  annual_rent          INTEGER NOT NULL DEFAULT 0,   -- 本年中の賃借料・権利金等 (合計)
  monthly_rent         INTEGER NOT NULL DEFAULT 0,   -- 補助: 月額参考値 (実費 median)
  deposit              INTEGER DEFAULT 0,
  management_fee       INTEGER DEFAULT 0,   -- 共益費・管理費
  -- 貸主 (借り先) 情報
  landlord_name        TEXT,
  landlord_address     TEXT,
  landlord_phone       TEXT,
  -- メモ
  note                 TEXT,
  created_at           TEXT DEFAULT (datetime('now','localtime')),
  updated_at           TEXT
);
-- INDEX は ensure_aoiro_schema の migration 内で fiscal_year ALTER 後に作成

-- ─── 領収書/証憑ファイル ────────────────────────
-- 仕訳と紐付くファイル (PDF/画像)。実体は receipts/ ディレクトリ、
-- メタ情報のみ DB。journal_entry_id がなければ「仕訳前のドラフト」扱い。
CREATE TABLE IF NOT EXISTS aoiro_attachments (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  journal_entry_id    INTEGER,
  filename            TEXT NOT NULL,
  stored_path         TEXT NOT NULL,
  mime_type           TEXT,
  byte_size           INTEGER,
  ocr_text            TEXT,
  uploaded_at         TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_aoiro_attach_je ON aoiro_attachments(journal_entry_id);

-- ─── 仕訳テンプレート (頻用する手動仕訳の保存) ────
CREATE TABLE IF NOT EXISTS aoiro_journal_templates (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT NOT NULL,
  debit_account   TEXT NOT NULL,
  credit_account  TEXT NOT NULL,
  default_amount  INTEGER DEFAULT 0,
  default_description TEXT,
  note            TEXT,
  created_at      TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS aoiro_declaration_runs (
  fiscal_year      INTEGER PRIMARY KEY,
  locked_at        TEXT,
  pl_json          TEXT,
  bs_json          TEXT,
  declaration_json TEXT,
  exports_json     TEXT,
  snapshot_json    TEXT,
  created_at       TEXT DEFAULT (datetime('now','localtime')),
  updated_at       TEXT
);
"""


def open_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _migration_needed(con: sqlite3.Connection) -> bool:
    """既存 DB に対してこれから DDL/ALTER を走らせる必要があるか判定。
    新規列が無い・想定テーブルが無い等を検出。"""
    try:
        # business_ratios の fiscal_year (年度別化) が無ければ migration 必要
        cols = {r[1] for r in con.execute("PRAGMA table_info(aoiro_business_ratios)")}
        if cols and "fiscal_year" not in cols:
            return True
        # rental_contracts の fiscal_year / annual_rent
        rc_cols = {r[1] for r in con.execute("PRAGMA table_info(aoiro_rental_contracts)")}
        if rc_cols and ("fiscal_year" not in rc_cols or "annual_rent" not in rc_cols):
            return True
    except Exception:
        return True
    return False


def _backup_db_if_needed(con: sqlite3.Connection) -> None:
    """migration が必要な場合のみ DB をバックアップ (.backups/<timestamp>.db.gz)。
    家事按分データ消失事故の再発防止。"""
    if not _migration_needed(con):
        return
    import gzip
    import shutil
    from datetime import datetime as _dt
    backups_dir = DB_PATH.parent / ".backups"
    backups_dir.mkdir(exist_ok=True)
    ts = _dt.now().strftime("%Y-%m-%d_%H%M%S")
    dst = backups_dir / f"premigration_{ts}.db.gz"
    # WAL の最新内容をディスクに反映してから圧縮コピー
    try:
        con.execute("PRAGMA wal_checkpoint(FULL)")
    except Exception:
        pass
    try:
        with open(DB_PATH, "rb") as src, gzip.open(dst, "wb", compresslevel=6) as dst_f:
            shutil.copyfileobj(src, dst_f)
        print(f"[aoiro.schema] migration 検出 → DB をバックアップ: {dst}")
    except Exception as e:
        print(f"[aoiro.schema] backup failed: {e}")


def ensure_aoiro_schema(con: sqlite3.Connection) -> None:
    # migration が必要なら自動バックアップ (DDL の auto-commit でロールバック効かないため)
    _backup_db_if_needed(con)
    con.executescript(_DDL)
    # ─── 列追加マイグレーション (旧 schema からの移行) ───
    cols = {r[1] for r in con.execute("PRAGMA table_info(aoiro_journal_rules)")}
    if "is_builtin" not in cols:
        # 旧 schema (match_json) からの移行: テーブルを作り直す方が簡単
        con.execute("DROP TABLE aoiro_journal_rules")
        con.executescript(_DDL)  # 再 create
    # 物件マスタに match_keywords 列追加 (旧 schema からの移行)
    cols = {r[1] for r in con.execute("PRAGMA table_info(aoiro_real_estate_properties)")}
    if "match_keywords" not in cols:
        con.execute("ALTER TABLE aoiro_real_estate_properties ADD COLUMN match_keywords TEXT DEFAULT ''")
    # declaration_runs に snapshot_json/updated_at 列追加 (旧 schema からの移行)
    cols = {r[1] for r in con.execute("PRAGMA table_info(aoiro_declaration_runs)")}
    if "snapshot_json" not in cols:
        con.execute("ALTER TABLE aoiro_declaration_runs ADD COLUMN snapshot_json TEXT")
    if "updated_at" not in cols:
        con.execute("ALTER TABLE aoiro_declaration_runs ADD COLUMN updated_at TEXT")
    # declaration_inputs に business_raw_override 列追加 (CSV 無い PDF だけの年度用)
    cols = {r[1] for r in con.execute("PRAGMA table_info(aoiro_declaration_inputs)")}
    if "business_raw_override" not in cols:
        con.execute("ALTER TABLE aoiro_declaration_inputs ADD COLUMN business_raw_override INTEGER")
    # rental_contracts に fiscal_year 追加 (旧 contract_start から年を推定して既存行へ)
    cols = {r[1] for r in con.execute("PRAGMA table_info(aoiro_rental_contracts)")}
    if "fiscal_year" not in cols:
        con.execute("ALTER TABLE aoiro_rental_contracts ADD COLUMN fiscal_year INTEGER")
        for r in con.execute("SELECT id, contract_start, created_at FROM aoiro_rental_contracts").fetchall():
            yr = None
            for src in (r[1], r[2]):
                if not src:
                    continue
                m = src[:4]
                if m.isdigit():
                    yr = int(m); break
            if yr is None:
                from datetime import datetime as _dt
                yr = _dt.now().year
            con.execute("UPDATE aoiro_rental_contracts SET fiscal_year=? WHERE id=?", (yr, r[0]))
        con.execute("CREATE INDEX IF NOT EXISTS idx_aoiro_rental_year ON aoiro_rental_contracts(fiscal_year)")
    # business_ratios に fiscal_year 追加 (年度別記録)
    cols = {r[1] for r in con.execute("PRAGMA table_info(aoiro_business_ratios)")}
    if "fiscal_year" not in cols:
        # 既存テーブルを別名にして新スキーマで再作成 (UNIQUE 制約変更のため)
        con.execute("ALTER TABLE aoiro_business_ratios RENAME TO aoiro_business_ratios_old")
        con.execute("""
            CREATE TABLE aoiro_business_ratios (
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              fiscal_year INTEGER NOT NULL,
              scope       TEXT NOT NULL,
              ratio_pct   INTEGER NOT NULL CHECK(ratio_pct BETWEEN 0 AND 100),
              note        TEXT,
              UNIQUE (fiscal_year, scope)
            )
        """)
        # 既存行は当年度として保持
        from datetime import datetime as _dt
        cy = _dt.now().year
        con.execute(
            "INSERT INTO aoiro_business_ratios "
            "(fiscal_year, scope, ratio_pct, note) "
            "SELECT ?, scope, ratio_pct, note FROM aoiro_business_ratios_old",
            (cy,),
        )
        con.execute("DROP TABLE aoiro_business_ratios_old")
        con.execute("CREATE INDEX IF NOT EXISTS idx_aoiro_br_year ON aoiro_business_ratios(fiscal_year)")

    # journal_entries に source_category 追加 (誤判定発見用)
    je_cols = {r[1] for r in con.execute("PRAGMA table_info(aoiro_journal_entries)")}
    if "source_category" not in je_cols:
        con.execute("ALTER TABLE aoiro_journal_entries ADD COLUMN source_category TEXT")

    # rental_contracts に annual_rent 追加 (申告書「地代家賃の内訳」 用)
    rc_cols = {r[1] for r in con.execute("PRAGMA table_info(aoiro_rental_contracts)")}
    if "annual_rent" not in rc_cols:
        con.execute("ALTER TABLE aoiro_rental_contracts ADD COLUMN annual_rent INTEGER NOT NULL DEFAULT 0")
        # 既存行は monthly_rent * 12 で補完
        con.execute("UPDATE aoiro_rental_contracts SET annual_rent = monthly_rent * 12 WHERE annual_rent = 0")
    # INDEX (fiscal_year ALTER 後)
    con.execute("CREATE INDEX IF NOT EXISTS idx_aoiro_rental_year ON aoiro_rental_contracts(fiscal_year)")
    con.commit()
