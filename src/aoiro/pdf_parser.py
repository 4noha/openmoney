"""申告書等送信票 PDF (= 申告書 B 第一表 + 第二表) のテキストから
申告書 B 入力値・所得控除明細を抽出するパーサ。

MarkItDown で抽出されたテキストはレイアウト崩れがあり、数字が
スペース区切りで分割されることが多い。第一表は番号 (31, 32, 45, 46, 49 等)、
第二表は控除区分名で値が並ぶ。

抽出失敗時は None を返し、import 側でデフォルト値にフォールバック。
"""
from __future__ import annotations

import re

# 第一表 (申告書 B 第一表) のラベル → snapshot キー
# 注意: 令和 6 年と令和 7 年で番号が 1 つズレるので、番号ベースではなく
# ラベル名ベースのアンカーを採用する (令和6年: 50源泉, 令和7年: 49源泉 等)
# 番号が後ろに来るパターン: "<ラベル>(\s+<番号>)?\s+<value digits>"
_FIRST_TABLE_PATTERNS: list[tuple[str, str, str]] = [
    # (key, regex pattern, description)
    # 営業等収入 (ア)
    ("business_revenue", r"営\s*業\s*等\s*区\s*分\s*\d*\s*ア\s+([\-\d](?:\s*[\-\d]){0,8})", "営業等収入"),
    # 給与収入 (オ)
    ("salary_revenue", r"給\s*与\s*区\s*分\s*\d*\s*オ\s+([\-\d](?:\s*[\-\d]){0,8})", "給与収入"),
    # 給与所得 (6)
    ("salary_income", r"給与\s*区\s*分\s*6\s+([\-\d](?:\s*[\-\d]){0,8})", "給与所得"),
    # 事業所得 (1)
    ("business_income", r"事\s*営\s*業\s*等\s*1\s+([\-\d](?:\s*[\-\d]){0,8})", "事業所得"),
    # 不動産所得 (3)
    ("real_estate_income", r"不\s*動\s*産\s*3\s+([\-\d](?:\s*[\-\d]){0,8})", "不動産所得"),
    # 合計所得金額 (12)
    ("total_income", r"合\s*計\s*12\s+([\-\d](?:\s*[\-\d]){0,8})", "合計所得"),
    # ─── 以下はラベル名アンカー (年度の番号差異に頑健) ───
    # 復興特別所得税額: ラベル直後に番号 + 値が来る
    ("reconstruction_surtax",
     r"復興特別所得税額\s*\d*\s+([\-\d](?:\s*[\-\d]){0,8})", "復興特別"),
    # 所得税及び復興特別所得税の額 (合計税額)
    ("total_tax",
     r"所得税及び復興特別所得税の額\s*\d*\s+([\-\d](?:\s*[\-\d]){0,8})", "合計税額"),
    # 源泉徴収税額: 第一表側 (短いラベルに数字)
    ("withholding_tax",
     r"源泉徴収税額\s*\d+\s+([\-\d](?:\s*[\-\d]){0,8})", "源泉徴収"),
    # 令和6年分 定額減税 (44): 「再差引...43 ... 44 <値> ... 再々差引...45」
    # 令和7年では (44) が存在しないので失敗する (=値なし)
    ("tax_credits",
     r"再差引.{0,200}?44\s+([\-\d](?:\s*[\-\d]){0,5}).{0,80}?再々差引",
     "税額控除 (定額減税等)"),
    # 還付額・納付額は (合計税額 − 源泉徴収 − 予定納税) で再計算するため
    # PDF からの抽出は省略 (隣接する説明番号と区別困難なため)
]

