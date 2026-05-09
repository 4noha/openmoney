# ペアリング API 仕様

`src/server/routes_pairing.py` で実装。
OpenMoney Eclipse (= Android アプリ) はこの API を叩いてペアリング・OTP 送受信する。

## エンドポイント

### `POST /api/pairing/create`
新規セットアップ ID を発行 (= 24h 有効、 1 回限り)。

**Request**: なし

**Response**:
```json
{
  "session_id": "AB3K7M2P",
  "uid": "<UUID>",
  "expires_at": "2026-05-11T00:00:00",
  "ttl_hours": 24
}
```

### `GET /api/pairing/sessions`
有効な (= 期限内 + 未使用) ペアリング session 一覧。

**Response**:
```json
[
  {
    "session_id": "AB3K7M2P",
    "uid": "<UUID>",
    "created_at": "2026-05-10T00:00:00",
    "expires_at": "2026-05-11T00:00:00"
  }
]
```

### `GET /api/pairing/devices`
登録済 Android デバイス一覧 (= マスク済 token)。

**Response**:
```json
{
  "uid": "<UUID>",
  "devices": [
    {
      "fcm_token": "fGxXXX...zZ12",
      "fcm_token_masked": "fGxXXXXX...zZ12",
      "device_name": "Pixel 7",
      "registered_at": "2026-05-10T00:00:00",
      "last_active_at": "2026-05-10T01:00:00"
    }
  ]
}
```

### `DELETE /api/pairing/devices/{token}`
デバイス解除 (= 以後 OTP push を送らない)。

**Response**: `{"deleted": 1}` or 404 if not found

### `POST /api/pairing/redeem`
**Android アプリから呼ぶ**: session_id を消費して自分の token を登録。

**Request**:
```json
{
  "session_id": "AB3K7M2P",
  "fcm_token": "fGxXXX...zZ12",
  "device_name": "Pixel 7"
}
```

**Response**:
```json
{
  "uid": "<UUID>",
  "ok": true,
  "session_id": "AB3K7M2P"
}
```

エラー:
- `404 not found`: session_id が存在しない
- `400 already used`: 既に使われた
- `400 expired`: 期限切れ

### `POST /api/pairing/cleanup`
期限切れ + 使用済 session を削除 (= メンテ用)。

**Response**: `{"deleted": N}`

## 文字種

session_id は 8 桁 alphanumeric (= `23456789ABCDEFGHJKLMNPQRSTUVWXYZ`)。
紛らわしい `0/O/1/I` は除外。 32^8 = 約 1.1兆通り (= 衝突リスク無視可)。

## OpenMoney Eclipse の使い方 (= 想定フロー)

1. 初回起動 → Setup wizard
2. ユーザに「PC で `/settings → Android アプリ ペアリング` から ID 発行」 を案内
3. 8 桁を入力 (= QR スキャン推奨、 後日実装)
4. `POST /api/pairing/redeem` で session 消費 + token 登録
5. 以後、 OTP 受信 → uid と紐付けて push

## 移植先 (= OpenMoney + 中間サーバ Cloud Functions)

OpenMoney のローカル API。
中間サーバ版では Cloud Functions に同じ shape の HTTPS エンドポイントを実装。

`src/server/routes_pairing.py` のロジックを Functions に置き換える際、 DB は
SQLite → Firestore に切替 (= スキーマは `docs/openmoney/README.md` 参照)。
