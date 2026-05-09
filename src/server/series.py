"""Kindle (Amazon D01-*) のシリーズ折り畳み用 base title 抽出。

複数巻物の同一作品をグループ化するため、 タイトルから巻号マーカーを除いた
共通部分を返す。 抽出失敗時は None。 抽出 base が同 oid + 2 件以上揃ったら
UI で `▶ シリーズ名 N冊 合計¥X` の折り畳みヘッダーに集約される。

巻号マーカーは多様:
- (N) / （N） 末尾  例: ビッグオーダー(10)
- N巻 / 第N巻 末尾  例: 猛獣性少年少女 新装版 1巻
- ：N + 任意副題 末尾  例: 坊っちゃんの時代 ： 3 かの蒼空に
- （上/中/下） 末尾  例: 機動戦士ガンダム ハサウェイ（下）
- 末尾ローマ数字  例: テルマエ・ロマエVI
- 末尾裸数字  例: ナニワ金融道1
- 中間 N巻  例: 天体戦士サンレッド 1巻 完全版 ...
- 中間 （上中下）  例: ハサウェイ（上） ハサウェイ
- 中間 N + 副題 (巻文字なし)  例: アオザイ通信 完全版 1 食と文化
"""
from __future__ import annotations

import re

# 末尾レーベル名: 「(角川コミックス・エース)」「(SMART COMICS)」等の非数字パレン。
# 巻番号パレン「(22)」 と 上中下パレン「（下）」 は剥がさない（負の先読みで除外）。
_LABEL_PAREN = re.compile(
    r'\s*[\(（]'
    r'(?!\s*\d+\s*[\)）])'         # 数字のみは剥がさない (= 巻番号として残す)
    r'(?!\s*[上中下]\s*[\)）])'    # 「（上）」「（中）」「（下）」 は剥がさない
    r'[^\)）]+[\)）]\s*$'
)

_VOL_END_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'\s*[\(（]\s*\d+\s*[\)）]\s*$'),                  # (N) （N）
    re.compile(r'\s*第?\s*\d+\s*巻\s*$'),                         # N巻 / 第N巻
    re.compile(r'\s*[：:]\s*\d+(?:\s+\S.*)?\s*$'),                # ：N (任意の副題)
    re.compile(r'\s*[\(（]\s*[上中下]\s*[\)）]\s*$'),               # （上/中/下）末尾
    re.compile(r'(?:\s|　)*(?:[IVX]{1,5}|[ＩＶＸ]{1,5})\s*$'),     # 末尾ローマ数字
    re.compile(r'\s*\d+\s*$'),                                    # 末尾数字（"ナニワ金融道1" 等）
)

_VOL_MIDDLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'\s+第?\s*\d+\s*巻[\s　]'),                       # "タイトル N巻 副題"
    re.compile(r'[\(（]\s*[上中下]\s*[\)）]'),                      # 中間（上/中/下）
    # 「タイトル<空白>N<空白>副題」 (巻文字なし; 副題2文字以上で誤判定を抑制)
    re.compile(r'(?:\s|　)+\d{1,3}(?:\s|　)+\S{2,}'),
)


def extract_series_base(title: str) -> str | None:
    """巻号マーカーを除いた series base を返す。 抽出失敗時 None。

    base 長 < 2 文字なら抽出失敗扱い (= 巻号だけ残しても意味ないので)。
    """
    s = title.strip()
    # 末尾レーベル副題を剥がす（巻番号パレンは保護）
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
