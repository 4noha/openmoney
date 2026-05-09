"""レシート OCR → 仕訳候補抽出。

MarkItDown でファイル (PDF / 画像) をテキスト化し、ヒューリスティクスで
日付・金額・店名・category を推定する。手動仕訳ダイアログにプリフィルする
用途を想定。

依存: markitdown (microsoft/markitdown)
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

# 抽出候補
_DATE_PATTERNS = [
    re.compile(r"(20\d{2})[/\-年\.](\d{1,2})[/\-月\.](\d{1,2})"),
    re.compile(r"令和(\d+)年(\d{1,2})月(\d{1,2})日"),
    re.compile(r"(\d{1,2})[/\-月\.](\d{1,2})日?"),  # 月日のみ (年なし)
]

# 金額: 優先度の高いキーワードから順に試行 (合計 > 現計 > 小計)
_TOTAL_PATTERNS = [
    re.compile(r"(?:合計|総計|総額)[\s:：]*[¥￥]?\s*([\d,]+)"),
    re.compile(r"(?:現計|請求金額|お支払金額|ご請求額)[\s:：]*[¥￥]?\s*([\d,]+)"),
    re.compile(r"(?:小計|お買上計|お買上げ計)[\s:：]*[¥￥]?\s*([\d,]+)"),
]
# 数字のみ (フォールバック)
_AMOUNT_PATTERN = re.compile(r"[¥￥]\s*([\d,]+)|([\d,]+)\s*円")


def _parse_date(text: str) -> str | None:
    """テキストから最初の日付を YYYY/MM/DD で返す。"""
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        groups = m.groups()
        try:
            if "令和" in pat.pattern:
                # 令和 N 年 → 西暦 = 2018 + N
                y = 2018 + int(groups[0])
                mo = int(groups[1])
                d = int(groups[2])
            elif len(groups) == 3 and groups[0] and len(groups[0]) == 4:
                y = int(groups[0])
                mo = int(groups[1])
                d = int(groups[2])
            else:
                # 月日のみ → 当年とみなす (呼び出し側で必要なら上書き)
                from datetime import datetime
                y = datetime.now().year
                mo = int(groups[0])
                d = int(groups[1])
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}/{mo:02d}/{d:02d}"
        except (ValueError, IndexError):
            continue
    return None


def _parse_amount(text: str) -> int | None:
    """テキストから合計金額を推定。
    優先順位: 合計/総計 → 現計/請求 → 小計 → 最大の ¥/円 数字
    """
    for pat in _TOTAL_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                continue
    candidates = []
    for m in _AMOUNT_PATTERN.finditer(text):
        try:
            n = int((m.group(1) or m.group(2)).replace(",", ""))
            if n >= 10:
                candidates.append(n)
        except (ValueError, AttributeError):
            continue
    return max(candidates) if candidates else None


def _parse_shop_name(text: str) -> str | None:
    """テキストの先頭付近から店名を推定。"""
    KEYWORDS = ("株式会社", "有限会社", "合同会社", "店", "ストア", "マート",
                "Store", "Shop", "Mart", "Inc.", "Co.")
    for line in text.splitlines()[:20]:
        line = line.strip()
        if not line:
            continue
        if len(line) < 2 or len(line) > 60:
            continue
        if any(kw in line for kw in KEYWORDS):
            return line
    # フォールバック: 最初の英数字以外の行
    for line in text.splitlines()[:5]:
        line = line.strip()
        if line and not line.replace(",", "").replace(".", "").isdigit():
            return line[:60]
    return None


def extract_receipt(file_bytes: bytes, filename: str) -> dict:
    """ファイルをテキスト化して仕訳候補を返す。

    Returns:
        {
            "raw_text": str,            # 抽出されたテキスト (debug 用)
            "date": "2026/05/09" | None,
            "amount": 1234 | None,
            "shop_name": str | None,
            "category_hint": str | None, # detect_tags の結果 (経費科目の手がかり)
            "suggested_debit_account": "6010" | None,
        }
    """
    suffix = Path(filename).suffix or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        from markitdown import MarkItDown
        md = MarkItDown()
        result = md.convert(tmp_path)
        raw_text = (result.text_content or "").strip()
    except Exception as e:
        raw_text = f"[OCR error: {e}]"
    finally:
        try:
            Path(tmp_path).unlink()
        except Exception:
            pass

    date = _parse_date(raw_text)
    amount = _parse_amount(raw_text)
    shop = _parse_shop_name(raw_text)

    # 店名/テキストから category タグを推定 → 経費科目に
    category_hint = None
    suggested_debit = None
    try:
        from src.server.categorize import detect_tags
        tags = detect_tags(raw_text[:500], "", "")
        # tag → debit_account のマッピング (rules.py のシードと同じ)
        TAG_TO_ACCOUNT = {
            "car": "6004", "insurance": "6008", "service": "6005",
            "restaurant": "6007", "drink": "6007",
            "convenience": "6010", "supermarket": "6010",
            "homecenter": "6010", "facility": "6010",
            "book": "7002", "landlord": "6016", "activity": "6012",
        }
        for tag in tags:
            if tag in TAG_TO_ACCOUNT:
                category_hint = tag
                suggested_debit = TAG_TO_ACCOUNT[tag]
                break
        if not suggested_debit:
            suggested_debit = "7008"  # 雑費
            category_hint = "fallback"
    except Exception:
        pass

    return {
        "raw_text": raw_text[:2000],  # 最初の 2000 文字だけ返す (UI 用)
        "date": date,
        "amount": amount,
        "shop_name": shop,
        "category_hint": category_hint,
        "suggested_debit_account": suggested_debit,
        "suggested_credit_account": "3003",  # 事業主借 (個人立替)
    }
