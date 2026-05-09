"""
カード明細 ↔ 銀行口座引き落としの突合、および
ショップ購入 ↔ カード個別請求の突合。

MUFG の「口座振替」行からカード会社を特定し、対応するカード明細の請求月を
推定して tx_links に link_type='card_bank_billing' で保存する。

また CAMPFIRE・Makuake・楽天市場・Amazon・PayPal・ヤフオク・紙レシート等の
個別購入をカード明細と突合し tx_links に link_type='shop_card' (1:1) または
link_type='amazon_split' (1:N) で保存する。

#19 Phase 5 で legacy 突合テーブル (card_bank_matches / shop_card_matches /
amazon_order_card_matches / mercard_mufg_matches / mercard_mufg_tx_links) は
すべて DROP 済。tx_links 1 テーブルに統一。
"""
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).parent.parent
DB_PATH = _ROOT / "transactions.db"

# MUFG 口座振替の description キーワード → card bank 名のマッピング
# 個人化レイヤから取得 (config/cards.toml + config/cards.local.toml)
from src.personal import CARD_KEYWORD_MAP  # noqa: E402,F401  (re-export for compat)


@dataclass
class Match:
    mufg_date: str
    mufg_desc: str
    mufg_amount: int
    card_bank: str
    billing_month: str       # YYYY/MM（推定）
    card_sum: int            # その月のカード合計
    diff: int                # mufg_amount - card_sum


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _parse_date(s: str) -> date:
    parts = [int(x) for x in s.split("/")]
    return date(parts[0], parts[1], parts[2])


# ─────────────────────────────────────────────
# tx_links 統一テーブルへの書き込みヘルパー (#19 Phase 2)
# 旧テーブルとの二重書き込み。読取は移行後に切替。
# ─────────────────────────────────────────────

def _upsert_tx_link(
    con: sqlite3.Connection,
    *,
    tx_a_id: int | None,
    tx_b_id: int | None,
    link_type: str,
    diff: int = 0,
    extra: dict | None = None,
) -> None:
    """tx_links に 1 行 INSERT or REPLACE。
    UNIQUE(tx_a_id, tx_b_id, link_type) なので idempotent。
    SQLite の UNIQUE は NULL を別個に扱うので、片側 NULL の場合は重複が
    発生しうる点に注意 (link_type 単位で WHERE して既存をチェックしてから挿入)。"""
    import json
    extra_json = json.dumps(extra, ensure_ascii=False) if extra else None
    if tx_a_id is None or tx_b_id is None:
        # 片側 NULL: 同 link_type で同じ tx_a_id (or tx_b_id) の既存行を更新
        non_null_col = "tx_a_id" if tx_a_id is not None else "tx_b_id"
        non_null_val = tx_a_id if tx_a_id is not None else tx_b_id
        existing = con.execute(
            f"SELECT id FROM tx_links WHERE {non_null_col}=? AND link_type=?",
            (non_null_val, link_type),
        ).fetchone()
        if existing:
            con.execute(
                "UPDATE tx_links SET diff=?, extra_json=?, matched_at=datetime('now','localtime') WHERE id=?",
                (diff, extra_json, existing[0]),
            )
            return
    con.execute(
        """INSERT OR REPLACE INTO tx_links
           (tx_a_id, tx_b_id, link_type, diff, extra_json)
           VALUES (?,?,?,?,?)""",
        (tx_a_id, tx_b_id, link_type, diff, extra_json),
    )


def _card_sum_for_month(con: sqlite3.Connection, card_bank: str, year: int, month: int) -> int:
    first = date(year, month, 1)
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    rows = con.execute(
        "SELECT SUM(debit) FROM transactions WHERE bank=? AND date BETWEEN ? AND ?",
        (card_bank, first.strftime("%Y/%m/%d"), last.strftime("%Y/%m/%d")),
    ).fetchone()
    return rows[0] or 0


