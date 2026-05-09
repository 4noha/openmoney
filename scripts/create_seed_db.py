"""tests/_local/seed.db を生成する。

tests/factories.py のサンプルデータを SQLite ファイルに書き出す。
--use-real-db の代わりに、個人データを含まないテスト用 DB として使える。

使い方:
    uv run python scripts/create_seed_db.py

出力先: tests/_local/seed.db (gitignore 済み)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "tests" / "_local" / "seed.db"


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()

    import src.db as _db_mod
    from src.db import ensure_schema
    from tests.factories import seed_all

    con = sqlite3.connect(OUT)
    con.row_factory = sqlite3.Row
    _db_mod._initialized.clear()
    ensure_schema(con)
    result = seed_all(con)

    tx_count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    receipt_count = con.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]

    con.close()
    print(f"[create_seed_db] {OUT}")
    print(f"  transactions : {tx_count}")
    print(f"  receipts     : {receipt_count}")
    print(f"  シナリオ      : {list(result.keys())}")
    print()
    print("pytest --use-real-db の代わりに seed.db を使うには:")
    print("  pytest tests/ --seed-db   (conftest に --seed-db オプション追加後)")
    print("または:")
    print("  SEED_DB=tests/_local/seed.db pytest tests/")


if __name__ == "__main__":
    main()
