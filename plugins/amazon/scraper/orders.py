"""注文履歴ページ取得・パース・DB 保存・注文詳細＆商品リスト取得。"""
from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import datetime

from playwright.async_api import Page

from . import (
    HISTORY_YEARS,
    ORDER_HISTORY_URL,
    _db_connect,
    _extract_order_id,
)


async def fetch_order_history(page: Page) -> list[dict]:
    """直近 HISTORY_YEARS 年分の注文履歴を取得する"""
    today = datetime.now()
    years = range(today.year - HISTORY_YEARS + 1, today.year + 1)

    all_txs: list[dict] = []
    for year in sorted(years, reverse=True):
        print(f"  {year}年 取得中 ...", end=" ", flush=True)

        year_txs: list[dict] = []
        page_num = 0
        PAGE_SIZE = 10
        seen_ids: set[str] = set()
        while page_num < 100:
            url = f"{ORDER_HISTORY_URL}?timeFilter=year-{year}&startIndex={page_num * PAGE_SIZE}"
            await page.goto(url, wait_until="load", timeout=60_000)
            await asyncio.sleep(1)

            page_txs = await _parse_orders(page)
            print(f"    p{page_num+1}: {len(page_txs)}件取得 (累計{len(year_txs)+len(page_txs)}件) url={url[-30:]}", flush=True)

            if not page_txs:
                print(f"    → 空ページ検出: 終了", flush=True)
                break

            new_ids = {t["description"] for t in page_txs}
            if new_ids & seen_ids:
                print(f"    → 重複検出: ループ終了", flush=True)
                break
            seen_ids |= new_ids

            year_txs.extend(page_txs)
            page_num += 1

            last_li = page.locator("li.a-last")
            if await last_li.count() == 0:
                print(f"    → li.a-last なし: 終了", flush=True)
                break
            cls = await last_li.get_attribute("class") or ""
            if "a-disabled" in cls:
                print(f"    → a-disabled 検出: 終了", flush=True)
                break

        print(f"{len(year_txs)} 件")
        all_txs.extend(year_txs)

    return all_txs


async def _parse_orders(page: Page) -> list[dict]:
    """現在ページの注文カードを解析する"""
    transactions: list[dict] = []
    fetched_at = datetime.now().isoformat()

    orders = await page.query_selector_all(".order-card.js-order-card")

    for order in orders:
        date_raw = ""
        el = await order.query_selector(".a-column.a-span3 .a-size-base.a-color-secondary.aok-break-word")
        if el:
            date_raw = (await el.inner_text()).strip()

        # 合計金額: レイアウトにより a-span9 または a-span2 の列
        # ※ a-span12（全幅）はキャンセル済み注文で請求なし → amount_raw が空になりスキップされる
        amount_raw = ""
        for col in [".a-column.a-span9", ".a-column.a-span2"]:
            el = await order.query_selector(f"{col} .a-size-base.a-color-secondary.aok-break-word")
            if el:
                t = (await el.inner_text()).strip()
                cleaned = t.replace(",", "").replace("¥", "").replace("￥", "").strip()
                if cleaned.isdigit():
                    amount_raw = cleaned
                    break

        if not date_raw or not amount_raw:
            continue

        normalized_date = _normalize_date(date_raw)
        if not normalized_date:
            continue

        order_id = ""
        el = await order.query_selector('.yohtmlc-order-id span[dir="ltr"]')
        if el:
            order_id = (await el.inner_text()).strip()

        title = ""
        el = await order.query_selector(".yohtmlc-product-title a.a-link-normal")
        if el:
            title = (await el.inner_text()).strip()[:180]

        is_kindle = False
        for span in await order.query_selector_all("span.a-size-small.a-text-bold"):
            if "Kindle版" in (await span.inner_text()):
                is_kindle = True
                break

        if order_id and title:
            desc = f"[{order_id}] {title}"
        elif order_id:
            desc = f"[{order_id}] Amazon 注文"
        elif title:
            desc = title
        else:
            desc = "Amazon 注文"

        amount = int(amount_raw)
        transactions.append({
            "date": normalized_date,
            "debit": max(amount, 0),
            "credit": max(-amount, 0),
            "description": desc,
            "fetched_at": fetched_at,
            "is_kindle": is_kindle,
        })

    return transactions


