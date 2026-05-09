"""transactions.db のスナップショットを .backups/ に保存。

実行方法:
    uv run python -m scripts.backup_db
    # → .backups/snapshot_YYYY-MM-DD_HHMMSS.db.gz

オプション:
    --keep N     新しい順に N 件残して古いものを削除（既定 14）
    --no-gzip    そのままコピー（圧縮しない、書き込み速度優先）
    --copy-to-tests  tests/_local/snapshot.db に上書き（テスト用最新スナップショット）

cron / launchd で日次実行する想定。.backups/ は .gitignore に入っている。
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB = ROOT / "transactions.db"
BACKUP_DIR = ROOT / ".backups"
TEST_LOCAL = ROOT / "tests" / "_local" / "snapshot.db"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--keep", type=int, default=14, help="新しい順に残す件数")
    p.add_argument("--no-gzip", action="store_true")
    p.add_argument("--copy-to-tests", action="store_true",
                   help="tests/_local/snapshot.db にも上書き（最新を opt-in テストで使う）")
    args = p.parse_args()

    if not DB.exists():
        print(f"[backup] DB が見つかりません: {DB}")
        sys.exit(1)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    name = f"snapshot_{ts}.db" + ("" if args.no_gzip else ".gz")
    dest = BACKUP_DIR / name

    if args.no_gzip:
        shutil.copy2(DB, dest)
    else:
        with DB.open("rb") as src, gzip.open(dest, "wb") as gz:
            shutil.copyfileobj(src, gz)
    size_kb = dest.stat().st_size / 1024
    print(f"[backup] 保存: {dest.relative_to(ROOT)} ({size_kb:.0f} KB)")

    if args.copy_to_tests:
        TEST_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DB, TEST_LOCAL)
        print(f"[backup] tests/_local/snapshot.db を更新")

    # ローテーション (gzip / 非gzip 両方を ts 順で)
    backups = sorted(BACKUP_DIR.glob("snapshot_*.db*"), reverse=True)
    for old in backups[args.keep:]:
        old.unlink()
        print(f"[backup] 削除: {old.name}")


if __name__ == "__main__":
    main()
