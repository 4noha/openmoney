"""自動仕訳ルール (aoiro_journal_rules) と bank → 科目マップ
(aoiro_payment_accounts) の seed + CRUD + マッチング。

エンジン (engine.py) が rules を priority 順に評価し、最初に
マッチしたルールの debit_account / credit_account を採用する。
credit_account が空なら aoiro_payment_accounts で bank → 科目を解決。
"""
from __future__ import annotations

import sqlite3

# ─────────────────────────────────────────────
# builtin rules (Phase 2 デフォルト)
# - tag 判定は detect_tags() の出力を前提
# - category は categorize.py の auto_classify 出力を前提 (経費 / 売上 / 給与 / 個人支出 / 出金 / 返金 / 保険金)
# - 個人支出: カード払いを未払金経由で正しく追跡するため仕訳化対象 (借方=事業主貸)
# - 出金 / 返金 / 保険金 は事業仕訳の対象外なのでルールを置かない
# ─────────────────────────────────────────────
# (name, category_match, tag_match, bank_match, keyword_match,
#  min_amount, max_amount, debit_account, credit_account, priority, business_ratio)
_BUILTIN_RULES: list[tuple] = [
    # ── 個人支出 (オーナーがカード等で私的支出) ──
    # 借方 3002 事業主貸 / 貸方 = payment.expense_credit_account
    # 例: VPASS で個人支出 → 借方 3002 / 貸方 2010 未払金
    #     引落日に 借方 2010 / 貸方 1002 とペアで未払金完全消去
    ("個人支出: フォールバック",      "個人支出", "",        "", "", 0, 0, "3002", "", 950, 100),
    # ── 事業外入金 (事業口座が個人口座兼用の場合に普通預金を実残高に合わせる) ──
    # 借方 1002 普通預金 / 貸方 3003 事業主借 (オーナーが個人収入を事業口座に入金した扱い)
    ("事業外入金: 給与振込",         "給与",    "",         "", "", 0, 0, "1002", "3003", 800, 100),
    ("事業外入金: 非課税収入",        "非課税",   "",         "", "", 0, 0, "1002", "3003", 810, 100),
    ("事業外入金: 保険金",           "保険金",   "",         "", "", 0, 0, "1002", "3003", 820, 100),
    ("事業外入金: 返金 (経費取消)",    "返金",    "",         "", "", 0, 0, "1002", "3003", 830, 100),
    # ── 突合無し出金 (現金引出 / 個人振込 等) ──
    # カード引落 (= card_bank_billing 突合済) は engine.py で別処理 (借方 2010 / 貸方 1002)。
    # 突合できなかった出金は「オーナー個人への引出」とみなす:
    # 借方 3002 事業主貸 / 貸方 1002 普通預金
    ("出金: 個人引出 (突合無し)",     "出金",    "",         "", "", 0, 0, "3002", "1002", 850, 100),
    # ── 経費 (タグ別) ──
    ("経費: 旅費交通費 (車・交通)",   "経費", "car",         "", "", 0, 0, "6004", "", 110, 100),
    ("経費: 損害保険料",              "経費", "insurance",   "", "", 0, 0, "6008", "", 120, 100),
    ("経費: 通信費 (サービス)",        "経費", "service",     "", "", 0, 0, "6005", "", 130, 100),
    ("経費: 接待交際費 (飲食店)",      "経費", "restaurant",  "", "", 0, 0, "6007", "", 140, 100),
    ("経費: 接待交際費 (お茶)",        "経費", "drink",       "", "", 0, 0, "6007", "", 145, 100),
    ("経費: 消耗品費 (コンビニ)",      "経費", "convenience", "", "", 0, 0, "6010", "", 150, 100),
    ("経費: 消耗品費 (スーパー)",      "経費", "supermarket", "", "", 0, 0, "6010", "", 155, 100),
    ("経費: 消耗品費 (ホームセンター)", "経費", "homecenter",  "", "", 0, 0, "6010", "", 160, 100),
    ("経費: 消耗品費 (商業施設)",      "経費", "facility",    "", "", 0, 0, "6010", "", 165, 100),
    ("経費: 新聞図書費 (本)",          "経費", "book",        "", "", 0, 0, "7002", "", 170, 100),
    ("経費: 地代家賃 (大家業)",        "経費", "landlord",    "", "", 0, 0, "6016", "", 180, 100),
    ("経費: 福利厚生費 (アクティビティ)", "経費", "activity",  "", "", 0, 0, "6012", "", 190, 100),
    # 医療費は所得控除側で扱うので仕訳しない (rule を置かない)
    # ── 経費フォールバック ──
    ("経費: 雑費 (フォールバック)",    "経費", "",           "", "", 0, 0, "7008", "", 900, 100),
    # ── 売上 ──
    ("売上 (Stripe / 振込 等)",        "売上", "",           "", "", 0, 0, "",     "4001", 100, 100),
    # ── 家賃収入 (= 不動産収入勘定 4100) ──
    ("家賃収入 (不動産)",              "家賃収入", "",       "", "", 0, 0, "",     "4100", 100, 100),
    # ── 保険金 (= 雑収入 4002) ──
    ("保険金 (雑収入)",                "保険金", "",         "", "", 0, 0, "",     "4002", 100, 100),
    # ── 給与 (借方=普通預金、貸方=事業外なので空欄、申告書 B 第一表で扱う) ──
    # 給与は事業仕訳の対象外。aoiro 集計には乗せない。
]

