"""タグ判定と自動カテゴリ分類のロジック。"""
from __future__ import annotations

import re
import sqlite3
import unicodedata

from src.server.constants import (
    ACTIVITY_KEYWORDS,
    BOOK_KEYWORDS,
    CAR_KEYWORDS,
    CONVENIENCE_KEYWORDS,
    DRINK_KEYWORDS,
    FACILITY_KEYWORDS,
    FANTASY_KEYWORDS,
    HOMECENTER_KEYWORDS,
    INSURANCE_INCOME_KEYWORDS,
    INSURANCE_KEYWORDS,
    INVEST_KEYWORDS,
    LANDLORD_KEYWORDS,
    MEDICAL_KEYWORDS,
    OUTGOING_KEYWORDS,
    RESTAURANT_KEYWORDS,
    SALARY_KEYWORDS,
    SALES_INCOME_KEYWORDS,
    RENT_INCOME_KEYWORDS,
    SERVICE_KEYWORDS,
    STORE_TAG_BANKS,
    STORE_TAG_KEYWORDS,
    SUPERMARKET_KEYWORDS,
    SUPERMARKET_EXCLUDE_KEYWORDS,
    VPASS_REFUND_KEYWORDS,
)


def normalize_desc(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    # クレカ明細の ―（U+2015 水平バー）を ー（カタカナ長音）に統一
    return s.replace("―", "ー")


def _nk(kws: list[str]) -> list[str]:
    """description_normalized は NFKC 済 (半角英数) なので、
    constants.py の全角キーワード『ＥＮＥＯＳ』が一致しない dead code 化を防ぐため
    起動時にキーワードリストも NFKC 正規化する。"""
    return [normalize_desc(kw) for kw in kws]


# 起動時に一括 NFKC 化（参照は元のリストを書き換えず別名で持つ）
_CAR_KW         = _nk(CAR_KEYWORDS)
_BOOK_KW        = _nk(BOOK_KEYWORDS)
_DRINK_KW       = _nk(DRINK_KEYWORDS)
_CONVENIENCE_KW = _nk(CONVENIENCE_KEYWORDS)
_SUPERMARKET_KW = _nk(SUPERMARKET_KEYWORDS)
_SUPERMARKET_EXCLUDE_KW = _nk(SUPERMARKET_EXCLUDE_KEYWORDS)
_RESTAURANT_KW  = _nk(RESTAURANT_KEYWORDS)
_HOMECENTER_KW  = _nk(HOMECENTER_KEYWORDS)
_LANDLORD_KW    = _nk(LANDLORD_KEYWORDS)
_FACILITY_KW    = _nk(FACILITY_KEYWORDS)
_INVEST_KW      = _nk(INVEST_KEYWORDS)
_INSURANCE_KW   = _nk(INSURANCE_KEYWORDS)
_MEDICAL_KW     = _nk(MEDICAL_KEYWORDS)
_SERVICE_KW     = _nk(SERVICE_KEYWORDS)
_ACTIVITY_KW    = _nk(ACTIVITY_KEYWORDS)
_FANTASY_KW     = _nk(FANTASY_KEYWORDS)
_OUTGOING_KW    = _nk(OUTGOING_KEYWORDS)
_SALARY_KW      = _nk(SALARY_KEYWORDS)
_SALES_INCOME_KW    = _nk(SALES_INCOME_KEYWORDS)
_RENT_INCOME_KW     = _nk(RENT_INCOME_KEYWORDS)
_INSURANCE_INCOME_KW = _nk(INSURANCE_INCOME_KEYWORDS)
_VPASS_REFUND_KW    = _nk(VPASS_REFUND_KEYWORDS)
_STORE_TAG_KW = {shop: _nk(kws) for shop, kws in STORE_TAG_KEYWORDS.items()}


def detect_tags(description: str, bank: str, description_normalized: str = "") -> list[str]:
    tags: list[str] = []
    d = description_normalized or normalize_desc(description)
    for kw in _CAR_KW:
        if kw in d:
            tags.append("car")
            break
    if "book" not in tags:
        for kw in _BOOK_KW:
            if kw in d:
                tags.append("book")
                break
    if "book" not in tags and re.search(r'第\d+巻|\d+巻', d):
        tags.append("book")
    # "タイトル N (HARTA COMIX)" / "タイトルN (SMART COMICS)" などマンガ巻数フォーマット
    if "book" not in tags and re.search(r'\d+\s*\([A-Z][A-Z ]{2,}\)', d):
        tags.append("book")
    # Amazon の技術書・入門書: "ゼロからはじめる" / "〇〇入門" など
    if "book" not in tags and bank == "Amazon" and re.search(r'入門|ゼロからはじめる|図解|テキスト|参考書|問題集', d):
        tags.append("book")
    for kw in _DRINK_KW:
        if kw in d:
            tags.append("drink")
            break
    if "drink" not in tags:
        for kw in _CONVENIENCE_KW:
            if kw in d:
                tags.append("convenience")
                break
    for kw in _SUPERMARKET_KW:
        if kw in d:
            # 除外 keyword (例: 「ライフ日鋼」 = ガソリンスタンド) を含む description は
            # supermarket タグから除外。 部分一致による誤検出を防ぐ。
            if any(ex in d for ex in _SUPERMARKET_EXCLUDE_KW):
                break
            tags.append("supermarket")
            break
    for kw in _HOMECENTER_KW:
        if kw in d:
            tags.append("homecenter")
            break
    if "homecenter" not in tags:
        for kw in _RESTAURANT_KW:
            if kw in d:
                tags.append("restaurant")
                break
    for kw in _LANDLORD_KW:
        if kw in d:
            tags.append("landlord")
            break
    for kw in _FACILITY_KW:
        if kw in d:
            tags.append("facility")
            break
    for kw in _INVEST_KW:
        if kw in d:
            tags.append("invest")
            break
    for kw in _INSURANCE_KW:
        if kw in d:
            tags.append("insurance")
            break
    for kw in _MEDICAL_KW:
        if kw in d:
            tags.append("medical")
            break
    for kw in _SERVICE_KW:
        if kw in d:
            tags.append("service")
            break
    for kw in _ACTIVITY_KW:
        if kw in d:
            tags.append("activity")
            break
    for kw in _FANTASY_KW:
        if kw in d:
            tags.append("fantasy")
            break
    # カード明細の『オンライン決済』(無記名の Stripe 経由決済) は Stripe charge lookup で
    # マーチャント特定できるので stripe タグを付ける (UI 側で内容欄に「調査」ボタン表示)。
    if "オンライン決済" in d:
        tags.append("stripe")
    if bank in STORE_TAG_BANKS:
        tags.append(f"store:{bank}")
    # 他銀行（PayPal / VPASS 等）でもショップ識別キーワードがあれば
    # 同じ store:{shop} タグを付与してチェーン全体を一括フィルタ可能にする。
    for shop, keywords in _STORE_TAG_KW.items():
        store_tag = f"store:{shop}"
        if store_tag in tags:
            continue  # 既に銀行マッチで付与済み
        for kw in keywords:
            if kw in d:
                tags.append(store_tag)
                break
    return tags


def apply_auto_rules(con: sqlite3.Connection, invalidate_cache) -> dict[str, int]:
    """config/auto_rules.toml の全ルールを適用する (#34 / Step 3)。

    旧 auto_categorize_outgoing / auto_categorize_amazon_zero /
    auto_categorize_income / propagate_fee_categories の合計挙動を
    1 関数で実行する。複雑な auto_categorize_returned_purchases /
    auto_categorize_from_history は別関数として残す。

    keyword_resolver はキーワードリスト名 (例: 'OUTGOING') を NFKC 正規化済の
    リストに解決する (description_normalized が NFKC 済なので両方を揃える)。
    """
    from src.personal import auto_rules as _ar

    def kr(name: str) -> list[str]:
        from src import personal as _P
        raw = getattr(_P, f"{name}_KEYWORDS", [])
        return _nk(list(raw))

    return _ar.apply_all(con, invalidate_cache, keyword_resolver=kr)


# ショップ購入と同額・同窓のカード credit を返品判定するためのキーワード。
# 個人化レイヤ (config/cards.toml) から取得。
from src.personal import RETURN_KEYWORDS as _RETURN_KEYWORDS  # noqa: E402


def auto_categorize_returned_purchases(con: sqlite3.Connection, invalidate_cache) -> int:
    """同額・同店舗・60日以内のカード credit がある購入行と返金行を両方「出金」に
    自動分類する。返金が確定した買い物は経費・収入どちらにも計上せず net 0 にする。
    自動分類で付与されうる空・経費系・返金系のカテゴリのみ上書き対象とし、
    ユーザが明示的に設定した固有カテゴリ（家賃収入/給与/保険金等）は触らない。
    """
    # 自動分類で付与されうる候補（空文字含む）。ここに一致するもののみ上書き。
    overwritable = ("", "経費", "今回は経費", "個人支出", "今回は個人支出", "返金")
    placeholders = ",".join("?" * len(overwritable))
    updated = 0
    for bank, keywords in _RETURN_KEYWORDS.items():
        kw_cond = " OR ".join(f"r.description_normalized LIKE '%{kw}%'" for kw in keywords)
        rows = con.execute(f"""
            SELECT s.id AS shop_id, r.id AS refund_id
            FROM transactions s
            JOIN transactions r
              ON r.bank IN ('VPASS','MUFGAmex','Orico')
             AND r.credit = s.debit
             AND r.credit > 0
             AND ({kw_cond})
             AND julianday(replace(r.date,'/','-'))
               - julianday(replace(s.date,'/','-')) BETWEEN -3 AND 60
            WHERE s.bank = ? AND s.debit > 0
        """, (bank,)).fetchall()
        for row in rows:
            for tx_id in (row["shop_id"], row["refund_id"]):
                cur = con.execute(
                    f"UPDATE transactions SET category='出金' "
                    f"WHERE id=? AND (category IS NULL OR category IN ({placeholders}))",
                    (tx_id, *overwritable),
                )
                updated += cur.rowcount
    if updated:
        con.commit()
        invalidate_cache()
    return updated


# propagate_fee_categories は config/auto_rules.toml の [[propagate]] fee_inherit_prev
# として宣言され apply_auto_rules() で実行される (#34)


def auto_categorize_from_history(con: sqlite3.Connection, invalidate_cache) -> int:
    """過去の手動分類から (bank, description_normalized) → category マップを学習し、
    未分類の同一(bank, description)行に自動適用する。

    - 学習対象: 恒常分類のみ（経費 / 個人支出 / 出金）。
      「今回は経費」「今回は個人支出」は『その回限り』の指示なのでルール化しない。
    - 衝突したキー（同じ desc で経費と個人支出が両方手動付与）はルール化しない
    - 既にカテゴリが入っている行は触らない（手動上書きを保護）
    - description_normalized が空の行はスキップ（曖昧マッチ防止）

    クレジットカード明細・MUFG 振込先など description が安定する取引で特に効く。
    Amazon / 楽天 等で order_id を含む行は description が一意なので自動的にマッチしない。
    """
    rows = con.execute("""
        SELECT bank, description_normalized, category, COUNT(*) AS n
        FROM transactions
        WHERE category IN ('経費', '個人支出', '出金')
          AND description_normalized IS NOT NULL
          AND description_normalized != ''
        GROUP BY bank, description_normalized, category
    """).fetchall()

    from collections import defaultdict
    key_to_cats: dict[tuple, dict[str, int]] = defaultdict(dict)
    for r in rows:
        key = (r["bank"], r["description_normalized"])
        key_to_cats[key][r["category"]] = key_to_cats[key].get(r["category"], 0) + r["n"]

    # 単一カテゴリにのみ収束しているキーをルール化
    rules: dict[tuple, str] = {}
    conflicts = 0
    for key, cats in key_to_cats.items():
        if len(cats) == 1:
            rules[key] = next(iter(cats.keys()))
        else:
            conflicts += 1

    # 未分類行に適用
    updated = 0
    for (bank, desc_norm), category in rules.items():
        cur = con.execute(
            "UPDATE transactions SET category = ? "
            "WHERE bank = ? AND description_normalized = ? "
            "  AND (category IS NULL OR category = '')",
            (category, bank, desc_norm),
        )
        updated += cur.rowcount
    if updated:
        con.commit()
        invalidate_cache()
    if conflicts or updated:
        print(f"[auto_history] ルール {len(rules)} 件 (衝突 {conflicts}) → {updated} 行を分類")
    return updated


# auto_categorize_income も config/auto_rules.toml の [[keyword_match]] income_*
# として宣言され apply_auto_rules() で実行される (#34)


def detect_category_conflict_keys(
    con: sqlite3.Connection, year: str | None = None
) -> set[tuple[str, str]]:
    """同 (bank, description_normalized) で「経費」 と「個人支出」 の両方に分類された
    tx group の key 集合を返す。 仕訳ブレ発見用。

    年度指定 (year='YYYY') があれば、 その年度内のみで判定する。 たとえば
    2026 年のセルフつちうら店が全件「経費」 でも、 2025 年に「個人支出」 が
    残っている場合、 全期間判定だと 2026 年の tx も conflict 判定されてしまうため。
    UI 側では year filter と conflict 判定の対象期間を一致させる。

    「今回は経費」 「今回は個人支出」 はその回限りの意図的な分類なので学習対象から
    外す (= auto_categorize_from_history と同じ方針)。 「経費」 vs 「個人支出」 という
    安定カテゴリ同士のブレのみを発見対象にする。
    """
    sql = (
        "SELECT bank, description_normalized "
        "FROM transactions "
        "WHERE category IN ('経費', '個人支出') "
        "  AND description_normalized IS NOT NULL "
        "  AND description_normalized != '' "
    )
    params: list = []
    if year:
        sql += "  AND substr(date,1,4)=? "
        params.append(year)
    sql += (
        "GROUP BY bank, description_normalized "
        "HAVING SUM(CASE WHEN category='経費' THEN 1 ELSE 0 END) > 0 "
        "   AND SUM(CASE WHEN category='個人支出' THEN 1 ELSE 0 END) > 0"
    )
    rows = con.execute(sql, params).fetchall()
    return {(r[0], r[1]) for r in rows}
