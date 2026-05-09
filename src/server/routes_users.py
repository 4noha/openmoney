"""アプリユーザ (= 自 user の Android 端末 + 連携 PC) の一覧 + 削除を
backend (= openmoney-backend) 経由で管理。

PC 自身の認証 (= OPENMONEY_PC_TOKEN) で backend `/api/pair/devices` を proxy。
PC 側 settings UI (= /settings) から呼ばれる想定。

注: 自 PC 自身の削除は `/api/pair/self` (= goodbye.sh) 経由が正規。 ここの
DELETE は 「他 PC / 旧 Android 端末を整理する」 用途。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from fastapi import HTTPException

from src.server import app

_TIMEOUT = 15.0


def _backend_request(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """OPENMONEY_PC_TOKEN で backend を叩く。 (status, json) を返す。

    .env の OPENMONEY_* は security bootstrap で暗号化されている可能性があるため、
    必ず get_secret() 経由で plaintext を取得する (= os.environ 直読みだと '...🔒'
    のままで Bearer に乗ってしまい backend が 401 を返す)。
    """
    from src.security import get_secret

    try:
        base = (get_secret("OPENMONEY_BACKEND_URL") or "").rstrip("/")
        token = get_secret("OPENMONEY_PC_TOKEN") or ""
    except PermissionError as e:
        raise HTTPException(
            status_code=403,
            detail=f"未解錠: master password でログインしてから再度開いてください ({e})",
        )
    if not base or not token:
        raise HTTPException(
            status_code=503,
            detail="OPENMONEY_BACKEND_URL / OPENMONEY_PC_TOKEN が .env に未設定 "
                   "(= setup.sh / openmoney_pair で初期化してください)",
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        f"{base}{path}",
        method=method,
        headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read())
        except Exception:
            data = {"detail": e.reason}
        return e.code, data
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"backend 接続失敗: {type(e).__name__}: {e}",
        )


@app.get("/api/users")
async def list_app_users():
    """自 user (= 同 uid) の Android 端末 + 連携 PC を backend から取得。

    Response:
        {
            "uid": "...",
            "android_devices": [
                {
                    "fcm_token_id": "<sha256 hash>",  # 削除 path で使う
                    "fcm_token_masked": "...",
                    "device_name": "...",
                    "registered_at": "...",
                    "last_active_at": "..."
                }, ...
            ],
            "pc_endpoints": [
                {
                    "pc_token_masked": "...",
                    "device_label": "...",
                    "tailscale_url": "...",
                    "registered_at": "...",
                    "last_active_at": "..."
                }, ...
            ]
        }
    """
    status, body = _backend_request("GET", "/api/pair/devices")
    if status != 200:
        raise HTTPException(status_code=status, detail=body)
    return body


@app.delete("/api/users/android/{fcm_token_id}")
async def revoke_android_user(fcm_token_id: str):
    """Android 端末を解除 (= fcm_token_id は GET /api/users response から取る)。

    backend は SHA-256 hex hash を受け付けるので、 PC 側で plaintext を持って
    いなくても削除できる (= masked 値しか UI に出していない設計と整合)。
    """
    encoded = urllib.parse.quote(fcm_token_id, safe="")
    status, body = _backend_request("DELETE", f"/api/pair/devices/android/{encoded}")
    if status not in (200, 204):
        raise HTTPException(status_code=status, detail=body)
    return body