def run_matching() -> list[Match]:
    """突合を実行して Match リストを返し、tx_links に保存する。
    #19 Phase 5 で legacy テーブル card_bank_matches への書込みは停止。"""
    con = _db()
    rows = con.execute(
        "SELECT date, description, debit FROM transactions WHERE bank='MUFG' AND description LIKE '%口座振替%' ORDER BY date"
    ).fetchall()

    matches: list[Match] = []
    for row in rows:
        mufg_date = row["date"]
        desc = row["description"]
        amount = row["debit"]

        card_bank = None
        for kw, cb in CARD_KEYWORD_MAP.items():
            if kw in desc:
                card_bank = cb
                break
        if not card_bank:
            continue

        pay_date = _parse_date(mufg_date)

        # 引き落とし月の 1〜2 ヶ月前を請求月候補として試す
        best: tuple[int, int, int] | None = None  # (abs_diff, year, month)
        for months_back in (1, 2):
            m = pay_date.month - months_back
            y = pay_date.year
            while m <= 0:
                m += 12
                y -= 1
            s = _card_sum_for_month(con, card_bank, y, m)
            diff = amount - s
            if best is None or abs(diff) < abs(best[0]):
                best = (diff, y, m)

        if best is None:
            continue

        diff, by, bm = best
        billing_month = f"{by}/{bm:02d}"
        card_sum = amount - diff

        m = Match(
            mufg_date=mufg_date,
            mufg_desc=desc.strip(),
            mufg_amount=amount,
            card_bank=card_bank,
            billing_month=billing_month,
            card_sum=card_sum,
            diff=diff,
        )
        matches.append(m)

        # tx_links に書込 (#19 Phase 5: legacy card_bank_matches への dual-write は停止)
        mufg_row = con.execute(
            "SELECT id FROM transactions WHERE bank='MUFG' AND date=? AND debit=? AND description=?",
            (m.mufg_date, m.mufg_amount, m.mufg_desc),
        ).fetchone()
        if mufg_row:
            _upsert_tx_link(
                con,
                tx_a_id=mufg_row[0], tx_b_id=None,
                link_type="card_bank_billing",
                diff=m.diff,
                extra={"card_bank": m.card_bank, "billing_month": m.billing_month, "card_sum": m.card_sum},
            )
    con.commit()
    con.close()
    return matches


def print_report(matches: list[Match]) -> None:
    print(f"{'引落日':<12} {'カード':<10} {'請求月':<8} {'引落額':>10} {'カード合計':>10} {'差額':>8}")
    print("-" * 65)
    seen: set[tuple] = set()
    for m in matches:
        key = (m.mufg_date, m.mufg_amount, m.card_bank)
        if key in seen:
            continue
        seen.add(key)
        diff_str = f"{m.diff:+,}" if m.diff != 0 else "一致"
        print(f"{m.mufg_date:<12} {m.card_bank:<10} {m.billing_month:<8} {m.mufg_amount:>10,} {m.card_sum:>10,} {diff_str:>8}")


# ─────────────────────────────────────────────
# ショップ購入 ↔ カード個別請求の突合
# ─────────────────────────────────────────────

# ショップ bank → (カード候補リスト, カード明細キーワード, 日付許容日数, 金額許容率)
# SHOP_CARD_MAP: ショップ × カード × 突合パラメータ
# 個人化レイヤから取得 (config/cards.toml + config/cards.local.toml)
from src.personal import SHOP_CARD_MAP  # noqa: E402,F401  (re-export for compat)


@dataclass
class ShopMatch:
    shop_bank: str
    shop_date: str
    shop_desc: str
    shop_amount: int
    card_bank: str
    card_date: str
    card_desc: str
    card_amount: int
    diff: int