# ─────────────────────────────────────────────
# builtin payment_accounts (bank → 既定科目)
# - クレカ系: 経費の貸方 = 未払金 (2010)、売上は通常通らない
# - 銀行系: 経費の貸方 = 普通預金 (1002)、売上の借方 = 普通預金
# - レシート系: 経費の貸方 = 事業主借 (3003) (個人カード/現金で立替)
# - PayPal: 普通預金扱い (1002) (専用 bank account がある場合は別途)
# - 注文明細系 (Amazon / 楽天 / Yahoo!ショッピング 等):
#   tx_links で カード明細と統合されるため、注文側 bank の rule が
#   走らないよう 経費 rule では対象外にしたい。デフォルトは未払金で
#   置いておくが、運用で fix する想定。
# ─────────────────────────────────────────────
# (bank, expense_credit_account, income_debit_account, note)
_BUILTIN_PAYMENTS: list[tuple[str, str, str, str]] = [
    # クレジットカード系
    ("VPASS",    "2010", "1002", "三井住友 (VPASS)"),
    ("MUFGAmex", "2010", "1002", "三菱UFJ Amex"),
    ("Orico",    "2010", "1002", "Orico"),
    ("メルカード", "2010", "1002", "メルカード (メルペイ翌月引落)"),
    ("AmazonPay", "2010", "1002", "Amazon Pay"),
    # 銀行系
    ("MUFG",     "1002", "1002", "三菱UFJ銀行 普通預金"),
    # ウォレット
    ("PayPal",   "1002", "1002", "PayPal"),
    # 立替系
    ("レシート",  "3003", "3002", "レシート (個人立替 → 事業主借)"),
    # 注文明細系 (経費は基本 tx_links 経由でカード側を見るため、
    # 注文側を直接仕訳する場合の fallback)
    ("Amazon",          "2010", "1002", "Amazon 注文明細"),
    ("楽天市場",        "2010", "1002", "楽天市場"),
    ("Yahoo!ショッピング", "2010", "1002", "Yahoo!ショッピング"),
    ("Mercari",         "2010", "1002", "メルカリ購入"),
    ("Mercari売上",     "1002", "1002", "メルカリ売上 (現金化)"),
    ("ヤフオク購入",     "2010", "1002", "ヤフオク購入"),
    ("ヤフオク売上",     "1002", "1002", "ヤフオク売上"),
    ("AliExpress",      "2010", "1002", "AliExpress"),
    ("CAMPFIRE",        "2010", "1002", "CAMPFIRE"),
    ("Makuake",         "2010", "1002", "Makuake"),
]


def seed_builtin_rules(con: sqlite3.Connection) -> int:
    """builtin ルールを冪等 upsert。name 一致で照合。"""
    inserted = 0
    for row in _BUILTIN_RULES:
        (name, cm, tm, bm, km, mn, mx, da, ca, pri, br) = row
        cur = con.execute("SELECT id, is_builtin FROM aoiro_journal_rules WHERE name=?", (name,))
        ex = cur.fetchone()
        if ex is None:
            con.execute(
                "INSERT INTO aoiro_journal_rules "
                "(name,category_match,tag_match,bank_match,keyword_match,"
                " min_amount,max_amount,debit_account,credit_account,priority,"
                " is_active,business_ratio,is_builtin) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,1,?,1)",
                (name, cm, tm, bm, km, mn, mx, da, ca, pri, br),
            )
            inserted += 1
        else:
            # builtin の condition 部分のみ追従更新 (ユーザの is_active / business_ratio は尊重)
            if ex["is_builtin"]:
                con.execute(
                    "UPDATE aoiro_journal_rules SET "
                    "category_match=?,tag_match=?,bank_match=?,keyword_match=?,"
                    "min_amount=?,max_amount=?,debit_account=?,credit_account=?,priority=? "
                    "WHERE id=?",
                    (cm, tm, bm, km, mn, mx, da, ca, pri, ex["id"]),
                )
    con.commit()
    return inserted


