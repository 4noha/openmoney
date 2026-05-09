"""青色申告決算書 PDF から「地代家賃の内訳」をインポートする。

過去の納税/<year>/青色申告決算書.pdf を MarkItDown でテキスト化し、
「地代家賃の内訳」 セクションから:
  - 支払先 (貸主) の住所・氏名
  - 賃借物件
  - 賃借料 (本年中の賃借料・権利金等)
を抽出する。

完全自動は困難なので、ヒューリスティックスで候補を出し、UI でユーザが確認・補正する想定。
"""
from __future__ import annotations

import re
from pathlib import Path

# 都道府県名 (住所判定用)
_PREFECTURES = (
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
)


def _extract_text(pdf_path: Path) -> str:
    """PDF を MarkItDown でテキスト化。失敗時は空文字。"""
    try:
        from markitdown import MarkItDown
        md = MarkItDown()
        return md.convert(str(pdf_path)).text_content or ""
    except Exception:
        return ""


def _find_section(text: str) -> str:
    """「地代家賃の内訳」セクションを抽出。"""
    if "地代家賃の内訳" not in text:
        return ""
    start = text.index("地代家賃の内訳")
    # 終了マーカー (次の見出し)
    end_markers = ("青色申告特別控除", "貸倒引当金", "本年中における特殊事情",
                    "決算の手引き", "貸借対照表", "別紙")
    end = len(text)
    for m in end_markers:
        idx = text.find(m, start + 10)
        if 0 < idx < end:
            end = idx
    return text[start:end]


def _extract_addresses(section: str) -> list[str]:
    """都道府県で始まる住所候補を抽出 (テキスト出現順でソート)。

    PDF の表セル分割で住所が複数行に分かれるケース (例: L1「茨城県牛久市小坂」
    + L3「町２７８５」) に対応。末尾が町・数字で終わっていない場合は
    後続テキスト 500 字以内から `町[数字漢字]+` または `数字+番地?` を
    探して結合する。
    """
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    # 続きパターン: 「町/字/大字/村」 から始まる連続文字のみ。
    # 単純な数字 (例: 金額 1,300,000) を誤って拾わないため厳格化。
    cont_re = re.compile(r"((?:大字|町|字|村)[０-９0-9一-鿿\-－―ー]{1,20})")
    # 既に末尾が町+xx or 数字 で終わっていれば結合不要
    end_done_re = re.compile(r"(?:町[^\s|│┃]+|[０-９0-9\-－―]+)$")
    for pref in _PREFECTURES:
        for m in re.finditer(re.escape(pref) + r"[^\s|│┃]{1,40}", section):
            base = re.sub(r"[│┃|]+", "", m.group(0).strip()).strip()
            if not end_done_re.search(base):
                # 後続 500 字以内で「町X」 や「数字」 を探す
                after = section[m.end():m.end() + 500]
                cm = cont_re.search(after)
                if cm:
                    base = base + cm.group(1)
            if base and base not in seen:
                seen.add(base)
                found.append((m.start(), base))
    found.sort(key=lambda x: x[0])
    return [s for _, s in found]


def _extract_amounts(section: str) -> list[int]:
    """1 万円以上の金額候補を大きい順で。"""
    amounts: set[int] = set()
    for m in re.finditer(r"([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{5,})", section):
        try:
            v = int(m.group(1).replace(",", ""))
            if v >= 10000:
                amounts.add(v)
        except ValueError:
            continue
    return sorted(amounts, reverse=True)


def _extract_names(section: str) -> list[str]:
    """氏名候補 (個人名 2〜5 文字 + 法人名)."""
    candidates: list[str] = []
    # 法人名
    for m in re.finditer(r"((?:株式会社|合同会社|有限会社)[^\s|│┃,。]{1,20})", section):
        s = m.group(1).strip()
        if s not in candidates:
            candidates.append(s)
    for m in re.finditer(r"([^\s|│┃,。]{1,20}(?:株式会社|合同会社|有限会社))", section):
        s = m.group(1).strip()
        if s not in candidates:
            candidates.append(s)
    # 個人名 (純粋に漢字 2〜5 文字、住所/物件/見出し語句を含まない)
    BLACKLIST = (
        "東京", "都道", "府県", "支払先", "賃借", "物件", "地代", "家賃",
        "本年", "賃料", "権利", "金等", "必要", "経費", "算入", "差引", "金額",
        "雑収入", "雑費", "雑損", "貸付", "貸倒", "繰入", "減価", "償却",
        "番地", "丁目", "氏名", "住所", "貸主", "計算", "年分", "決算",
        "内訳", "明細", "事業", "所得", "申告", "特別", "控除", "計算",
        "利益", "損失", "売上", "仕入", "棚卸", "資産", "負債", "資本",
        "金額", "合計", "純利", "差額", "繰越", "前期", "当期", "次期",
        "消費", "等の", "受取", "支払", "軽減", "税率", "対象", "入額",
        "都民", "市民", "区民", "府民", "県民",
        # 物件用途語彙 (PDF の「賃借物件」 列に書かれることがある)
        "住居", "納屋", "工場", "店舗", "事務所", "倉庫", "居宅", "車庫",
        "住宅", "事務", "建物", "土地", "駐車", "アパート", "マンション",
    )
    # 住所文字列内の漢字部分を除外するため、_extract_addresses の結果も避ける
    address_words = set()
    for addr in _extract_addresses(section):
        for piece in re.findall(r"[一-鿿]{2,}", addr):
            address_words.add(piece)
    for m in re.finditer(r"[一-鿿]{2,5}", section):
        s = m.group(0)
        if any(b in s for b in BLACKLIST):
            continue
        # 住所断片を除外
        if s in address_words:
            continue
        # 住所中に部分一致するものも除外 (例: 「区旭町」 「市小坂」)
        if any(s in addr for addr in _extract_addresses(section)):
            continue
        if s not in candidates:
            candidates.append(s)
    return candidates[:20]


