"""ensure_schema がコア + プラグイン extra_schema を正しく適用するかのテスト。

#29 の対応により plugin 固有テーブル (amazon_order_details / amazon_order_items /
yahoo_shopping_order_details / yahoo_shopping_order_items) は core ではなく
各プラグインの ServiceSpec.extra_schema に宣言されるようになった。
ensure_schema(con) 1 回でコア + 全プラグインのテーブルが揃うことを担保する。
"""
from __future__ import annotations


def _table_names(con) -> set[str]:
    return {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def test_core_tables_exist(mem_db):
    """コアスキーマが定義するテーブルは ensure_schema で必ず作られる。"""
    tables = _table_names(mem_db)
    for name in ("transactions", "invoice_receipts", "receipts", "receipt_links",
                 "daemon_state", "service_enabled"):
        assert name in tables, f"core テーブル {name} が無い: {tables}"


def test_plugin_extra_schema_amazon(mem_db):
    """amazon プラグインの extra_schema が適用されるか。"""
    tables = _table_names(mem_db)
    assert "amazon_order_details" in tables
    assert "amazon_order_items" in tables


def test_plugin_extra_schema_yahoo_shopping(mem_db):
    """yahoo_shopping プラグインの extra_schema が適用されるか。"""
    tables = _table_names(mem_db)
    assert "yahoo_shopping_order_details" in tables
    assert "yahoo_shopping_order_items" in tables


def test_plugin_extra_alters_amazon_columns(mem_db):
    """amazon の ALTER (items_fetched / quantity) は CREATE 時に既に存在するため
    OperationalError で握り潰されるが、最終カラムは存在する。"""
    cols_details = {
        r[1] for r in mem_db.execute("PRAGMA table_info(amazon_order_details)").fetchall()
    }
    assert "items_fetched" in cols_details
    cols_items = {
        r[1] for r in mem_db.execute("PRAGMA table_info(amazon_order_items)").fetchall()
    }
    assert "quantity" in cols_items