def run_shop_matching() -> list[ShopMatch]:
    """ショップ購入 ↔ カード個別請求を突合して tx_links に保存する。
    #19 Phase 5: legacy shop_card_matches への dual-write は停止。
    1:1 マッチング: カード行は最多 1 ショップにしか割当てない。同じ金額の
    ショップ複数候補があっても、より近い日付・金額のものが先に取り切る。
    """
    con = _db()

    results: list[ShopMatch] = []
    # 既存マッチで使用済みのカード行を「再利用禁止」に登録 (tx_links 経由)
    used_cards: set[tuple[str, str, int]] = {
        (r["bank"], r["date"], r["debit"])
        for r in con.execute(
            "SELECT c.bank, c.date, c.debit FROM tx_links tl "
            "JOIN transactions c ON c.id = tl.tx_b_id "
            "WHERE tl.link_type='shop_card'"
        )
    }

    for shop_bank, (card_banks, keywords, day_tol, amount_tol) in SHOP_CARD_MAP.items():
        if shop_bank == "Amazon":
            # 分割サブ行（[ID/N] 形式）と Kindle D01 注文を除外し、物理商品の親注文のみ対象
            # D01- は match_kindle_bundles()→invoice_receipts で処理するため除外
            shop_rows = con.execute(
                "SELECT date, description, debit FROM transactions "
                "WHERE bank=? AND debit > 0 "
                "AND description NOT GLOB '*[[]*/[0-9]*[]]*' "
                "AND description NOT LIKE '[D01-%' ORDER BY date",
                (shop_bank,),
            ).fetchall()
        else:
            shop_rows = con.execute(
                "SELECT date, description, debit FROM transactions WHERE bank=? AND debit > 0 ORDER BY date",
                (shop_bank,),
            ).fetchall()

        for row in shop_rows:
            shop_date = row["date"]
            shop_amount = row["debit"]
            shop_desc = row["description"]

            d = _parse_date(shop_date)
            date_from = (d - timedelta(days=5)).strftime("%Y/%m/%d")
            date_to   = (d + timedelta(days=day_tol)).strftime("%Y/%m/%d")

            best: ShopMatch | None = None
            for card_bank in card_banks:
                if keywords:
                    placeholders = " OR ".join(["description_normalized LIKE ?"] * len(keywords))
                    kw_params = [f"%{kw}%" for kw in keywords]
                    card_rows = con.execute(
                        f"SELECT date, description, debit FROM transactions "
                        f"WHERE bank=? AND ({placeholders}) AND date BETWEEN ? AND ? ORDER BY date",
                        (card_bank, *kw_params, date_from, date_to),
                    ).fetchall()
                else:
                    # キーワードなし: 金額が近い行を候補にする
                    lo = int(shop_amount * (1 - amount_tol))
                    hi = int(shop_amount * (1 + amount_tol))
                    card_rows = con.execute(
                        "SELECT date, description, debit FROM transactions "
                        "WHERE bank=? AND debit BETWEEN ? AND ? AND date BETWEEN ? AND ? ORDER BY date",
                        (card_bank, lo, hi, date_from, date_to),
                    ).fetchall()

                for cr in card_rows:
                    # 既に他のショップに割当て済みなら候補から除外（1 カード行 = 1 ショップ）
                    if (card_bank, cr["date"], cr["debit"]) in used_cards:
                        continue
                    diff = shop_amount - cr["debit"]
                    if abs(diff) <= shop_amount * amount_tol:
                        if best is None or abs(diff) < abs(best.diff):
                            best = ShopMatch(
                                shop_bank=shop_bank, shop_date=shop_date,
                                shop_desc=shop_desc, shop_amount=shop_amount,
                                card_bank=card_bank, card_date=cr["date"],
                                card_desc=cr["description"], card_amount=cr["debit"],
                                diff=diff,
                            )

            if best:
                results.append(best)
                used_cards.add((best.card_bank, best.card_date, best.card_amount))
                # tx_links に書込 (#19 Phase 5: legacy shop_card_matches への dual-write 停止)
                shop_row = con.execute(
                    "SELECT id FROM transactions WHERE bank=? AND date=? AND debit=? AND description=?",
                    (best.shop_bank, best.shop_date, best.shop_amount, best.shop_desc),
                ).fetchone()
                card_row = con.execute(
                    "SELECT id FROM transactions WHERE bank=? AND date=? AND debit=? AND description=?",
                    (best.card_bank, best.card_date, best.card_amount, best.card_desc),
                ).fetchone()
                if shop_row and card_row:
                    _upsert_tx_link(
                        con,
                        tx_a_id=shop_row[0], tx_b_id=card_row[0],
                        link_type="shop_card",
                        diff=best.diff,
                    )

    con.commit()
    con.close()
    return results