def _extract_type_marks(section: str) -> list[str]:
    """○賃 / ○権 / ○更 のマーカーを抽出 (種類)."""
    out = []
    for m in re.finditer(r"[○◯]\s*(賃|権|更)", section):
        out.append({"賃": "賃借料", "権": "権利金", "更": "更新料"}[m.group(1)])
    return out


def estimate_monthly_from_transactions(con, landlord_name: str,
                                         min_amount: int = 10000) -> dict | None:
    """transactions テーブルから landlord 名を含む振込/引落の月額を推定。

    Args:
      con: sqlite3 connection
      landlord_name: PDF から抽出された貸主名 (漢字)
      min_amount: 1 件あたりの最小金額 (ノイズ除外)

    Returns:
      {"monthly_rent", "samples": [...], "match_count", "median", "search_keyword"}
      または None (ヒット 0 件)

    実装: 貸主名の漢字 2 文字 (姓) で description を LIKE 検索。
    description には漢字でなくカタカナで載るケースが多いが、
    多くの場合 transactions の description には漢字 or カナ
    両方含まれる。漢字でヒットしない場合は カナ変換は行わず None。
    """
    import re as _re
    kanji = "".join(_re.findall(r"[一-鿿]+", landlord_name or ""))
    if len(kanji) < 2:
        return None
    # 姓 2 文字をキーワードにする
    keyword = kanji[:2]
    rows = con.execute("""
        SELECT date, debit, description FROM transactions
        WHERE debit >= ? AND description LIKE ?
        ORDER BY date DESC LIMIT 30
    """, (min_amount, f"%{keyword}%")).fetchall()
    amounts = [int(r["debit"]) for r in rows if r["debit"]]
    if not amounts:
        return None
    from statistics import median
    med = int(median(amounts))
    # 上位 5 件のサンプル
    samples = [{"date": r["date"], "amount": int(r["debit"]),
                "desc": (r["description"] or "")[:60]} for r in rows[:5]]
    return {
        "monthly_rent": med,
        "median": med,
        "match_count": len(amounts),
        "samples": samples,
        "search_keyword": keyword,
    }


def parse_rental_section(pdf_path: Path | str, con=None) -> dict:
    """PDF から地代家賃の内訳を解析。

    Returns:
        {
          "found": bool,
          "section_text": str (デバッグ用),
          "candidates": [{
            "landlord_name": ..., "landlord_address": ...,
            "property_address": ..., "annual_rent": ..., "type": ...
          }],
          "raw": {addresses, amounts, names, type_marks}
        }
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return {"found": False, "error": f"file not found: {pdf_path}"}

    text = _extract_text(pdf_path)
    if not text:
        return {"found": False, "error": "PDF text extraction failed"}

    section = _find_section(text)
    if not section:
        return {"found": False, "section_text": "", "candidates": [],
                "reason": "地代家賃の内訳セクションが見つからない"}

    addresses = _extract_addresses(section)
    amounts = _extract_amounts(section)
    names = _extract_names(section)
    type_marks = _extract_type_marks(section)

    # シンプルな組み立て:
    # - 1 つ目の住所 = 貸主住所、2 つ目 = 物件住所 (または同一)
    # - 1 つ目の名前 = 貸主名
    # - 1 番大きい金額 = 年額賃借料 (権利金等を含む可能性)
    candidates = []
    if addresses or names or amounts:
        landlord_address = addresses[0] if addresses else ""
        property_address = (addresses[1] if len(addresses) >= 2 else
                             addresses[0] if addresses else "")
        landlord_name = names[0] if names else ""
        annual_rent = amounts[0] if amounts else 0
        # PDF の年額 / 12 (権利金等含む可能性あり)
        monthly_from_pdf = (annual_rent // 12) if annual_rent else 0

        # 実費推定 (transactions から): landlord_name 漢字部分で検索
        actual = estimate_monthly_from_transactions(con, landlord_name) if con else None
        # 採用優先順位: 実費 (median) があればそれを採用、無ければ PDF/12
        monthly_rent = actual["monthly_rent"] if actual else monthly_from_pdf

        candidates.append({
            "landlord_name": landlord_name,
            "landlord_address": landlord_address,
            "property_address": property_address,
            "annual_rent": annual_rent,
            "monthly_rent": monthly_rent,
            "monthly_rent_from_pdf": monthly_from_pdf,
            "monthly_rent_actual": actual,  # None or {monthly_rent, samples, match_count}
            "type": (type_marks[0] if type_marks else "賃借料"),
        })

    return {
        "found": True,
        "candidates": candidates,
        "raw": {
            "addresses": addresses, "amounts": amounts,
            "names": names, "type_marks": type_marks,
        },
        "section_text": section[:1500],  # デバッグ用 (UI 表示)
    }


def find_kessan_pdf(year: int) -> Path | None:
    """過去の納税/<year>/ から青色申告決算書 PDF を探す。"""
    base = Path("過去の納税") / str(year)
    if not base.exists():
        return None
    for pat in ("青色申告決算書*.pdf", "*決算書*.pdf"):
        for p in base.glob(pat):
            return p
    return None
