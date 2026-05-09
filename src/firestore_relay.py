"""
Firestore リアルタイムリスナー: Android から書き込まれた瞬間に処理する。
ポーリング不要のため Firestore 読み取りコストがほぼゼロ。

env:
  FCM_SERVICE_ACCOUNT_PATH : Firebase service account JSON のパス
                             (= 既定 firebase-service-account.json)
  FCM_PROJECT_ID           : Firebase project ID (= storage bucket 名導出に使用)
  FCM_STORAGE_BUCKET       : 明示指定する場合 (= 既定 <FCM_PROJECT_ID>.firebasestorage.app)
"""
import os
import threading
from datetime import datetime
from pathlib import Path

_SA_PATH = Path(os.environ.get("FCM_SERVICE_ACCOUNT_PATH")
                 or (Path(__file__).parent.parent / "firebase-service-account.json"))
_PROJECT_ID = os.environ.get("FCM_PROJECT_ID", "")
_BUCKET = (
    os.environ.get("FCM_STORAGE_BUCKET")
    or (f"{_PROJECT_ID}.firebasestorage.app" if _PROJECT_ID else "")
)


def _init():
    import firebase_admin
    from firebase_admin import credentials, firestore
    if not firebase_admin._apps:
        # SA JSON があればそれを使う (= 個人 PC: setup.sh が download 済み)。
        # 無ければ ApplicationDefault credentials を試す
        # (= Cloud Run / GCE / GKE 等で metadata server から自動取得)。
        if _SA_PATH.exists():
            cred = credentials.Certificate(str(_SA_PATH))
        else:
            cred = credentials.ApplicationDefault()
        config: dict = {}
        if _BUCKET:
            config["storageBucket"] = _BUCKET
        firebase_admin.initialize_app(cred, config or None)
    return firestore.client()


def _on_commands(snapshots, changes, read_time, *, set_fn):
    for snap in snapshots:
        if not snap.exists:
            continue
        data = snap.to_dict()
        updates = {}

        for key in ("amazon_otp", "vpass_otp"):
            val = (data.get(key) or "").strip()
            if val:
                set_fn(key, val)
                print(f"[firestore] {key} 受信: {val[:2]}****")
                updates[key] = ""

        request_id = (data.get("accept_request_id") or "").strip()
        if request_id:
            set_fn("accept_status", "accepted")
            set_fn("pending_request_id", request_id)
            set_fn("accepted_at", datetime.now().isoformat())
            print(f"[firestore] Accept 受信: {request_id}")
            updates["accept_request_id"] = ""

        if data.get("run_now"):
            set_fn("run_now", "1")
            print("[firestore] 即時実行リクエスト受信")
            updates["run_now"] = False

        token = (data.get("device_token") or "").strip()
        if token:
            set_fn("device_token", token)
            set_fn("token_updated_at", datetime.now().isoformat())
            print(f"[firestore] FCM token 登録: {token[:20]}...")
            updates["device_token"] = ""

        if updates:
            snap.reference.update(updates)


def _detect_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return "image/jpeg"


def _on_receipts(snapshots, changes, read_time):
    from firebase_admin import storage
    from src.receipts import process_receipt

    processed_any = False
    for change in changes:
        if change.type.name != "ADDED":
            continue
        data = change.document.to_dict()
        if data.get("processed"):
            continue
        storage_path = data.get("storage_path", "")
        filename = data.get("filename", "receipt.jpg")
        try:
            bucket = storage.bucket()
            image_bytes = bucket.blob(storage_path).download_as_bytes()
            mime = data.get("mime_type") or _detect_mime(image_bytes)
            process_receipt(image_bytes, filename, mime)
            change.document.reference.update({"processed": True})
            processed_any = True
            print(f"[firestore] レシート処理完了: {filename}")
        except Exception as e:
            print(f"[firestore] レシート処理失敗 ({filename}): {e}")

    # process_receipt が transactions に行を INSERT している場合、 server の
    # in-memory tx cache が古いままだと /api/transactions が新しい行を返さない。
    # 1 件以上処理した時に明示的にキャッシュ invalidate する。
    if processed_any:
        try:
            from src.server import _invalidate_tx_cache
            _invalidate_tx_cache()
        except Exception as e:
            print(f"[firestore] tx cache invalidate 失敗: {e}")


def run_relay():
    """バックグラウンドスレッドで実行。リスナーを登録して待機し続ける。

    receipts: openmoney-backend が `/api/receipts/upload` で書き込む
    `users/{uid}/receipts/` subcollection を polling する (= 旧 mfw2 の
    `mfw2/receipts_queue/items` global path から uid scoped に移行済)。

    uid 解決順:
      1. `OPENMONEY_USER_UIDS` (csv 形式、 デモで複数 phone を 1 つの PC で
         共有するため): `uid_a,uid_b,uid_c` で複数 uid の receipts を同時 listen
      2. `OPENMONEY_USER_UID` (単一 uid、 通常の個人 PC 用)
      3. 未設定: 旧 mfw2 path にフォールバック (= dev / 過去データ救済)
    """
    from src.server import _set
    db = _init()

    commands_ref = db.collection("mfw2").document("commands")
    commands_ref.on_snapshot(
        lambda snaps, changes, t: _on_commands(snaps, changes, t, set_fn=_set)
    )

    uids_csv = os.environ.get("OPENMONEY_USER_UIDS", "").strip()
    if uids_csv:
        uids = [u.strip() for u in uids_csv.split(",") if u.strip()]
    else:
        single = os.environ.get("OPENMONEY_USER_UID", "").strip()
        uids = [single] if single else []

    if uids:
        for uid in uids:
            receipts_ref = (
                db.collection("users").document(uid)
                  .collection("receipts")
                  .where("processed", "==", False)
            )
            receipts_ref.on_snapshot(_on_receipts)
            print(f"[firestore] receipts polling: users/{uid}/receipts/")
    else:
        # legacy fallback: 旧 mfw2 path も同時に listen (= 過去データ救済 + dev 用)
        legacy_ref = (
            db.collection("mfw2").document("receipts_queue").collection("items")
              .where("processed", "==", False)
        )
        legacy_ref.on_snapshot(_on_receipts)
        print(
            "[firestore] OPENMONEY_USER_UID(S) 未設定 → 旧 mfw2/receipts_queue を polling "
            "(= setup.sh / openmoney_pair で .env を更新してください)"
        )

    print("[firestore] リアルタイムリスナー開始")
    threading.Event().wait()  # スレッドを生かし続ける
