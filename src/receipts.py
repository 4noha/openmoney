"""
レシート処理: 画像を Claude Vision で OCR → 金額・日付・店名を抽出 → 明細と突合。
"""
import base64
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_ROOT = Path(__file__).parent.parent
DB_PATH = _ROOT / "transactions.db"
RECEIPTS_DIR = _ROOT / "invoices" / "receipts"

_OCR_PROMPT = """\
このレシート画像をOCRして以下のJSONを返してください。日本語のレシートです。

{
  "date": "YYYY/MM/DD",       // 購入日 / 検針日 / 請求日 (不明なら null)
  "merchant": "発行事業者名",  // ★適格請求書の発行事業者名★ (法人格含む正式名称)
                              //  受領者 / 物件名 / 管理組合名 / 店舗名 ではない。
                              //  例: マンション名 / 物件名 ではなく管理会社の法人名
                              //  例: 「ひたち野店」 (店舗) ではなく
                              //       「株式会社ジョイフル本田」 (法人)
  "invoice_number": "T1234567890123",
                              // 適格請求書登録番号 (T + 13桁)。 適格請求書発行事業者の
                              // 登録番号として明記されている T 番号のみ。 通帳番号 /
                              // 商品コード / 電話番号 等は採用しない。 なければ null
  "amount": 1234,             // 合計金額 (税込、 整数円)
  "items": [                  // 内訳行 (= 商品 / 料金区分 ごと)
    {
      "name": "商品名 / 料金区分名",
      "price": 100,
      "merchant": "発行事業者名",     // この内訳の発行事業者 (= 1 枚に複数事業者の合算
                                     //  ケース、 例: 上水道料金 → 草津市水道事業会計 /
                                     //  下水道料金 → 草津市下水道事業会計)。
                                     //  全内訳が同一事業者なら省略可 (= 親 merchant を継承)
      "invoice_number": "T..."        // この内訳の適格事業者番号、 同上
    }
  ]
}

注意:
- merchant は「適格請求書発行事業者」 を優先。 物件名 / 管理組合名 / 店舗名は採用しない。
- 1 枚のレシートに複数事業者の合算がある場合 (例: 上水道料金 + 下水道料金、 管理費 +
  修繕積立金 で別管理会社) は、 各内訳の事業者名 + T 番号を items[].merchant /
  items[].invoice_number に記入。
- T 番号は適格請求書の登録番号 (T + 13 桁、 半角)。 ハイフン / 全角は除去して半角化。

JSONのみ返してください。余分なテキストは不要です。"""


def _compress(image_bytes: bytes, mime_type: str, max_bytes: int = 4_500_000) -> tuple[bytes, str]:
    """5MB 超の画像を JPEG に圧縮して返す。"""
    if len(image_bytes) <= max_bytes:
        return image_bytes, mime_type
    import io
    from PIL import Image
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    for quality in (85, 70, 55, 40):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        if buf.tell() <= max_bytes:
            return buf.getvalue(), "image/jpeg"
    # それでも大きければ短辺 1500px にリサイズ
    w, h = img.size
    scale = 1500 / min(w, h)
    img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=55)
    return buf.getvalue(), "image/jpeg"


def _ocr_with_claude(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    image_bytes, mime_type = _compress(image_bytes, mime_type)
    import anthropic
    # ANTHROPIC_BASE_URL が設定されていれば backend プロキシ経由になる (SDK が自動参照)。
    # 受領レシートの OCR 精度は Opus が大幅に高い (Haiku/Sonnet は手書き / かすれ / 縦長
    # レシートで誤認識しがち) ので Opus 固定。 backend proxy 側は plan で model 制限を
    # かけずに budget_jpy のみで制限する方針 (= ai_proxy._precheck 参照)。
    # X-OpenMoney-Task: backend proxy が ai_requests に用途タグを記録するため
    # (= レシート OCR と Claude Code エージェントのコストを分離集計する)。
    client = anthropic.Anthropic(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        default_headers={"X-OpenMoney-Task": "receipt-ocr"},
    )
    b64 = base64.standard_b64encode(image_bytes).decode()
    msg = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime_type, "data": b64},
                },
                {"type": "text", "text": _OCR_PROMPT},
            ],
        }],
    )
    text = msg.content[0].text.strip()
    # JSON ブロックを抽出
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    return json.loads(text)


