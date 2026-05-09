import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2 import service_account

load_dotenv()

_ROOT = Path(__file__).parent.parent
_CFG: dict = {}
_cfg_path = _ROOT / "config.json"
if _cfg_path.exists():
    _CFG = json.loads(_cfg_path.read_text())

_PROJECT_ID = os.environ.get("FCM_PROJECT_ID") or _CFG.get("FCM_PROJECT_ID", "")
_SA_PATH = Path(os.environ.get("FCM_SERVICE_ACCOUNT_PATH") or _CFG.get("FCM_SERVICE_ACCOUNT_PATH", "firebase-service-account.json"))
_FCM_URL = f"https://fcm.googleapis.com/v1/projects/{_PROJECT_ID}/messages:send"
_SCOPES = ["https://www.googleapis.com/auth/firebase.messaging"]

_creds: service_account.Credentials | None = None


def _get_token() -> str:
    global _creds
    if _creds is None:
        sa_file = _SA_PATH if _SA_PATH.is_absolute() else Path(__file__).parent.parent / _SA_PATH
        _creds = service_account.Credentials.from_service_account_file(str(sa_file), scopes=_SCOPES)
    if not _creds.valid:
        _creds.refresh(Request())
    return _creds.token


def send(token: str, title: str, body: str, data: dict | None = None) -> bool:
    # notification フィールドを含めない（data-only）ことで、
    # アプリがバックグラウンドでも onMessageReceived を必ず経由させる
    payload = {
        "message": {
            "token": token,
            "data": {
                "title": title,
                "body": body,
                **{k: str(v) for k, v in (data or {}).items()},
            },
            "android": {"priority": "high"},
        }
    }
    headers = {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
    }
    resp = httpx.post(_FCM_URL, json=payload, headers=headers, timeout=15)
    if resp.status_code == 200:
        print(f"  FCM送信成功: {title}")
        return True
    print(f"  FCM送信失敗 {resp.status_code}: {resp.text}")
    return False


def send_visual(token: str, title: str, body: str,
                  data: dict | None = None) -> bool:
    """notification + data 両方含む payload (= 通知バーに自動表示される)。

    Android アプリの onMessageReceived 実装に依存せず、 OS が直接通知を表示する。
    アプリ未起動 / バックグラウンド / 起動中 いずれでも視覚通知が出る。
    """
    payload = {
        "message": {
            "token": token,
            "notification": {"title": title, "body": body},
            "data": {
                "title": title, "body": body,
                **{k: str(v) for k, v in (data or {}).items()},
            },
            "android": {
                "priority": "high",
                "notification": {"channel_id": "mf2_login", "default_sound": True},
            },
        }
    }
    headers = {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
    }
    resp = httpx.post(_FCM_URL, json=payload, headers=headers, timeout=15)
    if resp.status_code == 200:
        print(f"  FCM (visual) 送信成功: {title}")
        return True
    print(f"  FCM (visual) 送信失敗 {resp.status_code}: {resp.text}")
    return False
