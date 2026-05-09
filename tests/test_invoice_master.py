"""invoice_master loader (#35 / Step 4) のテスト。

config/invoice_master.toml + invoice_master.local.toml を読み込み、
invoice_vendors テーブルに INSERT OR IGNORE で取り込む挙動を確認する。
既存 service_name の user 編集を保護することが最重要。
"""
from __future__ import annotations

from pathlib import Path

from src.personal import invoice_master as IM


def _make_invoice_vendors(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS invoice_vendors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_name TEXT NOT NULL DEFAULT '',
            company_name TEXT NOT NULL DEFAULT '',
            invoice_number TEXT NOT NULL DEFAULT ''
        )
    """)
    con.commit()


def _all_vendors(con):
    return [dict(r) for r in con.execute(
        "SELECT service_name, company_name, invoice_number FROM invoice_vendors ORDER BY service_name"
    ).fetchall()]


def test_load_master_vendors_returns_list():
    """本番 config/invoice_master.toml が読める + 各エントリが service_name を持つ。"""
    vs = IM.load_master_vendors()
    assert isinstance(vs, list)
    assert any(v.get("service_name") == "Amazon" for v in vs)
    for v in vs:
        if v.get("service_name") == "Amazon":
            assert v.get("invoice_number", "").startswith("T")
            assert "アマゾン" in v.get("company_name", "")


def test_sync_inserts_into_empty_table(mem_db):
    """空テーブルに対し master をフル取込。"""
    _make_invoice_vendors(mem_db)
    n = IM.sync_invoice_vendors(mem_db)
    assert n > 0
    rows = _all_vendors(mem_db)
    names = {r["service_name"] for r in rows}
    assert "Amazon" in names
    assert "Mercari" in names
    assert "楽天市場" in names


def test_sync_idempotent(mem_db):
    """2 回目の sync は INSERT 件数 0 (重複しない)。"""
    _make_invoice_vendors(mem_db)
    n1 = IM.sync_invoice_vendors(mem_db)
    n2 = IM.sync_invoice_vendors(mem_db)
    assert n1 > 0
    assert n2 == 0
    # 件数も増えない
    rows1 = _all_vendors(mem_db)
    rows2 = _all_vendors(mem_db)
    assert len(rows1) == len(rows2)


def test_sync_does_not_overwrite_existing_user_edit(mem_db):
    """user が UI で company_name / invoice_number を変更した行は上書きされない。"""
    _make_invoice_vendors(mem_db)
    mem_db.execute(
        "INSERT INTO invoice_vendors (service_name, company_name, invoice_number) "
        "VALUES (?, ?, ?)",
        ("Amazon", "ユーザ修正名", "T9999999999999"),
    )
    mem_db.commit()
    n = IM.sync_invoice_vendors(mem_db)
    # Amazon は既存なのでスキップされ、それ以外が新規 INSERT
    row = mem_db.execute(
        "SELECT company_name, invoice_number FROM invoice_vendors WHERE service_name='Amazon'"
    ).fetchone()
    assert row["company_name"] == "ユーザ修正名"
    assert row["invoice_number"] == "T9999999999999"
    # それ以外 (Mercari 等) は取り込まれている
    rows = _all_vendors(mem_db)
    names = {r["service_name"] for r in rows}
    assert "Mercari" in names


def test_sync_handles_empty_service_name(mem_db, monkeypatch):
    """service_name が空の vendor は INSERT されない。"""
    _make_invoice_vendors(mem_db)
    monkeypatch.setattr(IM, "load_master_vendors", lambda: [
        {"service_name": "", "company_name": "X", "invoice_number": "T1"},
        {"service_name": "Valid", "company_name": "C", "invoice_number": "T2"},
    ])
    n = IM.sync_invoice_vendors(mem_db)
    assert n == 1
    rows = _all_vendors(mem_db)
    assert [r["service_name"] for r in rows] == ["Valid"]


def test_sync_no_master_files(mem_db, monkeypatch):
    """master ファイルが空 / 存在しない場合は 0 件で正常終了。"""
    _make_invoice_vendors(mem_db)
    monkeypatch.setattr(IM, "load_master_vendors", lambda: [])
    n = IM.sync_invoice_vendors(mem_db)
    assert n == 0