def _find_matching_transaction(con: sqlite3.Connection, amount: int, receipt_date: str | None) -> int | None:
    """金額と日付でトランザクションを検索。"""
    if not amount:
        return None

    params: list = [amount]
    date_clause = ""
    if receipt_date:
        try:
            rd = datetime.strptime(receipt_date, "%Y/%m/%d").date()
            lo = (rd - timedelta(days=7)).strftime("%Y/%m/%d")
            hi = (rd + timedelta(days=7)).strftime("%Y/%m/%d")
            date_clause = "AND date BETWEEN ? AND ?"
            params += [lo, hi]
        except ValueError:
            pass

    # カードの debit（支出）でマッチを探す
    # 分割行 [ORDER_ID/N] を親行より優先（親行はUIで非表示のためバッジが見えなくなる）
    row = con.execute(
        f"""SELECT id FROM transactions WHERE debit=? {date_clause}
            ORDER BY CASE WHEN description LIKE '[%/%]%' THEN 0 ELSE 1 END,
                     date DESC LIMIT 1""",
        params,
    ).fetchone()
    return row[0] if row else None


def _db() -> sqlite3.Connection:
    """receipts/receipt_links 周りのスキーマも src.db に集約済み。"""
    from src.db import connect as _connect_central
    con = _connect_central(DB_PATH)
    # 旧 receipts スキーマには invoice_number 列が無いケースがあるので念のため
    cols = {r[1] for r in con.execute("PRAGMA table_info(receipts)").fetchall()}
    if "invoice_number" not in cols:
        try:
            con.execute("ALTER TABLE receipts ADD COLUMN invoice_number TEXT")
            con.commit()
        except sqlite3.OperationalError:
            pass
    return con


def _push_result(title: str, body: str) -> None:
    try:
        from src.server import _get
        from src.fcm import send
        token = _get("device_token")
        if token:
            send(token, title=title, body=body)
    except Exception as e:
        print(f"[receipts] Push送信失敗: {e}")