def seed_builtin_payments(con: sqlite3.Connection) -> int:
    inserted = 0
    for bank, exp_acc, inc_acc, note in _BUILTIN_PAYMENTS:
        cur = con.execute("SELECT 1 FROM aoiro_payment_accounts WHERE bank=?", (bank,))
        if cur.fetchone():
            continue
        con.execute(
            "INSERT INTO aoiro_payment_accounts "
            "(bank, expense_credit_account, income_debit_account, note) "
            "VALUES (?,?,?,?)",
            (bank, exp_acc, inc_acc, note),
        )
        inserted += 1
    con.commit()
    return inserted


# ─────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────
def list_rules(con: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM aoiro_journal_rules ORDER BY priority, id"
    ).fetchall()]


def upsert_rule(con: sqlite3.Connection, *, id_: int | None, name: str,
                category_match: str = "", tag_match: str = "", bank_match: str = "",
                keyword_match: str = "", min_amount: int = 0, max_amount: int = 0,
                debit_account: str = "", credit_account: str = "",
                priority: int = 100, is_active: bool = True,
                business_ratio: int = 100) -> int:
    if id_:
        cur = con.execute("SELECT is_builtin FROM aoiro_journal_rules WHERE id=?", (id_,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"rule id {id_} not found")
        # builtin の name / debit / credit / priority はユーザ編集可、condition だけ
        # 触ったら seed 上書きで戻る (これは挙動として OK)
        con.execute(
            "UPDATE aoiro_journal_rules SET name=?, category_match=?, tag_match=?, "
            "bank_match=?, keyword_match=?, min_amount=?, max_amount=?, "
            "debit_account=?, credit_account=?, priority=?, is_active=?, "
            "business_ratio=? WHERE id=?",
            (name, category_match, tag_match, bank_match, keyword_match,
             min_amount, max_amount, debit_account, credit_account,
             priority, 1 if is_active else 0, business_ratio, id_),
        )
        con.commit()
        return id_
    cur = con.execute(
        "INSERT INTO aoiro_journal_rules "
        "(name,category_match,tag_match,bank_match,keyword_match,min_amount,max_amount,"
        " debit_account,credit_account,priority,is_active,business_ratio,is_builtin) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (name, category_match, tag_match, bank_match, keyword_match, min_amount,
         max_amount, debit_account, credit_account, priority,
         1 if is_active else 0, business_ratio),
    )
    con.commit()
    return cur.lastrowid


def delete_rule(con: sqlite3.Connection, id_: int) -> bool:
    cur = con.execute("SELECT is_builtin FROM aoiro_journal_rules WHERE id=?", (id_,))
    row = cur.fetchone()
    if row is None or row["is_builtin"]:
        return False
    con.execute("DELETE FROM aoiro_journal_rules WHERE id=?", (id_,))
    con.commit()
    return True


def list_payment_accounts(con: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM aoiro_payment_accounts ORDER BY bank"
    ).fetchall()]


def upsert_payment_account(con: sqlite3.Connection, bank: str,
                           expense_credit_account: str, income_debit_account: str = "",
                           note: str = "") -> None:
    cur = con.execute("SELECT 1 FROM aoiro_payment_accounts WHERE bank=?", (bank,))
    if cur.fetchone():
        con.execute(
            "UPDATE aoiro_payment_accounts SET expense_credit_account=?, "
            "income_debit_account=?, note=? WHERE bank=?",
            (expense_credit_account, income_debit_account, note, bank),
        )
    else:
        con.execute(
            "INSERT INTO aoiro_payment_accounts (bank, expense_credit_account, "
            "income_debit_account, note) VALUES (?,?,?,?)",
            (bank, expense_credit_account, income_debit_account, note),
        )
    con.commit()


# ─────────────────────────────────────────────
# マッチング (engine から使われる)
# ─────────────────────────────────────────────
def _csv_set(s: str) -> set[str]:
    return {t.strip() for t in (s or "").split(",") if t.strip()}


def matches(rule: dict, *, category: str, tags: set[str], bank: str,
            description: str, amount: int) -> bool:
    if not rule.get("is_active"):
        return False
    cm = (rule.get("category_match") or "").strip()
    if cm and cm != category:
        return False
    tm = _csv_set(rule.get("tag_match") or "")
    if tm and not (tm & tags):
        return False
    bm = _csv_set(rule.get("bank_match") or "")
    if bm and bank not in bm:
        return False
    km = (rule.get("keyword_match") or "").strip()
    if km and km not in (description or ""):
        return False
    mn = rule.get("min_amount") or 0
    mx = rule.get("max_amount") or 0
    if mn and amount < mn:
        return False
    if mx and amount > mx:
        return False
    return True
