"""
PayPal CSV インポータ（公式エクスポート → SQLite）。

PayPal のアクティビティ画面右上「ステートメント → カスタム明細表示」から
CSV をダウンロードし、このスクリプトに渡すと bank='PayPal' で取り込む。

使い方:
    uv run python -m scripts.paypal_csv_import ~/Downloads/Download.CSV

CSV ヘッダー（PayPal 標準・日本語）:
    日付,時間,タイムゾーン,名前,タイプ,ステータス,通貨,純売上高,手数料,合計,...

支払い系（Type が「ご利用」「お支払い」「マーチャント支払い」「自動引き落とし」等で
合計が負）のみ debit に取り込む。受取・残高チャージ等は無視する。
"""
from __future__ import annotations

import csv
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "transactions.db"

# 「支払い」系と判断するタイプキーワード（部分一致）
_PAYMENT_TYPE_KEYWORDS = (
    "支払い", "ご利用", "ペイメント", "Payment", "マーチャント",
    "自動引き落とし", "プレフィルド", "Subscription",
)
# 完了済みのみ取り込む
_DONE_STATUS = ("完了", "Completed", "完了しました")


def _parse_amount(s: str) -> int:
    """『-1,200.00』『1,200』『-3,450円』等から整数（円）を返す。負号保持。"""
    if not s:
        return 0
    s = s.replace(",", "").replace("円", "").replace("¥", "").strip()
    m = re.match(r"^(-?)(\d+)(?:\.\d+)?$", s)
    if not m:
        return 0
    n = int(m.group(2))
    return -n if m.group(1) == "-" else n


def _parse_date(s: str) -> str | None:
    """各種日付表記を YYYY/MM/DD に正規化。
    サポート: 2026/02/14, 2026-02-14, 2/14/2026 (US式), 14/02/2026 (EU式)。
    """
    s = s.strip()
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            d = datetime.strptime(s, fmt)
            # US/EU 式は曖昧なので妥当性チェック: 結果が未来3年以内 / 過去20年以内
            yr = d.year
            if 2005 <= yr <= datetime.now().year + 3:
                return d.strftime("%Y/%m/%d")
        except ValueError:
            continue
    return None


def _row_get(row: dict, *keys: str) -> str:
    """行からキーで取得。空白付きキー（'合計 ' 等）も拾う。"""
    for k in keys:
        if k in row and row[k] is not None:
            return row[k]
        # 末尾スペース付きキーも試す
        for actual_key in row.keys():
            if actual_key.strip() == k:
                return row[actual_key] or ""
    return ""


def import_csv(path: Path) -> int:
    """CSV を読み込み bank='PayPal' で transactions に追加。新規追加件数を返す。"""
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    # PayPal の CSV は UTF-8 BOM 付き or Shift_JIS の両方が出回る
    encodings = ("utf-8-sig", "utf-8", "cp932", "shift_jis")
    rows = None
    for enc in encodings:
        try:
            with open(path, encoding=enc, newline="") as f:
                rows = list(csv.DictReader(f))
            break
        except UnicodeDecodeError:
            continue
    if rows is None:
        raise RuntimeError(f"CSV をデコードできませんでした: {path}")

    print(f"[paypal_csv] 読込: {len(rows)} 行")

    from src.db import connect as _connect_central
    con = _connect_central(DB_PATH)
    saved = 0
    skipped_pos = 0    # 受取（残高チャージ等）スキップ
    skipped_zero = 0
    skipped_date = 0
    skipped_internal = 0  # 残高チャージ等の内部移動

    # 内部移動・チャージ系の説明キーワード（debit でも経費にしないものをスキップ）
    _INTERNAL_KEYWORDS = (
        "口座への引き出し", "銀行口座への送金",
        "Withdrawal", "Bank Withdrawal",
        # マイナス側でも「PayPal への入金」自体は経費ではない
    )

    for r in rows:
        # 列名は CSV の言語/書式で揺れる（'合計 ' 末尾スペース等）
        date_raw = _row_get(r, "日付", "Date")
        amount_s = _row_get(r, "合計", "Total", "純売上高", "Net")
        type_str = _row_get(r, "タイプ", "Type")
        status   = _row_get(r, "ステータス", "Status")
        desc_str = _row_get(r, "説明", "Description")
        name     = _row_get(r, "名前", "Name", "From/To")
        item     = _row_get(r, "商品名", "Item Title")
        txn_id   = _row_get(r, "取引ID", "Transaction ID")

        date_norm = _parse_date(date_raw)
        if not date_norm:
            skipped_date += 1
            continue
        # ステータス列がある場合のみフィルタ（無ければ完了と見なす）
        if status and not any(k in status for k in _DONE_STATUS):
            continue
        amt = _parse_amount(amount_s)
        if amt == 0:
            skipped_zero += 1
            continue
        if amt > 0:
            skipped_pos += 1
            continue
        debit = -amt
        # 内部移動 (出金 → 銀行口座への送金) は経費でないのでスキップ
        if any(k in (desc_str + type_str) for k in _INTERNAL_KEYWORDS):
            skipped_internal += 1
            continue

        merchant = (name or "").strip() or (desc_str or "").strip() or "PayPal取引"
        if item:
            merchant = f"{merchant} | {item.strip()}"
        desc = f"[{txn_id}] {merchant}" if txn_id else merchant
        desc = desc[:250]

        cur = con.execute(
            "INSERT OR IGNORE INTO transactions "
            "(bank, date, description, debit, credit, fetched_at) "
            "VALUES (?,?,?,?,?,?)",
            ("PayPal", date_norm, desc, debit, 0, datetime.now().isoformat()),
        )
        saved += cur.rowcount

    con.commit()
    con.close()
    print(f"[paypal_csv] スキップ: 日付不明 {skipped_date} / 受取 {skipped_pos} "
          f"/ ¥0 {skipped_zero} / 内部移動 {skipped_internal}")
    print(f"[paypal_csv] DB保存: {saved} 件追加")
    return saved


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: uv run python -m scripts.paypal_csv_import <path/to/Download.CSV>")
        sys.exit(1)
    import_csv(Path(sys.argv[1]).expanduser())
