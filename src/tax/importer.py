"""
青色申告データインポーター。

過去の納税/<year>/ 以下にある CSV・PDF を読み込んで SQLite に保存する。
- 仕訳帳 / 損益計算書 / 貸借対照表 / 総勘定元帳 は CSV を優先、なければ PDF
- 青色申告決算書.pdf は markitdown で Markdown 変換して tax_documents に保存
"""
import csv
import io
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from markitdown import MarkItDown

TAX_DIR = Path(__file__).parent.parent.parent / "過去の納税"
DB_PATH = Path(__file__).parent.parent.parent / "transactions.db"

_md_converter = MarkItDown()


# ─────────────────────────────────────────────
# DB セットアップ
# ─────────────────────────────────────────────

def _db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS tax_journal (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            fiscal_year     INTEGER NOT NULL,
            date            TEXT NOT NULL,
            debit_account   TEXT,
            debit_amount    INTEGER DEFAULT 0,
            credit_account  TEXT,
            credit_amount   INTEGER DEFAULT 0,
            description     TEXT,
            imported_at     TEXT,
            UNIQUE(fiscal_year, date, debit_account, debit_amount, credit_account, credit_amount, description)
        );
        CREATE TABLE IF NOT EXISTS tax_pl (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            fiscal_year     INTEGER NOT NULL,
            account         TEXT NOT NULL,
            debit_amount    INTEGER DEFAULT 0,
            credit_amount   INTEGER DEFAULT 0,
            debit_balance   INTEGER DEFAULT 0,
            credit_balance  INTEGER DEFAULT 0,
            imported_at     TEXT,
            UNIQUE(fiscal_year, account)
        );
        CREATE TABLE IF NOT EXISTS tax_bs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            fiscal_year     INTEGER NOT NULL,
            account         TEXT NOT NULL,
            debit_amount    INTEGER DEFAULT 0,
            credit_amount   INTEGER DEFAULT 0,
            debit_balance   INTEGER DEFAULT 0,
            credit_balance  INTEGER DEFAULT 0,
            imported_at     TEXT,
            UNIQUE(fiscal_year, account)
        );
        CREATE TABLE IF NOT EXISTS tax_documents (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            fiscal_year     INTEGER NOT NULL,
            document_name   TEXT NOT NULL,
            content_md      TEXT,
            source_path     TEXT,
            imported_at     TEXT,
            UNIQUE(fiscal_year, document_name)
        );
    """)
    con.commit()
    return con


# ─────────────────────────────────────────────
# 金額パース
# ─────────────────────────────────────────────

def _parse_amount(raw: str) -> int:
    cleaned = str(raw).replace(",", "").replace('"', "").strip()
    if not cleaned or not cleaned.lstrip("-").isdigit():
        return 0
    return int(cleaned)


# ─────────────────────────────────────────────
# CSV パーサー群
# ─────────────────────────────────────────────

def _parse_journal_csv(path: Path, fiscal_year: int) -> list[dict]:
    entries = []
    imported_at = datetime.now().isoformat()
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header_found = False
        for row in reader:
            if not row:
                continue
            # ヘッダ行を探す（「日付」で始まる行）
            if row[0].strip() == "日付":
                header_found = True
                continue
            if not header_found:
                continue
            if len(row) < 5 or not re.match(r"\d{4}/\d{2}/\d{2}", row[0].strip()):
                continue
            entries.append({
                "fiscal_year":    fiscal_year,
                "date":           row[0].strip(),
                "debit_account":  row[1].strip(),
                "debit_amount":   _parse_amount(row[2]),
                "credit_account": row[3].strip(),
                "credit_amount":  _parse_amount(row[4]),
                "description":    row[5].strip() if len(row) > 5 else "",
                "imported_at":    imported_at,
            })
    return entries


def _parse_pl_csv(path: Path, fiscal_year: int) -> list[dict]:
    rows = []
    imported_at = datetime.now().isoformat()
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header_found = False
        for row in reader:
            if not row:
                continue
            if row[0].strip() == "勘定科目":
                header_found = True
                continue
            if not header_found or not row[0].strip():
                continue
            if len(row) < 4:
                continue
            # 集計行（勘定科目名なし）をスキップ
            account = row[0].strip()
            if not account or account.startswith(","):
                continue
            rows.append({
                "fiscal_year":    fiscal_year,
                "account":        account,
                "debit_amount":   _parse_amount(row[1]) if len(row) > 1 else 0,
                "credit_amount":  _parse_amount(row[2]) if len(row) > 2 else 0,
                "debit_balance":  _parse_amount(row[3]) if len(row) > 3 else 0,
                "credit_balance": _parse_amount(row[4]) if len(row) > 4 else 0,
                "imported_at":    imported_at,
            })
    return rows


# 貸借対照表も同じ形式
_parse_bs_csv = _parse_pl_csv


# ─────────────────────────────────────────────
# DB 書き込み
# ─────────────────────────────────────────────

def _insert_journal(con: sqlite3.Connection, entries: list[dict]) -> int:
    before = con.execute("SELECT COUNT(*) FROM tax_journal").fetchone()[0]
    for e in entries:
        try:
            con.execute(
                "INSERT OR IGNORE INTO tax_journal "
                "(fiscal_year,date,debit_account,debit_amount,credit_account,credit_amount,description,imported_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (e["fiscal_year"], e["date"], e["debit_account"], e["debit_amount"],
                 e["credit_account"], e["credit_amount"], e["description"], e["imported_at"]),
            )
        except Exception as ex:
            print(f"  仕訳帳保存スキップ: {ex}")
    con.commit()
    after = con.execute("SELECT COUNT(*) FROM tax_journal").fetchone()[0]
    return after - before


def _insert_summary(con: sqlite3.Connection, table: str, rows: list[dict]) -> int:
    before = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    for r in rows:
        try:
            con.execute(
                f"INSERT OR REPLACE INTO {table} "
                "(fiscal_year,account,debit_amount,credit_amount,debit_balance,credit_balance,imported_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (r["fiscal_year"], r["account"], r["debit_amount"], r["credit_amount"],
                 r["debit_balance"], r["credit_balance"], r["imported_at"]),
            )
        except Exception as ex:
            print(f"  {table}保存スキップ: {ex}")
    con.commit()
    after = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return after - before


def _insert_document(con: sqlite3.Connection, fiscal_year: int, name: str, md: str, path: str) -> bool:
    try:
        con.execute(
            "INSERT OR REPLACE INTO tax_documents (fiscal_year,document_name,content_md,source_path,imported_at) "
            "VALUES (?,?,?,?,?)",
            (fiscal_year, name, md, path, datetime.now().isoformat()),
        )
        return con.total_changes > 0
    except Exception as ex:
        print(f"  tax_documents保存スキップ: {ex}")
        return False


# ─────────────────────────────────────────────
# ファイル検索ヘルパー
# ─────────────────────────────────────────────

def _find_csv(year_dir: Path, keyword: str) -> Path | None:
    for p in year_dir.glob("*.csv"):
        if keyword in p.name:
            return p
    return None


def _find_pdf(year_dir: Path, keyword: str) -> Path | None:
    for p in year_dir.glob("*.pdf"):
        if keyword in p.name:
            return p
    return None


def _fiscal_year_from_dir(year_dir: Path) -> int:
    """ディレクトリ名がそのまま会計年度（データの実際の年）"""
    return int(year_dir.name)


# ─────────────────────────────────────────────
# 1 年分のインポート
# ─────────────────────────────────────────────

def import_year(year_dir: Path, con: sqlite3.Connection) -> dict:
    fiscal_year = _fiscal_year_from_dir(year_dir)
    result = {"fiscal_year": fiscal_year, "journal": 0, "pl": 0, "bs": 0, "docs": 0}

    print(f"\n[税務] {year_dir.name}/ (会計年度 {fiscal_year}) ─────")

    # 仕訳帳
    csv_path = _find_csv(year_dir, "仕訳帳")
    if csv_path:
        entries = _parse_journal_csv(csv_path, fiscal_year)
        added = _insert_journal(con, entries)
        con.commit()
        print(f"  仕訳帳   CSV: {len(entries)} 件 (新規 {added} 件)")
        result["journal"] = len(entries)

    # 損益計算書
    csv_path = _find_csv(year_dir, "損益計算書")
    if csv_path:
        rows = _parse_pl_csv(csv_path, fiscal_year)
        added = _insert_summary(con, "tax_pl", rows)
        con.commit()
        print(f"  損益計算書 CSV: {len(rows)} 科目 (更新 {added} 件)")
        result["pl"] = len(rows)

    # 貸借対照表
    csv_path = _find_csv(year_dir, "貸借対照表")
    if csv_path:
        rows = _parse_bs_csv(csv_path, fiscal_year)
        added = _insert_summary(con, "tax_bs", rows)
        con.commit()
        print(f"  貸借対照表 CSV: {len(rows)} 科目 (更新 {added} 件)")
        result["bs"] = len(rows)

    # 青色申告決算書 PDF → Markdown として保存
    pdf_path = _find_pdf(year_dir, "青色申告決算書")
    if pdf_path:
        print(f"  青色申告決算書 PDF → Markdown 変換中...")
        try:
            md_text = _md_converter.convert(str(pdf_path)).text_content
            saved = _insert_document(con, fiscal_year, "青色申告決算書", md_text, str(pdf_path))
            con.commit()
            print(f"  青色申告決算書 {'保存' if saved else '更新なし'}: {len(md_text)} 文字")
            result["docs"] += 1
        except Exception as ex:
            print(f"  青色申告決算書変換エラー: {ex}")

    return result


# ─────────────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────────────

def run() -> None:
    if not TAX_DIR.exists():
        print(f"[税務] 納税ディレクトリが見つかりません: {TAX_DIR}")
        return

    con = _db_connect()
    totals = {"journal": 0, "pl": 0, "bs": 0, "docs": 0}

    year_dirs = sorted(d for d in TAX_DIR.iterdir() if d.is_dir() and d.name.isdigit())
    for year_dir in year_dirs:
        r = import_year(year_dir, con)
        for k in totals:
            totals[k] += r[k]

    con.close()
    print(f"\n[税務] 完了 — 仕訳 {totals['journal']} 件 / P&L {totals['pl']} 科目 / BS {totals['bs']} 科目 / 書類 {totals['docs']} 件")


if __name__ == "__main__":
    run()
