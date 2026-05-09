"""matching.py の Phase 5 後の挙動 smoke test。

dual-write 停止後、tx_links のみに書き込まれることと、再実行で idempotent に
動くことを担保する (legacy テーブル削除によるデグレを早期検出)。
"""
from __future__ import annotations

from datetime import datetime

import pytest


def _insert_tx(con, bank, date, debit=0, desc="test", description_normalized=None):
    cur = con.execute(
        "INSERT INTO transactions (bank, date, description, description_normalized, "
        "debit, credit, fetched_at, category) VALUES (?,?,?,?,?,0,?,?)",
        (bank, date, desc, description_normalized or desc, debit,
         datetime.now().isoformat(), ""),
    )
    return cur.lastrowid


def _link_count(con, link_type):
    return con.execute(
        "SELECT COUNT(*) FROM tx_links WHERE link_type=?", (link_type,)
    ).fetchone()[0]


@pytest.fixture
def tmp_db(tmp_path):
    """run_matching が con.close() するので、:memory: ではなくファイル DB に。
    複数接続でも同じ DB を見られる。"""
    import sqlite3
    db_path = tmp_path / "test.db"
    from src.db import ensure_schema
    import src.db as _db_mod
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    _db_mod._initialized.clear()
    ensure_schema(con)
    yield db_path, con
    con.close()


def test_run_matching_writes_card_bank_billing_to_tx_links(tmp_db, monkeypatch):
    """run_matching が MUFG 振替→カード月次集計を tx_links に書き込む。
    legacy card_bank_matches テーブルは存在しない (Phase 5)。"""
    from src import matching as M
    db_path, con = tmp_db

    # MUFG の振替行 + 該当月の VPASS 明細
    mufg_id = _insert_tx(con, "MUFG", "2026/04/26", debit=20000,
                         desc="口座振替 ミツイスミトモカ－ド")
    _insert_tx(con, "VPASS", "2026/03/15", debit=10000)
    _insert_tx(con, "VPASS", "2026/03/20", debit=10000)
    con.commit()

    monkeypatch.setattr(M, "DB_PATH", db_path)

    matches = M.run_matching()
    assert len(matches) == 1
    assert matches[0].card_bank == "VPASS"

    assert _link_count(con, "card_bank_billing") == 1
    row = con.execute(
        "SELECT tx_a_id, tx_b_id FROM tx_links WHERE link_type='card_bank_billing'"
    ).fetchone()
    assert row["tx_a_id"] == mufg_id
    assert row["tx_b_id"] is None  # 月次集計なので片側 NULL

    legacy_exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='card_bank_matches'"
    ).fetchone()
    assert legacy_exists is None


def test_run_matching_idempotent(tmp_db, monkeypatch):
    """run_matching を 2 回呼んでも tx_links 件数増えない (UPDATE される)。"""
    from src import matching as M
    db_path, con = tmp_db
    _insert_tx(con, "MUFG", "2026/04/26", debit=10000, desc="口座振替 オリコ")
    _insert_tx(con, "Orico", "2026/03/15", debit=10000)
    con.commit()
    monkeypatch.setattr(M, "DB_PATH", db_path)

    M.run_matching()
    n1 = _link_count(con, "card_bank_billing")
    M.run_matching()
    n2 = _link_count(con, "card_bank_billing")
    assert n1 == n2 == 1


def test_drop_migration_removes_legacy_tables(mem_db):
    """ensure_schema が _DROPS で legacy テーブルを削除する。
    本番データでは legacy 5 テーブルがあったので DROP IF EXISTS で消える。"""
    # 旧テーブルを意図的に作って、ensure_schema が消すか確認
    mem_db.execute("CREATE TABLE IF NOT EXISTS shop_card_matches (id INTEGER)")
    mem_db.execute("CREATE TABLE IF NOT EXISTS card_bank_matches (id INTEGER)")
    mem_db.execute("CREATE TABLE IF NOT EXISTS amazon_order_card_matches (id INTEGER)")
    mem_db.execute("CREATE TABLE IF NOT EXISTS mercard_mufg_matches (id INTEGER)")
    mem_db.execute("CREATE TABLE IF NOT EXISTS mercard_mufg_tx_links (id INTEGER)")
    mem_db.commit()

    # ensure_schema を再呼出 (_initialized キャッシュをクリアして強制実行)
    from src import db as _db
    _db._initialized.clear()
    _db.ensure_schema(mem_db)

    # 5 つの legacy テーブルが全て消えてる
    for tbl in ("shop_card_matches", "card_bank_matches",
                "amazon_order_card_matches", "mercard_mufg_matches",
                "mercard_mufg_tx_links"):
        row = mem_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
        ).fetchone()
        assert row is None, f"{tbl} が DROP されていない"


def test_tx_links_orphan_reference_detection(mem_db):
    """tx_links が transaction 削除後に orphan として検出できる構造。
    /api/health の tx_links_orphans が拾うクエリと同じ条件を直接検証。"""
    # 正常な link
    a = _insert_tx(mem_db, "Amazon", "2026/04/01", debit=1000)
    b = _insert_tx(mem_db, "VPASS", "2026/04/02", debit=1000)
    mem_db.execute(
        "INSERT INTO tx_links (tx_a_id, tx_b_id, link_type) VALUES (?,?,?)",
        (a, b, "shop_card"),
    )
    # 故意に b を削除して orphan 化
    mem_db.execute("DELETE FROM transactions WHERE id=?", (b,))
    mem_db.commit()

    orphans = mem_db.execute("""
        SELECT id FROM tx_links
        WHERE (tx_a_id IS NOT NULL AND tx_a_id NOT IN (SELECT id FROM transactions))
           OR (tx_b_id IS NOT NULL AND tx_b_id NOT IN (SELECT id FROM transactions))
           OR (tx_a_id IS NULL AND tx_b_id IS NULL)
    """).fetchall()
    assert len(orphans) == 1