# ─────────────────────────────────────────────
# Amazon 複数配送 ↔ カード合計突合（N:M マッチ）
# ─────────────────────────────────────────────

# #19 Phase 5: legacy amazon_order_card_matches は廃止、tx_links link_type='amazon_split' に統一


def _subset_sum(
    candidates: list,
    target: int,
    max_count: int = 6,
) -> list | None:
    """
    candidates の中から debit の合計が target になる部分集合を返す。
    見つからなければ None。max_count 枚以下に限定して計算量を抑える。
    """
    from itertools import combinations
    for size in range(2, min(max_count, len(candidates)) + 1):
        for combo in combinations(candidates, size):
            if sum(c["debit"] for c in combo) == target:
                return list(combo)
    return None


def run_amazon_multi_shipment_matching(day_window: int = 7, verbose: bool = True) -> int:
    """
    Amazon 注文を複数 VPASS/MUFGAmex 請求の合計で突合する（N枚対応）。
    単一カード請求で突合済みの注文は除外。

    戻り値: 新規突合した Amazon 注文件数
    """
    con = _db()
    con.row_factory = sqlite3.Row
    # _amazon_multi_shipment_table 廃止 (#19 Phase 5)

    # 未突合の Amazon 親行（分割サブ行・Kindle D01 を除く）
    parent_rows = con.execute("""
        SELECT t.id, t.date, t.description, aod.order_total
        FROM transactions t
        JOIN amazon_order_details aod
          ON aod.order_id = substr(t.description, 2, instr(t.description, ']') - 2)
        WHERE t.bank = 'Amazon'
          AND t.description NOT LIKE '[%/%]%'
          AND t.description NOT LIKE '[D01-%'
          AND aod.order_total > 0
          -- 既に shop_card / amazon_split で突合済の親注文は除外 (tx_links 経由)
          AND NOT EXISTS (
              SELECT 1 FROM tx_links tl
              WHERE tl.tx_a_id = t.id AND tl.link_type='shop_card'
          )
          AND NOT EXISTS (
              SELECT 1 FROM tx_links tl
              WHERE tl.tx_a_id = t.id AND tl.link_type='amazon_split'
          )
        ORDER BY t.date
    """).fetchall()

    # 利用可能なカード請求（Amazon キーワード付き、未突合）
    card_rows = con.execute("""
        SELECT id, bank, date, debit FROM transactions
        WHERE bank IN ('VPASS','MUFGAmex')
          AND (description_normalized LIKE '%AMAZON%' OR description_normalized LIKE '%アマゾン%')
          AND debit > 0
          -- 既に amazon_split / shop_card link が付いているカード行は除外
          AND id NOT IN (SELECT tx_b_id FROM tx_links
                         WHERE link_type='amazon_split' AND tx_b_id IS NOT NULL)
          AND NOT EXISTS (
              SELECT 1 FROM tx_links tl
              WHERE tl.tx_b_id = transactions.id AND tl.link_type='shop_card'
          )
        ORDER BY date, debit
    """).fetchall()

    used_card_ids: set[int] = set()
    matched_orders = 0

    for row in parent_rows:
        order_id = row["description"][1: row["description"].index("]")]
        order_total = row["order_total"]
        d = _parse_date(row["date"])
        date_from = (d - timedelta(days=2)).strftime("%Y/%m/%d")
        date_to   = (d + timedelta(days=day_window)).strftime("%Y/%m/%d")

        candidates = [
            c for c in card_rows
            if c["id"] not in used_card_ids and date_from <= c["date"] <= date_to
        ]
        if not candidates:
            continue

        found = _subset_sum(candidates, order_total)
        if not found:
            continue

        # 突合成立
        # Amazon 親 transaction (description LIKE '[order_id]%') を取得して tx_links の tx_a に
        amazon_parent_row = con.execute(
            "SELECT id FROM transactions WHERE bank='Amazon' AND description LIKE ? LIMIT 1",
            (f"[{order_id}]%",),
        ).fetchone()
        for c in found:
            used_card_ids.add(c["id"])
            # tx_links に書込 (#19 Phase 5: legacy amazon_order_card_matches への dual-write 停止)
            if amazon_parent_row:
                _upsert_tx_link(
                    con,
                    tx_a_id=amazon_parent_row[0], tx_b_id=c["id"],
                    link_type="amazon_split",
                    extra={"order_id": order_id, "match_type": "subset_sum"},
                )

        matched_orders += 1
        if verbose:
            charges = " + ".join(f"¥{c['debit']:,}" for c in found)
            print(f"[multi] {row['date']} {order_id[:20]} ¥{order_total:,} → {found[0]['bank']} {charges}")

    con.commit()
    con.close()
    if verbose:
        print(f"[multi] Amazon 複数配送突合: {matched_orders} 件")
    return matched_orders


