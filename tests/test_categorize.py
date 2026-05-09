"""タグ判定 + 自動分類 (history-based) のテスト。"""
from __future__ import annotations

from datetime import datetime

from src.server.categorize import detect_tags, auto_categorize_from_history


def _noop_invalidate():
    pass


def test_detect_tags_car_keyword():
    """カナの『エネオス』も、半角『ENEOS』(全角ＥＮＥＯＳ → NFKC 後) も両方マッチ。
    constants.py 側のキーワードは起動時に NFKC 正規化される (#31 で対応)。"""
    tags = detect_tags("エネオス札幌", "VPASS", "エネオス札幌")
    assert "car" in tags
    # 全角『ＥＮＥＯＳ』はキーワード側にあるが、normalize_desc で半角化されるので
    # 入力側は半角『ENEOS』を渡す。これでもキーワードリストの全角は無効化されず一致。
    tags = detect_tags("ENEOS タチカワ", "VPASS", "ENEOS タチカワ")
    assert "car" in tags
    # 高速道路 (ETC) も car 扱い
    tags = detect_tags("ＥＴＣ首都高", "VPASS", "ETC首都高")
    assert "car" in tags


def test_detect_tags_keyword_normalization_works_both_sides():
    """全角キーワード (ENEOS / KEPCO 等) が NFKC 正規化により半角入力でも一致する。
    回帰防止: detect_tags 内のリスト参照を _CAR_KW (NFKC 済) に切替後の挙動。"""
    # 半角 (description_normalized は NFKC 後に半角になっている)
    tags = detect_tags("ENEOS", "VPASS", "ENEOS")
    assert "car" in tags
    tags = detect_tags("ENEOS Wing", "VPASS", "ENEOS Wing")
    assert "car" in tags


def test_detect_tags_book_via_kanji():
    tags = detect_tags("第10巻", "Amazon", "第10巻")
    assert "book" in tags


def test_detect_tags_aliexpress_in_chain():
    """AliExpress の VPASS 請求行 (PAYPAL *ALIPAY EUR) も store:AliExpress 付与。"""
    tags = detect_tags("PAYPAL *ALIPAY EUR (xxx)", "VPASS", "PAYPAL *ALIPAY EUR (xxx)")
    assert "store:AliExpress" in tags


def test_detect_tags_fantasy_pixiv():
    tags = detect_tags("ピクシブ株式会社", "PayPal", "ピクシブ株式会社")
    assert "fantasy" in tags


def test_detect_tags_stripe_online_payment():
    """カード明細の『オンライン決済』(無記名 Stripe 経由) は stripe タグ。
    UI 側で内容欄に Stripe charge lookup の調査ボタンを出すフラグ。"""
    tags = detect_tags("ｵﾝﾗｲﾝｹﾂｻｲ", "VPASS", "オンライン決済")
    assert "stripe" in tags
    tags = detect_tags("オンライン決済", "Orico", "オンライン決済")
    assert "stripe" in tags
    # 別のキーワード (例: マクドナルド) と混ざっても stripe 検知が独立に効く
    tags = detect_tags("オンライン決済 マクドナルド", "VPASS", "オンライン決済 マクドナルド")
    assert "stripe" in tags
    assert "restaurant" in tags  # 共存
    # 紛らわしい『オンライン振込』はマッチしないこと
    tags = detect_tags("オンライン振込", "MUFG", "オンライン振込")
    assert "stripe" not in tags


def test_detect_tags_service_keyword_lower_and_upper():
    """SERVICE_KEYWORDS は Title Case と UPPER の両方を持つ (Discord / DISCORD)。"""
    t1 = detect_tags("Discord Inc", "PayPal", "Discord Inc")
    t2 = detect_tags("PAYPAL *DISCORD (xxx)", "VPASS", "PAYPAL *DISCORD (xxx)")
    assert "service" in t1
    assert "service" in t2


def test_auto_categorize_from_history(mem_db):
    """過去の手動分類から (bank, desc) → category ルールを学習。"""
    now = datetime.now().isoformat()
    rows = [
        # ENEOS は過去に「個人支出」で 5 回分類済 → ルール
        *[("VPASS", "2025/0%d/01" % i, "ＥＮＥＯＳ タチカワ", "ENEOS タチカワ", 1500, 0, "個人支出", now) for i in range(1, 6)],
        # 同じ desc で別のカテゴリは無し → 衝突なし、ルール採用
        # 未分類の同一 ENEOS 行 → 自動分類対象
        ("VPASS", "2026/04/01", "ＥＮＥＯＳ タチカワ", "ENEOS タチカワ", 1800, 0, "", now),
        ("VPASS", "2026/05/01", "ＥＮＥＯＳ タチカワ", "ENEOS タチカワ", 1900, 0, "", now),
        # 「今回は経費」は学習対象外なのでルールにならない
        ("VPASS", "2025/02/01", "外食",       "外食",       3000, 0, "今回は経費", now),
        ("VPASS", "2026/04/01", "外食",       "外食",       3000, 0, "",          now),
        # 衝突 (経費 / 個人支出 両方) → ルールにならない
        ("VPASS", "2025/01/01", "ABC",        "ABC", 100, 0, "経費",     now),
        ("VPASS", "2025/02/01", "ABC",        "ABC", 100, 0, "個人支出", now),
        ("VPASS", "2026/04/01", "ABC",        "ABC", 100, 0, "",         now),
    ]
    for r in rows:
        mem_db.execute(
            "INSERT INTO transactions (bank, date, description, description_normalized, "
            "debit, credit, fetched_at, category) VALUES (?,?,?,?,?,?,?,?)",
            (r[0], r[1], r[2], r[3], r[4], r[5], r[7], r[6]),
        )
    mem_db.commit()

    auto_categorize_from_history(mem_db, _noop_invalidate)

    # ENEOS 未分類 2 行 → 個人支出 になる
    eneos = mem_db.execute(
        "SELECT category FROM transactions WHERE description='ＥＮＥＯＳ タチカワ' "
        "AND date LIKE '2026/%'"
    ).fetchall()
    assert all(r["category"] == "個人支出" for r in eneos)

    # 外食 未分類: ルール無し (今回は経費 はルール化されない) → 空のまま
    gaishoku = mem_db.execute(
        "SELECT category FROM transactions WHERE description='外食' AND date='2026/04/01'"
    ).fetchone()
    assert gaishoku["category"] == ""

    # ABC 未分類: 衝突あり → 空のまま
    abc = mem_db.execute(
        "SELECT category FROM transactions WHERE description='ABC' AND date='2026/04/01'"
    ).fetchone()
    assert abc["category"] == ""