def _normalize_date(raw: str) -> str | None:
    raw = raw.strip().replace("年", "/").replace("月", "/").replace("日", "").strip()
    for fmt in ["%Y/%m/%d", "%Y/ %m/ %d", "%Y/%m/ %d"]:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y/%m/%d")
        except ValueError:
            continue
    return None


def _parse_subtotals(text: str) -> dict:
    """#od-subtotals のinnerTextから金額内訳を抽出する"""
    result = {
        "item_subtotal": 0, "shipping": 0, "discount": 0,
        "points_used": 0, "gift_card": 0, "order_total": 0,
    }

    def yen(s: str) -> int:
        m = re.search(r"[\d,]+", s.replace("¥", "").replace("￥", "").replace(" ", ""))
        return int(m.group().replace(",", "")) if m else 0

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if "商品の小計" in line:
            result["item_subtotal"] = yen(nxt)
        elif "配送料" in line:
            v = yen(nxt)
            if v:
                result["shipping"] = v
        elif re.search(r"ポイント[：:]?$", line) and nxt.startswith("-"):
            result["points_used"] = yen(nxt)
        elif re.search(r"ギフト[券カード]*[：:]?$", line) and nxt.startswith("-"):
            result["gift_card"] = yen(nxt)
        elif re.search(r"(割引|クーポン)[：:]?$", line) and nxt.startswith("-"):
            result["discount"] += yen(nxt)
        elif re.search(r"(この注文の合計|注文合計|ご請求額)[：:]?$", line):
            v = yen(nxt)
            if v:
                result["order_total"] = v
    return result


def save_to_db(transactions: list[dict]) -> int:
    con = _db_connect()
    before = con.execute("SELECT COUNT(*) FROM transactions WHERE bank='Amazon'").fetchone()[0]
    for tx in transactions:
        try:
            # description 先頭の [order_id] が一意キー。
            # debit が後の処理（_parse_subtotals 等）で更新されるため、
            # UNIQUE(...,debit,credit) では重複検知できない。
            desc = tx["description"] or ""
            if desc.startswith("[") and "]" in desc:
                oid = desc[1:desc.index("]")]
                if oid and "/" not in oid and con.execute(
                    "SELECT 1 FROM transactions WHERE bank='Amazon' "
                    "AND description LIKE ? AND description NOT LIKE ? LIMIT 1",
                    (f"[{oid}]%", f"[{oid}/%"),
                ).fetchone():
                    continue
            con.execute(
                "INSERT OR IGNORE INTO transactions "
                "(bank, date, description, debit, credit, balance, fetched_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("Amazon", tx["date"], tx["description"],
                 int(tx["debit"] or 0), int(tx["credit"] or 0),
                 0, tx["fetched_at"]),
            )
            # Kindle注文: 詳細ページを開かずに order_details / order_items を即時登録
            if tx.get("is_kindle") and tx.get("description", "").startswith("["):
                oid = tx["description"][1:tx["description"].index("]")]
                amount = int(tx["debit"] or 0)
                title = tx["description"][len(oid) + 3:]
                con.execute(
                    "INSERT OR IGNORE INTO amazon_order_details "
                    "(order_id, order_date, item_subtotal, order_total, items_fetched, fetched_at) "
                    "VALUES (?,?,?,?,1,?)",
                    (oid, tx["date"], amount, amount, datetime.now().isoformat()),
                )
                con.execute(
                    "INSERT OR IGNORE INTO amazon_order_items (order_id, seq, title, price, is_kindle) "
                    "VALUES (?,1,?,?,1)",
                    (oid, title, amount),
                )
        except Exception as e:
            print(f"  DB保存スキップ: {e}")
    con.commit()
    after = con.execute("SELECT COUNT(*) FROM transactions WHERE bank='Amazon'").fetchone()[0]
    con.close()
    return after - before


