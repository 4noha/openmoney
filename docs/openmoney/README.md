# OpenMoney + OpenMoney Eclipse 設計

## 製品名

| repo | 名前 | 役割 |
|--|--|--|
| `OpenMoney` | OSS サーバ | ローカルで動く Web アプリ |
| `OpenMoney Eclipse` | OSS Android | OTP 中継 + push 受信 |
| (中間サーバ) | 運営者の Firebase project | ペアリング + Push 中継 |

## 全体フロー

```
[ユーザ A の PC]                [運営者の Firebase]              [ユーザ A の Android #1]
┌────────────────┐              ┌──────────────────┐             ┌────────────────────┐
│ OpenMoney      │              │ Firestore        │             │ OpenMoney Eclipse  │
│ (= ローカル)    │ ←── pair ──→│ /sessions/{8桁}  │←── pair ───→│                    │
│                │              │ /users/{uid}/    │             │                    │
│                │ ←── OTP ────│   devices/{tk}   │←── OTP ─────│ (銀行 push 受信)    │
│                │              │   pc_endpoint    │             │                    │
└────────────────┘              └──────────────────┘             └────────────────────┘
                                          ↑                              ↑
                                          └───── 同じ uid に ────────────┘
                                                 別端末 #2 も登録可能
                                                 (= タブレット / 別スマホ)
```

## ペアリングフロー

1. **OpenMoney 初回起動**
   - 中間サーバに新規 `user_id (UUID)` を作成リクエスト
   - サーバがランダム 8 桁の `session_id` を発行 (= 24h 有効)
   - PC 画面に QR コード or 手動入力用の 8 桁を表示

2. **OpenMoney Eclipse でペアリング**
   - 8 桁を入力 or QR スキャン
   - 端末の FCM token をサーバに登録 (= `/users/{uid}/devices/{token}`)
   - サーバがペアリング完了を OpenMoney に push

3. **2 台目以降の Android 追加**
   - OpenMoney 側で再度 session_id 発行 (= 同 uid 紐付け)
   - 別端末で入力 → 同 uid に追加登録
   - これで 1 ユーザに複数端末がぶら下がる

## DB スキーマ (= Firestore)

```typescript
sessions/{session_id}                   // 8 桁ランダム、 24h で expire
  user_id: string                        // ペアリング先 uid
  expires_at: Timestamp
  used: boolean                          // 1 回使ったら true、 同じ id で再ペアリング不可

users/{uid}                              // UUID
  created_at: Timestamp
  pc_endpoint: string                    // 例: "https://user-pc.local:8765"
                                         // (= ローカル LAN 内アドレス、 NAT 越えなしの想定)
  paired_devices: map<token, deviceInfo> // 複数端末

users/{uid}/devices/{fcm_token}
  device_name: string                    // "Pixel 7", "iPad Pro" 等
  registered_at: Timestamp
  last_active_at: Timestamp

users/{uid}/otp_queue/{queue_id}         // 端末 → PC への OTP 中継
  code: string                           // 暗号化推奨 (= AES-256)
  source_app: string                     // 銀行アプリ名
  received_at: Timestamp
  consumed_at: Timestamp | null

users/{uid}/notifications/{notif_id}     // PC → 端末への push (= "OTP 入れて" 通知)
  title: string
  body: string
  target_devices: token[]                // 全端末 or 特定端末
  sent_at: Timestamp
```

## Firestore セキュリティルール

```javascript
match /sessions/{sid} {
  // 認証なしで read/write 可 (= ペアリング前)。 ただし 8 桁ランダム + 24h 失効で十分なリスク低
  allow read, write: if true;
}
match /users/{uid} {
  // user_id は session で取得した値、 一致するクライアントのみ書込
  allow read, write: if request.auth.uid == uid;
}
```

→ 実は session_id ベースの認証だと Firebase Auth 必須ではない (= custom token を Cloud Functions で発行する方式)。

## OpenMoney 月額課金 (= Google Play Billing)

| プラン | 価格 | 機能 |
|--|--|--|
| Free | 無料 | OTP 中継 月 10 回まで、 端末 1 台 |
| Pro | ¥500/月 | OTP 無制限、 端末最大 5 台、 push 履歴 90 日保持 |
| Family | ¥980/月 | 5 user 共有、 端末無制限 |

(= 試算、 後で調整)

## 実装ロードマップ

### Phase 1: 設計確定 + リポジトリ準備 (1 週間)

- [ ] OpenMoney repo セットアップ
- [ ] OpenMoney Eclipse repo セットアップ
- [ ] 中間サーバ Firebase project 作成 (= 例: `openmoney-relay`)
- [ ] Firestore schema 確定 (= 上記)
- [ ] Cloud Functions 設計 (= /api/session/create, /api/session/redeem 等)

### Phase 2: 中間サーバ実装 (1〜2 週間)

- [ ] Cloud Functions: session 発行 + redeem
- [ ] Custom token 発行 (= Firebase Auth)
- [ ] Firestore セキュリティルール
- [ ] OTP 中継 (= devices subcollection ↔ FCM)

### Phase 3: OpenMoney (= サーバ) 改修 (1〜2 週間)

- [ ] Firebase project ID hard-coded 排除 (= `.env` 経由)
- [ ] /login UI に「クラウド利用 / セルフホスト」 切替
- [ ] 中間サーバ クラウド利用時のフロー:
  - [ ] 起動時に session_id 発行 → 画面に QR / 8 桁表示
  - [ ] Firestore リスナーで OTP 受信 → /api/otp/receive に渡す
  - [ ] FCM 送信を中間サーバ経由に
- [ ] セルフホスト mode は既存の挙動を維持

### Phase 4: OpenMoney Eclipse 改修 (= 2 週間)

- [ ] 初回起動 setup wizard
- [ ] 8 桁入力 / QR スキャン → ペアリング
- [ ] FCM token 取得 + サーバ登録
- [ ] OTP 受信 → 中間サーバへ POST
- [ ] Push 受信 → 通知表示
- [ ] 「別端末追加」 ボタン
- [ ] Play Billing 統合

### Phase 5: 法務・運用 (1〜2 週間)

- [ ] 利用規約 / プライバシーポリシー (= Web で公開、 アプリ内リンク)
- [ ] 特定商取引法表記
- [ ] サポート Email
- [ ] OSS LICENSE (= MIT)
- [ ] README + setup walkthrough (= スクショ付き)

### Phase 6: Closed Beta → Open Release (2〜4 週間)

- [ ] Internal Test (= 数人)
- [ ] Closed Test (= 20 人招待)
- [ ] Open Test → 公開

## 質問待ち / 未決事項

- [ ] OpenMoney 利用者の Firebase Auth 方式 (= Email+PW / Google / 匿名 + custom token)
- [ ] Play Billing の subscription SKU 構成 (= 価格 / 期間 / 機能差)
- [ ] LAN 内 NAT 越えの方針 (= ローカル URL 直接 / Tailscale / WebSocket relay)
- [ ] サポート言語 (= 日本語のみ / 英語も)
- [ ] サーバ side のホスティング (= Firebase Hosting + Cloud Functions / Cloud Run / 自前 VPS)