def match_amazon_preorders(verbose: bool = True) -> int:
    """
    Pass 2: 日付制約なし・金額一致のみで予約注文を突合する。

    対象: 通常突合（±5日）で未マッチの VPASS/MUFGAmex Amazon 請求
    条件: order_details.order_total と完全一致する注文が1件のみ存在すること
    除外: 同額注文が複数ある場合（誤突合リスク）
    """
    con = _db()
    # _amazon_multi_shipment_table 廃止 (#19 Phase 5)

    # 未突合の Amazon カード請求（date_amount でも amount_only でも未登録）
    unmatched_charges = con.execute("""
        SELECT v.id, v.date, v.debit, v.bank
        FROM transactions v
        WHERE v.bank IN ('VPASS', 'MUFGAmex')
          AND (v.description_normalized LIKE '%AMAZON%' OR v.description_normalized LIKE '%アマゾン%')
          AND v.debit > 0
          AND NOT EXISTS (SELECT 1 FROM tx_links WHERE tx_b_id = v.id AND link_type='amazon_split')
          AND NOT EXISTS (SELECT 1 FROM tx_links WHERE tx_b_id = v.id AND link_type='shop_card')
          -- 通常マッチ（±5日）で対応するAmazon行がある場合は除外
          AND NOT EXISTS (
              SELECT 1 FROM transactions a
              WHERE a.bank = 'Amazon' AND a.debit = v.debit
                AND abs(julianday(replace(a.date,'/','-'))
                      - julianday(replace(v.date,'/','-'))) <= 5
          )
    """).fetchall()

    inserted = 0
    for charge in unmatched_charges:
        # 同額かつ未突合の Amazon 注文を検索
        candidates = con.execute("""
            SELECT aod.order_id, aod.order_date
            FROM amazon_order_details aod
            WHERE aod.order_total = ?
              -- order_id に対応する Amazon 親 transaction が tx_links で
              -- amazon_split / shop_card のどちらかで突合済なら除外
              AND NOT EXISTS (
                  SELECT 1 FROM tx_links tl
                  JOIN transactions parent ON parent.id = tl.tx_a_id
                  WHERE parent.bank='Amazon'
                  AND parent.description LIKE '['||aod.order_id||']%'
                  AND tl.link_type IN ('amazon_split','shop_card')
              )
        """, (charge["debit"],)).fetchall()

        if len(candidates) != 1:
            # 0件（DBにない注文 or Amazon Pay 外部決済）、複数件（誤突合リスク）はスキップ
            if verbose and candidates:
                print(f"[preorder] {charge['date']} ¥{charge['debit']:,} — 候補{len(candidates)}件: スキップ")
            continue

        cand = candidates[0]
        # tx_links に書込 (#19 Phase 5: legacy amazon_order_card_matches への dual-write 停止)
        amazon_parent_row = con.execute(
            "SELECT id FROM transactions WHERE bank='Amazon' AND description LIKE ? LIMIT 1",
            (f"[{cand['order_id']}]%",),
        ).fetchone()
        if amazon_parent_row:
            _upsert_tx_link(
                con,
                tx_a_id=amazon_parent_row[0], tx_b_id=charge["id"],
                link_type="amazon_split",
                extra={"order_id": cand["order_id"], "match_type": "amount_only"},
            )
        inserted += 1
        if verbose:
            print(f"[preorder] {charge['date']} ¥{charge['debit']:,} → {cand['order_id']} (注文日: {cand['order_date']})")

    con.commit()
    con.close()
    if verbose:
        print(f"[preorder] Amazon 予約注文突合: {inserted} 件")
    return inserted