def process_receipt(image_bytes: bytes, filename: str, mime_type: str = "image/jpeg") -> dict:
    """
    レシート画像を OCR → DB 保存 → 突合結果を返す。
    返り値: {"receipt": {...}, "transaction_id": int|None, "saved_path": str}
    """
    parsed = _ocr_with_claude(image_bytes, mime_type)
    print(f"[receipts] OCR結果: {parsed}")

    if not parsed.get("amount"):
        print("[receipts] レシートと認識できなかったためスキップ")
        _push_result("レシート読み取り失敗", "レシートを認識できませんでした")
        return {"receipt": parsed, "transaction_id": None, "saved_path": None}

    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    saved_path = RECEIPTS_DIR / filename
    saved_path.write_bytes(image_bytes)

    con = _db()
    tx_id = _find_matching_transaction(con, parsed.get("amount"), parsed.get("date"))
    merchant = parsed.get("merchant") or filename
    invoice_number = parsed.get("invoice_number") or None
    amount = parsed["amount"]
    was_matched = tx_id is not None

    if not tx_id and parsed.get("date"):
        cur = con.execute(
            """INSERT OR IGNORE INTO transactions (bank, date, description, debit, fetched_at)
               VALUES ('レシート', ?, ?, ?, datetime('now','localtime'))""",
            (parsed["date"], merchant, amount),
        )
        con.commit()
        tx_id = cur.lastrowid or None

    cur = con.execute(
        "INSERT INTO receipts (filename, receipt_date, merchant, invoice_number, amount, transaction_id, raw_json) VALUES (?,?,?,?,?,?,?)",
        (
            filename,
            parsed.get("date"),
            merchant,
            invoice_number,
            amount,
            tx_id,
            json.dumps(parsed, ensure_ascii=False),
        ),
    )
    receipt_id = cur.lastrowid
    con.commit()

    # receipt_links に (receipt_id, tx_id) を記録する (= /api/transactions が
    # receipt_links JOIN で receipt_ids を取得するため、 これが無いと receipt_items
    # が UI に出てこない)。 was_matched に関係なく、 receipt_id + tx_id があれば常に link。
    if receipt_id and tx_id:
        con.execute(
            "INSERT OR IGNORE INTO receipt_links (receipt_id, transaction_id) VALUES (?, ?)",
            (receipt_id, tx_id),
        )
        con.commit()

    # 内訳 (= 上水道料金 + 下水道料金 のような複数事業者、 もしくは 1 商品の購入)
    # を receipt_items に展開。 1 商品でも label (= 商品名) を /invoices に出すため
    # len(items) >= 1 で INSERT (= 旧仕様は >1 で 1 商品は skip だった)。
    # 優先順: OCR が直接抽出した items[].merchant / items[].invoice_number > alias
    # マスタ (config/receipt_item_aliases.toml) > 親 receipt の merchant/invoice_number。
    items = parsed.get("items") or []
    if receipt_id and isinstance(items, list) and len(items) >= 1:
        from src.personal.receipt_item_aliases import resolve_item_alias
        for idx, it in enumerate(items):
            label = (it.get("name") or "").strip()
            sub_amount = it.get("price")
            try:
                sub_amount = int(sub_amount) if sub_amount is not None else None
            except (TypeError, ValueError):
                sub_amount = None
            if not label or sub_amount is None:
                continue
            # OCR 直接抽出が最優先
            sub_merchant = (it.get("merchant") or "").strip() or None
            sub_invoice = (it.get("invoice_number") or "").strip() or None
            # 取れていなければ alias マスタで補完
            if not sub_merchant or not sub_invoice:
                alias = resolve_item_alias(label)
                if alias:
                    if not sub_merchant and alias.get("merchant"):
                        sub_merchant = alias["merchant"]
                    if not sub_invoice and alias.get("invoice_number"):
                        sub_invoice = alias["invoice_number"]
            # 最後に親 receipt から継承
            sub_merchant = sub_merchant or merchant or ""
            sub_invoice = sub_invoice or (invoice_number or "")
            con.execute(
                "INSERT INTO receipt_items (receipt_id, label, merchant, "
                "invoice_number, amount, sort_order) VALUES (?,?,?,?,?,?)",
                (receipt_id, label, sub_merchant, sub_invoice, sub_amount, idx),
            )
        con.commit()

    con.close()

    # 紙レシート ↔ カード明細の突合（VPASS等の同期後に新しいレシートが入ったタイミング）
    try:
        from src.matching import match_receipts_to_cards
        match_receipts_to_cards(verbose=False)
    except Exception as e:
        print(f"[receipts] match_receipts_to_cards エラー: {e}")

    result = {
        "receipt": parsed,
        "transaction_id": tx_id,
        "saved_path": str(saved_path),
    }

    if was_matched:
        print(f"[receipts] transaction_id={tx_id} に突合しました")
        _push_result(
            f"突合完了 ¥{amount:,}",
            f"{merchant}\nカード/銀行明細と一致しました",
        )
    elif tx_id:
        print(f"[receipts] transactions に追加 (bank=レシート, id={tx_id})")
        _push_result(
            f"現金払いとして追加 ¥{amount:,}",
            f"{merchant}\n突合する明細がないため現金払いとして記録しました",
        )
    else:
        print(f"[receipts] 日付不明のためスキップ (amount={amount})")
        _push_result("レシート読み取り失敗", "レシートを認識できませんでした")

    return result