async def _parse_order_items(page: Page, item_subtotal: int = 0) -> list[dict]:
    """注文詳細ページから注文商品リストのみをパースする。
    #od-subtotals.innerText 末尾を body.innerText のアンカーとして、その後のテキストを取得。
    item_subtotal が渡された場合、累計金額がその95%を超えた時点で打ち切る（推薦商品混入防止）。
    """
    items: list[dict] = []
    # item_subtotal=0 の注文（キャンセル済・空注文等）は商品を取らない。
    # ページ下部の推薦・スポンサー枠（「賭博堕天録 カイジ」等の固定タイル）が
    # 誤って商品として取り込まれるのを防ぐ。
    if item_subtotal == 0:
        return items
    try:
        order_text: str = await page.evaluate("""
            () => {
                const fullText = document.body.innerText;
                const sub = document.querySelector('#od-subtotals');
                if (!sub) return '';

                // subtotals innerText の末尾2行をアンカーに使って位置を特定
                const subLines = sub.innerText.trim().split('\\n').filter(l => l.trim());
                const anchor = subLines.slice(-2).map(l => l.trim()).join('\\n');
                const pos = fullText.indexOf(anchor);
                if (pos >= 0) {
                    return fullText.slice(pos + anchor.length);
                }
                return '';
            }
        """)
        if not order_text.strip():
            return items

        lines = [l.strip() for l in order_text.splitlines() if l.strip()]

        rec_markers = {"この商品を見た後", "よく一緒に購入", "スポンサー", "関連する商品",
                       "この注文を見た後", "Customers who", "Similar items",
                       "トップへ戻る", "Amazonについて",
                       # 推薦ブロック関連の追加マーカー
                       "閲覧履歴に基づくおすすめ", "閲覧履歴に基づく", "おすすめ商品",
                       "あなたへのおすすめ", "Sponsored", "PR -",
                       "履歴をクリア",
                       "過去1か月で",  # "過去1か月で100回以上閲覧されました"
                       "もう一度買う",  # 「もう一度買う表示内容を管理する」
                       "閲覧履歴",
                       "新着商品",
                       }
        cutoff = len(lines)
        for k, line in enumerate(lines):
            if any(m in line for m in rec_markers):
                cutoff = k
                break
        lines = lines[:cutoff]

        skip_prefixes = ("販売:", "発売元:", "出版:", "ブランド:", "商品レビュー",
                         "ショップ:", "配送:", "在庫:", "商品の返品", "返品または交換",
                         "返品期間", "返品・交換期間",
                         "5つ星のうち", "4つ星のうち", "3つ星のうち", "2つ星のうち", "1つ星のうち",
                         "過去1か月で",        # "過去1か月で100回以上閲覧されました"
                         "もう一度買う",        # 「もう一度買う表示内容を管理する」
                         "無料翌日配達",        # "無料翌日配達 明日X/Yにお届け"
                         "明日",               # "明日5/8にお届け"
                         "閲覧履歴に基づく",
                         )
        skip_exact = {"Kindle版", "Audible版", "デジタル", "新品", "中古品",
                      "コレクター商品", "kindle版", "商品を表示", "カートに追加",
                      "今すぐ買う", "レビューを書く", "ほしい物リストに追加",
                      "商品について質問する", "この商品について"}

        # 商品単位パース:
        # - Amazon の Kindle 詳細ページは同じ価格を 2 行連続で表示するため、
        #   直前行が同額の価格なら重複としてスキップ（list 価格 + 支払額の二重表示）
        # - 価格直前のタイトル候補は「最も近い」もの（最長ではない）を選ぶ。
        #   最長選択だと 2 桁巻番号（"...(10)..."）が 1 桁巻（"...(6)..."）より長くなり、
        #   全行が (10) に誤って紐付けられる
        cumsum = 0
        # cumsum が item_subtotal に達した時点で打ち切る。0.95 倍だと
        # 単価が小さい巻多数の Kindle で 1 巻早く止まってしまう
        # (例: 24×¥33=¥792 で 23 巻時点 ¥759 が 0.95×792=¥752 を超え誤停止)
        stop_at = item_subtotal if item_subtotal > 0 else 0
        i = 0
        prev_price: int | None = None
        while i < len(lines):
            price_m = re.match(r"^[¥￥]([\d,]+)$", lines[i])
            if price_m:
                price = int(price_m.group(1).replace(",", ""))
                # 直前行が同額の価格 → list 価格との重複なのでスキップ
                if prev_price == price:
                    prev_price = price
                    i += 1
                    continue
                prev_price = price
                if 1 <= price <= 500_000:
                    # 直近のタイトル行を1つだけ取得（最も近い候補で打ち切り）
                    best_cand = None
                    best_j = None
                    for j in range(i - 1, max(i - 20, -1), -1):
                        cand = lines[j]
                        if (len(cand) >= 5
                                and cand not in skip_exact
                                and not re.match(r"^[¥￥][\d,]+$", cand)
                                and not re.match(r"^\d+月\d+日に", cand)
                                and not any(cand.startswith(p) for p in skip_prefixes)
                                # 短い純和文（著者名等）を除外: ASCII・数字・括弧類を含まない10字以下
                                and not (len(cand) <= 10 and not re.search(r'[A-Za-z0-9\(\)\[\]【】（）「」『』・]', cand))
                                # カンマ・×区切り純CJKテキスト（著者名列挙等）を除外
                                and not (re.search(r'[,×]', cand) and not re.search(r'[A-Za-z\d\(\)\[\]【】（）「」『』]', cand))):
                            best_cand = cand
                            best_j = j
                            break  # 最も近い候補で打ち切る
                    if best_cand is not None:
                        ctx_start = max(0, best_j - 10)
                        ctx_end = min(len(lines), i + 10)
                        context = "\n".join(lines[ctx_start: ctx_end])
                        _return_markers = ("返品済み", "返金済み", "返品手続き済み",
                                           "返品受付済み", "返品処理中", "返品完了",
                                           "返品・交換済み", "返品・交換手続き済み")
                        if any(m in context for m in _return_markers):
                            i += 1
                            continue
                        is_kindle = any(k in context for k in ("Kindle", "デジタル", "電子書籍"))
                        # 数量検出: タイトル直前に "^\d{1,2}$" の単独行があれば quantity
                        page_qty = 1
                        if best_j > 0:
                            for back in range(best_j - 1, max(best_j - 4, -1), -1):
                                qm = re.match(r"^(\d{1,2})$", lines[back])
                                if qm:
                                    page_qty = int(qm.group(1))
                                    break
                                # 「商品を表示」「再度購入」を跨いで検査
                                if lines[back] not in ("商品を表示", "再度購入", "もう一度買う"):
                                    break
                        # 追加前に超過チェック: 累計が item_subtotal を超えるなら、
                        # この item は推薦タイル等の混入と判断してスキップ。
                        # 例: subtotal=¥199 で既に ¥99 取得済、次に ¥528 が来た場合 → ¥528 は推薦
                        if stop_at > 0 and cumsum + price * page_qty > stop_at + 5:
                            break
                        items.append({"title": best_cand[:180], "price": price,
                                      "is_kindle": 1 if is_kindle else 0,
                                      "page_quantity": page_qty})
                        cumsum += price * page_qty
                        if stop_at > 0 and cumsum >= stop_at:
                            break
            else:
                prev_price = None
            i += 1

    except Exception as e:
        print(f"  [Amazon] 商品リスト解析エラー: {e}")

    # 同じ (title, price) を quantity で集約する。page_quantity（ページ上に
    # 表示された数量）とパーサ重複検出（同タイトル複数ヒット）の両方を加算。
    grouped: dict[tuple, dict] = {}
    for it in items:
        key = (it["title"], it["price"])
        page_qty = it.get("page_quantity", 1)
        if key in grouped:
            grouped[key]["quantity"] += page_qty
        else:
            base = {k: v for k, v in it.items() if k != "page_quantity"}
            grouped[key] = {**base, "quantity": page_qty}
    return list(grouped.values())