# 第二表 (所得控除明細) のラベル → kind
# ラベル直後に番号 (年度別) + 値が並ぶ。番号は \d+ で許容
_DEDUCTION_PATTERNS: list[tuple[str, str]] = [
    ("社保",      r"社会保険料控除\s*\d+\s+([\-\d](?:\s*[\-\d]){0,8})"),
    ("小規模",    r"小規模企業共済等掛金控除\s*\d+\s+([\-\d](?:\s*[\-\d]){0,8})"),
    ("生命保険",  r"生命保険料控除\s*\d+\s+([\-\d](?:\s*[\-\d]){0,8})"),
    ("地震保険",  r"地震保険料控除\s*\d+\s+([\-\d](?:\s*[\-\d]){0,8})"),
    # 基礎控除: 令和6年「基 礎 控 除 24 480000」/ 令和7年「特 基 定 別 親 控 族 除 ... 24 25 ...」
    # ラベル「基 礎 控 除」直後の番号+値で取る (令和6年は確実、令和7年は表崩れで失敗する場合あり)
    ("基礎控除",  r"基\s*礎\s*控\s*除\s*\d+\s+([\-\d](?:\s*[\-\d]){0,8})"),
    ("雑損控除",  r"雑\s*損\s*控\s*除\s*\d+\s+([\-\d](?:\s*[\-\d]){0,8})"),
    ("医療費",    r"医療費控除\s*区\s*\d*\s*分?\s*\d+\s+([\-\d](?:\s*[\-\d]){0,8})"),
    ("寄附金",    r"寄\s*附\s*金\s*控\s*除\s*\d+\s+([\-\d](?:\s*[\-\d]){0,8})"),
    # 配偶者控除/配偶者特別控除 (21 22 区分): 表崩れで「区 分 2 ～ 21 22 <値>」 形式
    # 「～」をアンカーにして「～ 番号 番号 値」を取得
    ("配偶者",
     r"配\s*\(?\s*特別.{0,60}～\s*\d+\s+\d+\s+([\-\d](?:\s*[\-\d]){0,7})"),
    # 扶養控除 (23): 「扶養控除 区 分 23 <値>」
    ("扶養",
     r"扶\s*養\s*控\s*除\s*区\s*分\s*\d+\s+([\-\d](?:\s*[\-\d]){0,7})"),
    # 控除合計: 「合 計 ... 25 数値」 (令和6年は 29、令和7年は 30 だが「合計 (25+...) <digits>」で取得試行)
    # これは _allow_deduction_total_extraction のため別関数で処理
]


# 控除合計の抽出 (年度別番号差異に対応)
_DEDUCTION_TOTAL_PATTERNS: list[str] = [
    # 「13 から ?? までの計 ?? <値>」: 13-24 計 (令和5/6) や 13-25 計 (令和7)
    r"13\s*から\s*\d+\s*までの計\s*\d+\s+([\-\d](?:\s*[\-\d]){0,8})",
]


def extract_deduction_subtotal(text: str) -> int | None:
    """13 から 24 (or 25) までの計 (= 個別控除13番〜配偶者・扶養・基礎までの計) を抽出。
    医療費控除・寄附金等の控除前の合計値。
    """
    import re as _re
    for pattern in _DEDUCTION_TOTAL_PATTERNS:
        m = _re.search(pattern, text, _re.DOTALL)
        if m:
            v = _spaces_to_int(m.group(1))
            if v and v > 0:
                return v
    return None


def _spaces_to_int(s: str) -> int | None:
    cleaned = re.sub(r"[\s,]+", "", (s or "").strip())
    if not cleaned or cleaned in ("-", "・"):
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def extract_first_table(text: str) -> dict:
    """申告書 B 第一表の主要 KPI を抽出。"""
    out: dict = {}
    for key, pattern, desc in _FIRST_TABLE_PATTERNS:
        # tax_credits の正規表現は DOTALL で複数行マッチ
        flags = re.DOTALL if key == "tax_credits" else 0
        m = re.search(pattern, text, flags)
        if not m:
            continue
        v = _spaces_to_int(m.group(1))
        if v is not None:
            out[key] = v
    return out


def extract_deductions(text: str) -> list[dict]:
    """第二表 (申告書 B 第二表) から所得控除明細を抽出。
    各 (kind, amount) のリスト。amount=0 はスキップ。
    """
    out = []
    for kind, pattern in _DEDUCTION_PATTERNS:
        m = re.search(pattern, text)
        if not m:
            continue
        v = _spaces_to_int(m.group(1))
        if v and v > 0:
            out.append({"kind": kind, "amount": v,
                         "payee": "PDF 自動抽出", "note": "申告書等送信票 第二表"})
    return out


def extract_all(text: str) -> dict:
    """第一表 + 第二表をまとめて抽出。
    PDF テキストに基礎控除ラベルが現れない場合 (令和5年など)、
    13-25 計と個別控除合計の差から基礎控除を逆算する。
    """
    first = extract_first_table(text)
    deductions = extract_deductions(text)
    # 基礎控除がない場合、控除小計から逆算
    has_basic = any(d["kind"] == "基礎控除" for d in deductions)
    if not has_basic:
        subtotal = extract_deduction_subtotal(text)
        if subtotal:
            indiv_sum = sum(d["amount"] for d in deductions)
            delta = subtotal - indiv_sum
            # 基礎控除は通常 ¥480,000 (合計所得 2,400 万円超は減額あり)。
            # ¥48 万 ± ¥10 万なら基礎控除と推定して登録。
            if 380_000 <= delta <= 580_000:
                deductions.append({
                    "kind": "基礎控除", "amount": delta,
                    "payee": "PDF 自動抽出 (合計差分推定)",
                    "note": f"申告書等送信票 第二表 合計¥{subtotal:,} − 個別¥{indiv_sum:,}",
                })
    return {
        "first_table": first,
        "deductions": deductions,
    }
