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
from pydantic import BaseModel

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
        if e.code == 401:
            raise HTTPException(
                status_code=401,
                detail="token_expired",
            )
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

    # 自 PC のトークン hash を計算して self_pc_token_id として付加
    # (UI 側で「この PC」バッジを正確に表示するため)
    try:
        import hashlib
        from src.security import get_secret
        raw_token = get_secret("OPENMONEY_PC_TOKEN") or ""
        if raw_token:
            body["self_pc_token_id"] = hashlib.sha256(raw_token.encode()).hexdigest()
    except Exception:
        pass
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


@app.delete("/api/users/pc/{pc_token_id}")
async def revoke_pc_endpoint(pc_token_id: str):
    """PC endpoint を解除 (= pc_token_id は GET /api/users response から取る)。

    注: 自 PC 自身を解除する場合は backend 側でトークンが失効するだけで
    ローカルの .env は残る。 完全削除は ./goodbye.sh を使うこと。
    """
    encoded = urllib.parse.quote(pc_token_id, safe="")
    status, body = _backend_request("DELETE", f"/api/pair/devices/pc/{encoded}")
    if status not in (200, 204):
        raise HTTPException(status_code=status, detail=body)
    return body


class RepairBody(BaseModel):
    code: str


@app.post("/api/users/repair")
async def repair_pairing(body: RepairBody):
    """UI から 8 桁コードを受け取って再ペアリングを実行する。

    openmoney_pair.main() と同じロジックで backend に POST し、
    取得した pc_token / ai_token を .env / tools/.claude/settings.local.json に保存する。
    """
    import socket
    from pathlib import Path
    from src.security import get_secret

    code = body.code.strip().upper()
    if len(code) < 6:
        raise HTTPException(status_code=400, detail="コードが短すぎます")

    try:
        backend_url = (get_secret("OPENMONEY_BACKEND_URL") or "").rstrip("/")
    except PermissionError:
        # 未ロック状態では暗号化済み .env を復号できない → DEFAULT_BACKEND を使う
        from scripts.openmoney_pair import DEFAULT_BACKEND
        backend_url = DEFAULT_BACKEND
    if not backend_url:
        raise HTTPException(status_code=503, detail="OPENMONEY_BACKEND_URL が未設定です")

    label = socket.gethostname() or "PC"
    req = urllib.request.Request(
        f"{backend_url}/api/pair/redeem",
        data=json.dumps({"session_id": code, "device_label": label}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
        except Exception:
            err = {"detail": e.reason}
        raise HTTPException(status_code=e.code, detail=err.get("detail", str(err)))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"backend 接続失敗: {e}")

    uid = data.get("uid", "")
    pc_token = data.get("pc_token", "")
    ai_token = data.get("ai_token", "")
    ai_token_id = data.get("ai_token_id", "")
    effective_backend = data.get("backend_url") or backend_url

    if not uid or not pc_token:
        raise HTTPException(status_code=502, detail=f"不正な応答: {data}")

    # .env 更新
    repo_root = Path(__file__).parent.parent.parent
    env_path = repo_root / ".env"
    lines = env_path.read_text().splitlines(keepends=True) if env_path.exists() else []
    skip = {
        "OPENMONEY_BACKEND_URL=", "OPENMONEY_USER_UID=", "OPENMONEY_PC_TOKEN=",
        "OPENMONEY_DEVICE_LABEL=", "OPENMONEY_USER_EMAIL=", "OPENMONEY_AI_TOKEN_ID=",
        "ANTHROPIC_API_KEY=", "ANTHROPIC_BASE_URL=",
    }
    lines = [l for l in lines if not any(l.startswith(k) for k in skip)]
    if lines and not lines[-1].endswith("\n"):
        lines.append("\n")
    lines += [
        f"OPENMONEY_BACKEND_URL={effective_backend}\n",
        f"OPENMONEY_USER_UID={uid}\n",
        f"OPENMONEY_PC_TOKEN={pc_token}\n",
        f"OPENMONEY_DEVICE_LABEL={label}\n",
        f"OPENMONEY_USER_EMAIL={uid}@openmoney.io\n",
    ]
    if ai_token_id:
        lines.append(f"OPENMONEY_AI_TOKEN_ID={ai_token_id}\n")
    if ai_token:
        lines += [
            f"ANTHROPIC_API_KEY={ai_token}\n",
            f"ANTHROPIC_BASE_URL={effective_backend}\n",
        ]
    env_path.write_text("".join(lines))
    os.chmod(env_path, 0o600)

    # 実行中プロセスの os.environ も更新（再起動なしで即反映）
    os.environ["OPENMONEY_BACKEND_URL"] = effective_backend
    os.environ["OPENMONEY_USER_UID"] = uid
    os.environ["OPENMONEY_PC_TOKEN"] = pc_token
    os.environ["OPENMONEY_DEVICE_LABEL"] = label
    os.environ["OPENMONEY_USER_EMAIL"] = f"{uid}@openmoney.io"
    if ai_token_id:
        os.environ["OPENMONEY_AI_TOKEN_ID"] = ai_token_id
    if ai_token:
        os.environ["ANTHROPIC_API_KEY"] = ai_token
        os.environ["ANTHROPIC_BASE_URL"] = effective_backend

    # tools/.claude/settings.local.json 更新
    if ai_token:
        from scripts.openmoney_pair import _write_claude_settings
        _write_claude_settings(
            repo_root / "tools" / ".claude" / "settings.local.json",
            backend_url=effective_backend,
            ai_token=ai_token,
        )

    # 新トークンで Tailscale / LAN URL を即時再登録
    import threading as _threading
    def _reregister():
        import src.openmoney_client as _oc
        _oc._last_reported = None  # 強制再申告
        for fn in (_oc.register_tailscale_self, _oc.register_lan_self):
            try:
                fn()
            except Exception:
                pass
    _threading.Thread(target=_reregister, daemon=True).start()

    return {
        "ok": True,
        "uid": uid,
        "pc_token_prefix": pc_token[:8],
        "ai_token_prefix": data.get("ai_token_prefix", ""),
    }
