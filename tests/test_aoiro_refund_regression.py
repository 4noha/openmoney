"""還付額リグレッションテスト

過去 5 年 (2021〜2025) の還付額が変わらないことを保証する。
本番 DB スナップショット (`tests/_local/snapshot.db`) が必要なので
`pytest --use-real-db` で実行する。

これらの値は実際の確定申告書 PDF と一致しており、コードの変更で
壊さないための snapshot test。
"""
from __future__ import annotations

import json
import sqlite3

import pytest


# 各年度の確定済還付額 (PDF 実値)
EXPECTED_REFUNDS = {
    2021: 71432,
    2022: 353317,
    2023: 404019,
    2024: 505204,
    2025: 559078,
}


@pytest.mark.real_data
@pytest.mark.parametrize("year,expected", sorted(EXPECTED_REFUNDS.items()))
def test_locked_snapshot_refund(real_db_path, year, expected):
    """ロック済 snapshot に保存された還付額が PDF 実値と一致する。"""
    con = sqlite3.connect(str(real_db_path))
    con.row_factory = sqlite3.Row
    try:
        r = con.execute(
            "SELECT declaration_json, locked_at FROM aoiro_declaration_runs "
            "WHERE fiscal_year=?",
            (year,),
        ).fetchone()
        if r is None:
            pytest.skip(f"{year} snapshot 未作成")
        if not r["locked_at"]:
            pytest.skip(f"{year} snapshot は unlock 状態 (lock 済のみ検証対象)")
        decl = json.loads(r["declaration_json"]) if r["declaration_json"] else {}
        actual = (decl.get("tax") or {}).get("final_refund", 0)
        assert actual == expected, (
            f"{year}年 還付額が変わっています: "
            f"snapshot={actual:,} / 期待値 (PDF)={expected:,}"
        )
    finally:
        con.close()


@pytest.mark.real_data
def test_pl_balance_check(real_db_path):
    """全年度で BS 検算 (資産=負債+資本) が 0 であることを保証。"""
    con = sqlite3.connect(str(real_db_path))
    con.row_factory = sqlite3.Row
    try:
        from src.aoiro.aggregate import compute_bs
        for year in EXPECTED_REFUNDS:
            bs = compute_bs(con, year)
            assert bs.get("balance_check") == 0, (
                f"{year}年 BS 検算失敗: balance_check="
                f"{bs.get('balance_check'):,}"
            )
    finally:
        con.close()


@pytest.mark.real_data
def test_no_negative_payable_2010(real_db_path):
    """全年度で 2010 未払金が借方残にならないことを保証 (= 期首未払金が
    正しく繰越されていれば 0 以上)。"""
    con = sqlite3.connect(str(real_db_path))
    con.row_factory = sqlite3.Row
    try:
        from src.aoiro.aggregate import compute_bs
        for year in EXPECTED_REFUNDS:
            bs = compute_bs(con, year)
            for line in bs.get("asset_lines", []):
                if line["code"] == "2010":
                    pytest.fail(
                        f"{year}年 2010 未払金が借方残になっています: "
                        f"ending={line['ending']:,} (期首未払金の不足)"
                    )
    finally:
        con.close()