async def fetch_order_details(page: Page) -> int:
    """未取得の注文詳細・商品リストを取得して保存する。保存件数を返す。"""
    con = _db_connect()
    # 旧パーサで保存された不整合データを検出して再取得対象にリセット。
    # ① items の合計（price × quantity）が item_subtotal と乖離している order
    # ② item_subtotal=0 なのに items が混入している order（推薦・スポンサー誤取得）
    # を items_fetched=0 に戻し、関連 items / 子 transaction を削除して
    # 新パーサで再取得させる。スクレイプを走らせれば自動回復する。
    bad = con.execute("""
        SELECT od.order_id FROM amazon_order_details od
        WHERE od.items_fetched = 1
          AND (
            (od.item_subtotal > 0
             AND ABS(od.item_subtotal - COALESCE(
                 (SELECT SUM(price * quantity) FROM amazon_order_items WHERE order_id = od.order_id), 0
             )) > 5)
            OR
            (od.item_subtotal = 0
             AND (SELECT COUNT(*) FROM amazon_order_items WHERE order_id = od.order_id) > 0)
          )
    """).fetchall()
    if bad:
        print(f"[Amazon] 旧パーサ不整合検出: {len(bad)} 件 → 再取得対象にリセット")
        for r in bad:
            oid = r["order_id"]
            con.execute("DELETE FROM amazon_order_items WHERE order_id=?", (oid,))
            con.execute("UPDATE amazon_order_details SET items_fetched=0 WHERE order_id=?", (oid,))
            con.execute(
                "DELETE FROM transactions WHERE bank='Amazon' AND description LIKE ?",
                (f"[{oid}/%",),
            )
        con.commit()

    tx_rows = con.execute(
        "SELECT description, date FROM transactions WHERE bank='Amazon' ORDER BY date DESC"
    ).fetchall()
    pending = []
    for tx in tx_rows:
        oid = _extract_order_id(tx["description"])
        if not oid:
            continue
        det = con.execute(
            "SELECT items_fetched FROM amazon_order_details WHERE order_id=?", (oid,)
        ).fetchone()
        if not det or not det["items_fetched"]:
            pending.append((oid, tx["date"]))

    print(f"[Amazon] 注文詳細取得: {len(pending)} 件")
    saved = 0
    for oid, order_date in pending:
        url = f"https://www.amazon.co.jp/gp/your-account/order-details?orderID={oid}"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            await asyncio.sleep(1)

            el = await page.query_selector("#od-subtotals")
            subtotal_text = (await el.inner_text()) if el else ""
            detail = _parse_subtotals(subtotal_text)

            items = await _parse_order_items(page, item_subtotal=detail["item_subtotal"])

            # 整合性チェック: items の合計が item_subtotal の 1.2倍を超える場合
            # レコメンドブロックを誤取得している可能性が高い → 商品リストを破棄して
            # items_fetched=0 のままにし、次回再試行する。
            # 過去の汚染データが残っている場合に備え amazon_order_items も全削除。
            sum_items = sum(it["price"] for it in items)
            if detail["item_subtotal"] > 0 and sum_items > detail["item_subtotal"] * 1.2:
                print(f"  [Amazon] 商品リスト誤取得疑い [{oid}]: "
                      f"items_sum=¥{sum_items:,} > item_subtotal=¥{detail['item_subtotal']:,} × 1.2 → 破棄")
                items = []
                items_fetched_flag = 0
                con.execute("DELETE FROM amazon_order_items WHERE order_id=?", (oid,))
            else:
                items_fetched_flag = 1
                # 新しい正しい商品リストで上書きするため古いものは削除
                con.execute("DELETE FROM amazon_order_items WHERE order_id=?", (oid,))

            con.execute(
                "INSERT OR REPLACE INTO amazon_order_details "
                "(order_id, order_date, item_subtotal, shipping, discount, "
                " points_used, gift_card, order_total, items_fetched, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (oid, order_date,
                 detail["item_subtotal"], detail["shipping"], detail["discount"],
                 detail["points_used"], detail["gift_card"], detail["order_total"],
                 items_fetched_flag, datetime.now().isoformat()),
            )
            for i, item in enumerate(items):
                con.execute(
                    "INSERT OR IGNORE INTO amazon_order_items "
                    "(order_id, seq, title, price, quantity, is_kindle) "
                    "VALUES (?,?,?,?,?,?)",
                    (oid, i + 1, item["title"], item["price"],
                     item.get("quantity", 1), item["is_kindle"]),
                )
            # transactions.debit 補正: order_total=0（ポイント全額払い）かつ
            # 一覧から保存された debit が item_subtotal 等になっている場合は
            # 0 に書き戻す。経費合計に誤って計上されないようにする。
            if (detail["order_total"] == 0 and detail["points_used"] > 0
                    and detail["item_subtotal"] > 0):
                con.execute(
                    "UPDATE transactions SET debit=0 "
                    "WHERE bank='Amazon' AND debit > 0 "
                    "AND description LIKE ? AND description NOT LIKE ?",
                    (f"[{oid}]%", f"[{oid}/%"),
                )
            con.commit()
            saved += 1
        except Exception as e:
            print(f"  詳細スキップ [{oid}]: {e}")
        await asyncio.sleep(1.2)

    con.close()
    print(f"[Amazon] 注文詳細保存: {saved}/{len(pending)} 件")
    return saved


