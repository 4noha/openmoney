"""共通 pytest fixture。

提供:
- `mem_db`           : :memory: SQLite (ensure_schema 適用済) の Connection
- `seed_db`          : mem_db にサンプルデータを投入済みの Connection
                       (factories.seed_all で MUFG/VPASS/Orico/Amazon/レシート シナリオを投入)
- `tmp_env_path`     : 一時 .env ファイル (security テスト用)
- `--use-real-db`    : 本番スナップショット (tests/_local/snapshot.db) を opt-in で読む
- `real_db_path`     : --use-real-db 指定時にだけ存在するスナップショットの絶対パス

ファイルベースの seed DB を生成する場合:
    uv run python scripts/create_seed_db.py
    → tests/_local/seed.db (gitignore 済み)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import ensure_schema  # noqa: E402
import src.db as _db_mod  # noqa: E402

LOCAL_DIR = Path(__file__).parent / "_local"
SNAPSHOT_PATH = LOCAL_DIR / "snapshot.db"


def pytest_addoption(parser):
    parser.addoption(
        "--use-real-db",
        action="store_true",
        default=False,
        help="本番スナップショット tests/_local/snapshot.db を使う real_data テストも実行",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--use-real-db"):
        return  # 全テスト走らせる
    skip_real = pytest.mark.skip(
        reason="本番スナップショット要 (`pytest --use-real-db`)"
    )
    for item in items:
        if "real_data" in item.keywords:
            item.add_marker(skip_real)


@pytest.fixture
def mem_db():
    """:memory: の clean な SQLite (Schema 適用済)。"""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    # ensure_schema は DB パス単位のキャッシュ (_initialized) を持つため、
    # :memory: ("" キー) では 2 度目以降スキップされる。テストごとに
    # キャッシュをクリアして毎回 schema を貼る。
    _db_mod._initialized.clear()
    ensure_schema(con)
    yield con
    con.close()


@pytest.fixture
def seed_db(mem_db):
    """サンプルデータ投入済みの SQLite Connection。

    投入シナリオ (tests/factories.py):
      - MUFG 口座振替 → VPASS 月次 (3 明細)
      - MUFG 口座振替 → Orico 月次 (1 明細)
      - Amazon 注文 → VPASS 複数配送 (2 カード行)
      - レシート OCR → VPASS 明細リンク
    """
    from tests.factories import seed_all
    seed_all(mem_db)
    return mem_db


@pytest.fixture
def tmp_env_path(tmp_path):
    """テスト中に書き換えていい一時 .env パス。"""
    p = tmp_path / ".env"
    p.write_text("")
    return p


@pytest.fixture
def real_db_path(request):
    """本番スナップショットへのパス。--use-real-db 指定時のみ有効、
    無ければそのテストはスキップ。"""
    if not request.config.getoption("--use-real-db"):
        pytest.skip("--use-real-db 未指定")
    if not SNAPSHOT_PATH.exists():
        pytest.skip(f"snapshot 未配置: {SNAPSHOT_PATH}")
    return SNAPSHOT_PATH