def match_receipts_to_cards(verbose: bool = True, day_window: int = 5) -> int:
    """紙レシート（bank='レシート'）と同額のカード明細（VPASS/MUFGAmex/Orico）を
    日付±day_window 日以内で突合し shop_card_matches に登録する。
    既に登録済みの組み合わせは除外。
    """
    con = _db()
    # _shop_card_table 廃止 (#19 Phase 5)

    receipts = con.execute("""
        SELECT id, date, debit, description
        FROM transactions
        WHERE bank = 'レシート' AND debit > 0
          AND NOT EXISTS (
              SELECT 1 FROM tx_links tl
              WHERE tl.tx_a_id = transactions.id AND tl.link_type='shop_card'
          )
    """).fetchall()

    inserted = 0
    for r in receipts:
        d = _parse_date(r["date"])
        date_from = (d - timedelta(days=day_window)).strftime("%Y/%m/%d")
        date_to   = (d + timedelta(days=day_window)).strftime("%Y/%m/%d")

        # 同額・日付近接の未突合カード明細
        candidates = con.execute("""
            SELECT id, date, bank, description, debit
            FROM transactions
            WHERE bank IN ('VPASS','MUFGAmex','Orico')
              AND debit = ?
              AND date BETWEEN ? AND ?
              AND NOT EXISTS (
                  SELECT 1 FROM tx_links tl
                  WHERE tl.tx_b_id = transactions.id AND tl.link_type='shop_card'
              )
            ORDER BY ABS(julianday(replace(date,'/','-')) - julianday(replace(?,'/','-')))
        """, (r["debit"], date_from, date_to, r["date"])).fetchall()

        if len(candidates) != 1:
            if verbose and len(candidates) > 1:
                print(f"[receipt] {r['date']} ¥{r['debit']:,} — 候補{len(candidates)}件: スキップ")
            continue

        c = candidates[0]
        # tx_links に書込 (#19 Phase 5: legacy shop_card_matches への dual-write 停止)
        _upsert_tx_link(
            con,
            tx_a_id=r["id"], tx_b_id=c["id"],
            link_type="shop_card", diff=0,
        )
        inserted += 1
        if verbose:
            print(f"[receipt] {r['date']} ¥{r['debit']:,} ↔ {c['bank']} {c['date']}")

    con.commit()
    if verbose:
        print(f"[receipt] 紙レシート↔カード突合: {inserted} 件")
    return inserted


def print_shop_report(matches: list[ShopMatch]) -> None:
    print(f"{'ショップ':<12} {'購入日':<12} {'購入額':>10} {'カード':<8} {'請求日':<12} {'請求額':>10} {'差額':>8}")
    print("-" * 75)
    for m in matches:
        diff_str = f"{m.diff:+,}" if m.diff != 0 else "一致"
        desc = m.shop_desc[:30]
        print(f"{m.shop_bank:<12} {m.shop_date:<12} {m.shop_amount:>10,} {m.card_bank:<8} {m.card_date:<12} {m.card_amount:>10,} {diff_str:>8}  {desc}")


if __name__ == "__main__":
    results = run_matching()
    print_report(results)
    print(f"\n合計 {len({(m.mufg_date, m.mufg_amount) for m in results})} 件の引き落としを突合しました。")

    print("\n" + "=" * 75)
    print("ショップ ↔ カード個別突合")
    print("=" * 75)
    shop_results = run_shop_matching()
    print_shop_report(shop_results)
    print(f"\n合計 {len(shop_results)} 件突合しました。")

    print("\n" + "=" * 75)
    print("Amazon 予約注文突合（Pass 2: 金額一致・日付制約なし）")
    print("=" * 75)
    run_amazon_multi_shipment_matching()
    match_amazon_preorders()

    print("\n" + "=" * 75)
    print("紙レシート ↔ カード明細 突合")
    print("=" * 75)
    match_receipts_to_cards()