def _upsert_split_row(con: sqlite3.Connection, bank: str, date: str,
                      desc: str, debit: int, category: str) -> int:
    """description を自然キーに使い、debit が変わっても重複を作らないUPSERT。
    新規挿入時は1、既存行を更新したときは0、変化無しなら0を返す。"""
    existing = con.execute(
        "SELECT id, debit FROM transactions WHERE bank=? AND description=?",
        (bank, desc),
    ).fetchone()
    if existing:
        if existing["debit"] != debit:
            con.execute(
                "UPDATE transactions SET debit=?, fetched_at=? WHERE id=?",
                (debit, datetime.now().isoformat(), existing["id"]),
            )
        return 0
    con.execute(
        "INSERT INTO transactions "
        "(bank, date, description, debit, credit, balance, fetched_at, category) "
        "VALUES (?,?,?,?,0,0,?,?)",
        (bank, date, desc, debit, datetime.now().isoformat(), category),
    )
    return 1


def split_orders(con: sqlite3.Connection) -> int:
    """複数商品注文を個別 transaction 行に分割する。
    商品行 [oid/N] と送料補完行 [oid/送料] を生成し、サブ行の合計が必ず親の debit と一致するようにする。
    再実行で商品が増減してもidempotent。
    """
    # 単一商品 ×N 個（綾鷹 ×3 箱等）は親 description に「×N」サフィックスを付与
    # して数量を可視化する。サブ行は作らない（親の debit が cash 支払額 = 真値）。
    single_qty = con.execute("""
        SELECT aoi.order_id, aoi.quantity, aoi.title
        FROM amazon_order_items aoi
        JOIN (
            SELECT order_id, COUNT(*) AS n, MAX(quantity) AS maxq
            FROM amazon_order_items GROUP BY order_id
        ) g ON g.order_id = aoi.order_id
        WHERE g.n = 1 AND g.maxq > 1
    """).fetchall()
    for sq in single_qty:
        oid, qty, title = sq[0], sq[1], sq[2]
        suffix = f" ×{qty}"
        # 旧バージョンが作った可能性のあるサブ行を削除（親に統合する方針に戻すため）
        con.execute(
            "DELETE FROM transactions WHERE bank='Amazon' AND description LIKE ?",
            (f"[{oid}/%",),
        )
        parent_row = con.execute(
            "SELECT id, description FROM transactions WHERE bank='Amazon' "
            "AND description LIKE ? AND description NOT LIKE ?",
            (f"[{oid}]%", f"[{oid}/%"),
        ).fetchone()
        if not parent_row:
            continue
        cur_desc = parent_row["description"]
        if cur_desc.endswith(suffix):
            continue
        new_desc = cur_desc.rstrip() + suffix
        con.execute(
            "UPDATE transactions SET description=? WHERE id=?",
            (new_desc[:250], parent_row["id"]),
        )

    multi = con.execute(
        "SELECT order_id FROM amazon_order_items GROUP BY order_id HAVING COUNT(*) > 1"
    ).fetchall()

    added = 0
    for row in multi:
        order_id = row[0]
        parent = con.execute(
            "SELECT date, debit, category FROM transactions "
            "WHERE bank='Amazon' AND description LIKE ? AND description NOT LIKE ?",
            (f"[{order_id}]%", f"[{order_id}/%"),
        ).fetchone()
        if not parent:
            continue
        # parent.debit=0（ポイント全額払い）の場合: 各 item を debit=0 のサブ行として
        # 作る。サブ行合計も 0 なので親と一致。商品ごとにタイトルが見える状態にする。
        if parent["debit"] == 0:
            items_zero = con.execute(
                "SELECT seq, title, price, quantity FROM amazon_order_items "
                "WHERE order_id=? ORDER BY seq",
                (order_id,),
            ).fetchall()
            if len(items_zero) <= 1:
                continue  # 1 item ならわざわざ分割しない
            expected = set()
            for item in items_zero:
                if not item["price"]:
                    continue
                qty = item["quantity"] or 1
                suffix = f" ×{qty}" if qty > 1 else ""
                desc = f"[{order_id}/{item['seq']}] {item['title']}{suffix}"[:250]
                expected.add(desc)
                added += _upsert_split_row(
                    con, "Amazon", parent["date"], desc, 0, parent["category"] or "",
                )
            # stale children を削除
            existing = con.execute(
                "SELECT id, description FROM transactions WHERE bank='Amazon' "
                "AND description LIKE ? AND description NOT LIKE ?",
                (f"[{order_id}/%", f"[{order_id}/送料]%"),
            ).fetchall()
            for c in existing:
                if c["description"] not in expected:
                    con.execute("DELETE FROM transactions WHERE id=?", (c["id"],))
            continue

        # parent.debit と items の差を埋める前に、数量取得漏れヒューリスティック:
        # 差額がいずれかの商品の単価と一致する場合、その商品の数量を +1 する
        # （「ガン玉 6B ×2 だがパーサが1つ落としてる」ようなパターンを救済）
        # ただし Kindle (D01-*) は同シリーズ複数巻のケースが多く、欠巻を別商品の数量に
        # 誤帰属するリスクがある（ビッグオーダー(7) 欠巻 → (4) ×2 と誤判定）。
        # Kindle はヒューリスティックをスキップ。
        items_raw = con.execute(
            "SELECT seq, title, price, quantity FROM amazon_order_items "
            "WHERE order_id=? ORDER BY seq",
            (order_id,),
        ).fetchall()
        is_kindle_order = order_id.startswith("D01-")
        if items_raw and not is_kindle_order:
            current_sum = sum((it["price"] or 0) * (it["quantity"] or 1) for it in items_raw)
            diff = parent["debit"] - current_sum
            # discount があれば parent.debit には含まれていないので考慮
            od = con.execute(
                "SELECT shipping, discount FROM amazon_order_details WHERE order_id=?",
                (order_id,),
            ).fetchone()
            ship = (od["shipping"] or 0) if od else 0
            disc = (od["discount"] or 0) if od else 0
            # 真の差額 = parent + discount - sub_sum - shipping
            real_gap = diff + disc - ship
            if real_gap > 0:
                # diff が item.price の倍数になっているか + 同価格の他 item と被っていないか
                # 単価が一意 (他 item と異なる) な item のみ対象にして誤帰属を避ける
                price_counts = {}
                for it in items_raw:
                    price_counts[it["price"]] = price_counts.get(it["price"], 0) + 1
                for it in items_raw:
                    p = it["price"] or 0
                    if p > 0 and real_gap % p == 0 and price_counts[p] == 1:
                        bump = real_gap // p
                        if 1 <= bump <= 5:  # 妥当な数量増分のみ
                            new_qty = (it["quantity"] or 1) + bump
                            con.execute(
                                "UPDATE amazon_order_items SET quantity=? WHERE order_id=? AND seq=?",
                                (new_qty, order_id, it["seq"]),
                            )
                            print(f"  [split] 数量補正 {order_id}/{it['seq']} qty={new_qty} ({it['title'][:30]})")
                            break
        items = con.execute(
            "SELECT seq, title, price, quantity FROM amazon_order_items "
            "WHERE order_id=? ORDER BY seq",
            (order_id,),
        ).fetchall()
        # 期待されるサブ行 description セット（quantity を suffix に含む）
        def _item_desc(item) -> str:
            qty = item["quantity"] or 1
            suffix = f" ×{qty}" if qty > 1 else ""
            return f"[{order_id}/{item['seq']}] {item['title']}{suffix}"[:250]
        expected_descs: set[str] = set()
        for item in items:
            if not item["price"]:
                continue
            expected_descs.add(_item_desc(item))
        # stale children 削除
        existing_children = con.execute(
            "SELECT id, description FROM transactions "
            "WHERE bank='Amazon' AND description LIKE ? AND description NOT LIKE ?",
            (f"[{order_id}/%", f"[{order_id}/送料]%"),
        ).fetchall()
        for c in existing_children:
            if c["description"] not in expected_descs:
                con.execute("DELETE FROM transactions WHERE id=?", (c["id"],))

        items_sum = 0
        for item in items:
            if not item["price"]:
                continue
            qty = item["quantity"] or 1
            row_total = item["price"] * qty
            desc = _item_desc(item)
            added += _upsert_split_row(
                con, "Amazon", parent["date"], desc,
                row_total, parent["category"] or "",
            )
            items_sum += row_total

        # 送料・差額補完行（item_sum < parent.debit のとき）
        diff = parent["debit"] - items_sum
        # まず古い「送料」「差額」ラベル両方を一旦削除
        for d in (f"[{order_id}/送料] 送料・その他",
                  f"[{order_id}/送料] 送料",
                  f"[{order_id}/差額] その他"):
            con.execute(
                "DELETE FROM transactions WHERE bank='Amazon' AND description=?",
                (d,),
            )
        if diff > 0:
            # shipping > 0 なら 送料 ラベル、それ以外は「その他」（取得漏れ等）
            od = con.execute(
                "SELECT shipping FROM amazon_order_details WHERE order_id=?",
                (order_id,),
            ).fetchone()
            shipping_val = (od["shipping"] or 0) if od else 0
            if shipping_val and abs(shipping_val - diff) <= 5:
                ship_desc = f"[{order_id}/送料] 送料"[:250]
            else:
                ship_desc = f"[{order_id}/差額] その他"[:250]
            added += _upsert_split_row(
                con, "Amazon", parent["date"], ship_desc,
                diff, parent["category"] or "",
            )

    con.commit()
    print(f"[Amazon] 注文分割: {added} 件追加")
    return added
