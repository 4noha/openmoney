"""routes_tax._detect_payment_method の挙動テスト (#19 Phase 4-A)。

shop_card_matches → tx_links 切替後も既存の判定フローと同じ結果を返すことを確認。
"""
from __future__ import annotations

from src.server.routes_tax import _detect_payment_method
from src.matching import _upsert_tx_link


def _insert_tx(con, bank: str, date: str = "2026/04/01", debit: int = 1000,
               desc: str = "test") -> int:
    cur = con.execute(
        "INSERT INTO transactions (bank, date, description, debit, credit, fetched_at, category) "
        "VALUES (?,?,?,?,0,?,?)",
        (bank, date, desc, debit, "2026-05-08T00:00:00", ""),
    )
    return cur.lastrowid


def test_card_banks_return_card_directly(mem_db):
    """VPASS/MUFGAmex/Orico はカード扱いで早期 return (tx_links 不問)。"""
    for bank in ("VPASS", "MUFGAmex", "Orico"):
        tx = _insert_tx(mem_db, bank)
        assert _detect_payment_method(mem_db, tx, bank, has_receipt=False) == "card"


def test_mufg_returns_bank(mem_db):
    """MUFG (口座振替) は bank 扱い。"""
    tx = _insert_tx(mem_db, "MUFG")
    assert _detect_payment_method(mem_db, tx, "MUFG", has_receipt=False) == "bank"


def test_amazon_with_shop_card_link_returns_card(mem_db):
    """Amazon 注文に shop_card link があれば card 扱い。"""
    shop = _insert_tx(mem_db, "Amazon", desc="[D01-X] book")
    card = _insert_tx(mem_db, "VPASS", date="2026/04/02", desc="AMAZON 課金")
    _upsert_tx_link(mem_db, tx_a_id=shop, tx_b_id=card, link_type="shop_card")
    assert _detect_payment_method(mem_db, shop, "Amazon", has_receipt=False) == "card"


def test_amazon_without_link_no_receipt_falls_to_card(mem_db):
    """突合 link が無く、レシートも無い場合は default の card に落ちる。"""
    shop = _insert_tx(mem_db, "Amazon")
    assert _detect_payment_method(mem_db, shop, "Amazon", has_receipt=False) == "card"


def test_amazon_without_link_with_receipt_returns_receipt(mem_db):
    """link 無し + 紙レシートあり = receipt 扱い。"""
    shop = _insert_tx(mem_db, "Amazon")
    assert _detect_payment_method(mem_db, shop, "Amazon", has_receipt=True) == "receipt"


def test_receipt_bank_with_link_returns_card_not_receipt(mem_db):
    """bank='レシート' の行でも shop_card link があれば card 優先 (現金じゃない)。"""
    shop = _insert_tx(mem_db, "レシート", desc="店舗レシート")
    card = _insert_tx(mem_db, "VPASS", desc="VPASS 明細")
    _upsert_tx_link(mem_db, tx_a_id=shop, tx_b_id=card, link_type="shop_card")
    assert _detect_payment_method(mem_db, shop, "レシート", has_receipt=True) == "card"


def test_amazon_split_link_alone_does_not_count_as_card(mem_db):
    """amazon_split は legacy 互換のため shop_card と区別される (link_type='shop_card' のみ判定)。"""
    shop = _insert_tx(mem_db, "Amazon", desc="[ABC-XYZ] 注文")
    card = _insert_tx(mem_db, "VPASS", desc="AMAZON 1")
    _upsert_tx_link(mem_db, tx_a_id=shop, tx_b_id=card, link_type="amazon_split")
    # amazon_split のみだと has_receipt が無いので default の 'card' に落ちるが、
    # has_receipt=True なら 'receipt' (= shop_card と amazon_split が区別される)
    assert _detect_payment_method(mem_db, shop, "Amazon", has_receipt=True) == "receipt"
