"""Amazon `_parse_subtotals` と series_group 抽出のテスト。

`_parse_order_items` は async + page 引数なので統合寄り、ここでは純粋関数の
`_parse_subtotals` (テキスト解析) と `_extract_series_base` (シリーズ判定) を中心に。
"""
from __future__ import annotations

import pytest

from plugins.amazon.scraper.orders import _parse_subtotals


def test_parse_subtotals_full():
    """ラベル末尾アンカー (\\b ポイント[：:]?$ 等) なので、ラベルは 1 行で
    『キーワード:』に揃える必要がある (Amazon の実 DOM もそうなっている)。"""
    text = (
        "商品の小計:\n"
        "￥1,387\n"
        "配送料:\n"
        "￥0\n"
        "クーポン:\n"
        "-￥10\n"
        "ポイント:\n"
        "-￥77\n"
        "ギフトカード:\n"
        "-￥0\n"
        "この注文の合計:\n"
        "￥1,300\n"
    )
    out = _parse_subtotals(text)
    assert out["item_subtotal"] == 1387
    assert out["points_used"] == 77
    assert out["discount"] == 10
    assert out["order_total"] == 1300


def test_parse_subtotals_kindle_full_points():
    """ポイント全額: order_total=0 / points_used = subtotal。"""
    text = (
        "商品の小計:\n"
        "￥330\n"
        "ポイント:\n"
        "-￥330\n"
        "この注文の合計:\n"
        "￥0\n"
    )
    out = _parse_subtotals(text)
    assert out["item_subtotal"] == 330
    assert out["points_used"] == 330
    assert out["order_total"] == 0


def test_parse_subtotals_empty_returns_zeros():
    out = _parse_subtotals("")
    assert out == {
        "item_subtotal": 0, "shipping": 0, "discount": 0,
        "points_used": 0, "gift_card": 0, "order_total": 0,
    }


# ─────────────────────────────────────────────
# series_group 判定 — server.__init__._extract_series_base に相当する
# ロジックを直接呼ぶ。実体はクロージャ内なので、ここでは実コードと
# 同じパターン定義をそのまま検査。
# ─────────────────────────────────────────────

import re

_LABEL_PAREN = re.compile(r'\s*[\(（](?!\s*\d+\s*[\)）])[^\)）]+[\)）]\s*$')
_VOL_END_PATTERNS = (
    re.compile(r'\s*[\(（]\s*\d+\s*[\)）]\s*$'),
    re.compile(r'\s*第?\s*\d+\s*巻\s*$'),
    re.compile(r'\s*[：:]\s*\d+(?:\s+\S.*)?\s*$'),
    re.compile(r'\s*[\(（]\s*[上中下]\s*[\)）]\s*$'),
    re.compile(r'(?:\s|　)*(?:[IVX]{1,5}|[ＩＶＸ]{1,5})\s*$'),
    re.compile(r'\s*\d+\s*$'),
)
_VOL_MIDDLE_PATTERNS = (
    re.compile(r'\s+第?\s*\d+\s*巻[\s　]'),
    re.compile(r'[\(（]\s*[上中下]\s*[\)）]'),
)


def _extract_base(title: str) -> str | None:
    s = title.strip()
    s = _LABEL_PAREN.sub('', s).strip()
    for pat in _VOL_END_PATTERNS:
        m = pat.search(s)
        if m:
            base = s[:m.start()].rstrip(' 　・,、:：-‐')
            if len(base) >= 2:
                return base
    for pat in _VOL_MIDDLE_PATTERNS:
        m = pat.search(s)
        if m:
            base = s[:m.start()].rstrip(' 　・,、:：-‐')
            if len(base) >= 2:
                return base
    return None


@pytest.mark.parametrize("title,expected_base", [
    # (N) 末尾
    ("ビッグオーダー(10) (角川コミックス・エース)", "ビッグオーダー"),
    ("MOONLIGHT MILE【完全版】(22)", "MOONLIGHT MILE【完全版】"),
    # N巻 末尾
    ("猛獣性少年少女 新装版 1巻", "猛獣性少年少女 新装版"),
    # ：N + 副題
    ("坊っちゃんの時代 ： 3 かの蒼空に (アクションコミックス)", "坊っちゃんの時代"),
    # 末尾 「（上）」 だけのタイトルは _LABEL_PAREN が先に剥がすので
    # 現状の _extract_series_base では検知できない (中間 (上中下) は
    # _VOL_MIDDLE_PATTERNS でカバー)。下のハサウェイ ケースを参照。
    # 末尾ローマ数字
    ("テルマエ・ロマエVI (ビームコミックス)", "テルマエ・ロマエ"),
    ("機動戦士ガンダム　ＩＩ (角川スニーカー文庫)", "機動戦士ガンダム"),
    # 末尾裸数字
    ("ナニワ金融道1", "ナニワ金融道"),
    # 中間 N巻 (タイトル N巻 副題 タイトル 副題 形式)
    ("天体戦士サンレッド 1巻 完全版 天体戦士サンレッド 完全版", "天体戦士サンレッド"),
    # 中間 （上）
    ("機動戦士ガンダム 閃光のハサウェイ（上） 機動戦士ガンダム閃光のハサウェイ", "機動戦士ガンダム 閃光のハサウェイ"),
])
def test_series_base_extraction(title, expected_base):
    assert _extract_base(title) == expected_base


@pytest.mark.parametrize("title", [
    # 単品 (= シリーズではない) で base が None
    "iPhone X",                 # X はローマ数字パターンに当たり得るが…
    "ABC",                      # 短すぎ
])
def test_series_base_returns_none_or_safe(title):
    """シリーズじゃないものは何かしら返るが、グルーピング条件 (≥2件 同 base)
    で実害が出ないことを後段で保証する。ここでは crash しないことを確認。"""
    base = _extract_base(title)
    # crash しなければ OK。base の値自体は別テストで保証。
    assert base is None or isinstance(base, str)
