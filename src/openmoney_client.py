"""openmoney-backend HTTP client (= PC daemon が backend に自己申告するため)。

役割:
  - daemon 起動時に Tailscale URL + LAN URL を検出 → backend に PATCH /api/pair/self/info
  - 1h おきに再検出 → 変化があれば再申告
  - backend 接続エラー / token 無効でも daemon 自体は止めない (= ベストエフォート)

環境変数 (= `.env` から load_dotenv 済の前提):
  OPENMONEY_BACKEND_URL  : backend URL (= setup.sh で書かれている)
  OPENMONEY_PC_TOKEN     : 認証用 bearer (= setup.sh で書かれている)
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import urllib.request
import urllib.error
from typing import Optional

from src.tailscale import detect_or_none

_DEFAULT_TIMEOUT = 10.0


def _backend_url() -> Optional[str]:
    """OPENMONEY_BACKEND_URL を get_secret 経由で取得 (= 暗号化済 .env を復号)。
    未解錠 / 未設定 / 復号失敗 はすべて None で返し呼出側で 「未設定」 扱い。
    """
    try:
        from src.security import get_secret
        url = (get_secret("OPENMONEY_BACKEND_URL") or "").rstrip("/")
        return url or None
    except Exception:
        return None


def _pc_token() -> Optional[str]:
    """OPENMONEY_PC_TOKEN を get_secret 経由で取得 (= 同上)。"""
    try:
        from src.security import get_secret
        return get_secret("OPENMONEY_PC_TOKEN") or None
    except Exception:
        return None


def _patch_self_info(payload: dict) -> tuple[int, str]:
    """PATCH /api/pair/self/info を urllib で叩く。 (status, body) を返す。
    backend 設定不備や接続エラーは (0, "...") で返す。
    """
    url = _backend_url()
    token = _pc_token()
    if not url or not token:
        return 0, "OPENMONEY_BACKEND_URL or OPENMONEY_PC_TOKEN not configured"
    req = urllib.request.Request(
        f"{url}/api/pair/self/info",
        method="PATCH",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return e.code, body
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


# ─────────────────────────────────────────────
# Tailscale URL 申告 (= daemon 起動時 + 定期)
# ─────────────────────────────────────────────

# 直近申告した URL を保持 (= 変化検知用、 同じ URL を毎回 PATCH しない)
_last_reported: Optional[str] = None


def reset_last_reported() -> None:
    """次回 register_*_self() 呼び出し時に強制再申告させる（再ペアリング後に使用）。"""
    global _last_reported
    _last_reported = None


def register_tailscale_self() -> Optional[str]:
    """Tailscale URL を検出 → backend に申告 (= 変化があれば PATCH)。

    戻り値:
      - 申告した URL (= 変化あり / 初回)
      - "" (= Tailscale 検出されなかった、 既に null 申告済み)
      - None (= backend 設定不備 or 接続エラーで申告不能)

    daemon 起動時に 1 回 + 定期 (= 1h おき) に呼ぶ想定。
    """
    global _last_reported
    info = detect_or_none()
    new_url = info.get("url") if info else None

    # 初回 + 変化検知
    if new_url == _last_reported:
        return _last_reported  # no change, no PATCH

    status, body = _patch_self_info({"tailscale_url": new_url})
    if status == 200:
        _last_reported = new_url
        if new_url:
            print(f"[openmoney_client] tailscale_url 申告成功: {new_url}")
        else:
            print("[openmoney_client] tailscale_url を null に clear (= Tailscale 未検出)")
        return new_url
    elif status == 0:
        # 設定不備 or 接続エラー: 黙って fail (= daemon 動作は継続)
        print(f"[openmoney_client] tailscale_url 申告 skip: {body}")
        return None
    else:
        print(f"[openmoney_client] tailscale_url 申告失敗 (HTTP {status}): {body[:200]}")
        return None


# ─────────────────────────────────────────────
# LAN URL 申告 (= 同 LAN 内の Android 端末が Tailscale なしで開けるように)
# ─────────────────────────────────────────────

# 直近申告した LAN URLs (= 変化検知用)
_last_lan_urls: Optional[list[str]] = None


def _detect_lan_ips() -> list[str]:
    """RFC 1918 private 範囲の interface IP を検出。

    候補:
      - 10.0.0.0/8
      - 172.16.0.0/12
      - 192.168.0.0/16
    Tailscale (= 100.64.0.0/10 CGNAT) や loopback / link-local は除外。
    Docker bridge (= 172.17.0.0/16 等) は技術的に private 範囲だが、
    LAN 内端末からは到達できない bridge IP なので含めない方針:
      - 172.17, 172.18, ... 172.31 のうち docker0 / br- に紐付くものは除外。
      - 簡易的に socket.gethostbyname_ex + getaddrinfo を使う実装では区別が
        付かないので、 全 RFC 1918 を含めて clients 側でプローブで弾く方針。

    複数 IP がヒットした場合は全部返す (= 複数 NIC 環境想定、 client がプローブ)。
    """
    ips: set[str] = set()
    try:
        # AF_INET 全 interface 列挙: getaddrinfo(hostname, None) は loopback しか
        # 返さない環境があるので、 socket.gethostname() + getaddrinfo を併用。
        host = socket.gethostname()
        for fam, _, _, _, sockaddr in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = sockaddr[0]
            ips.add(ip)
    except Exception:
        pass
    # macOS / Linux で広く拾うため、 UDP socket で 「外向き」 の primary IP も追加
    # (= ルーティング table が default で選ぶ interface の IP)。
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # 実通信せず route 計算だけ
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass

    private = []
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if not addr.is_private:
            continue
        if addr.is_loopback or addr.is_link_local:
            continue
        # Tailscale CGNAT (100.64.0.0/10) は private 判定されないが念のため除外
        if str(addr).startswith("100.") and 64 <= int(str(addr).split(".")[1]) <= 127:
            continue
        private.append(str(addr))
    private.sort()
    return private


def register_lan_self() -> Optional[list[str]]:
    """LAN IP を検出 → URL 化して backend に申告 (= 変化があれば PATCH)。

    port は server PORT 環境変数 / DAEMON_PORT 環境変数 / 既定 8765 の順。

    戻り値:
      - 申告した URL リスト (= 変化あり / 初回)
      - [] (= LAN 検出されなかった、 既に空申告済み)
      - None (= backend 設定不備 or 接続エラーで申告不能)
    """
    global _last_lan_urls
    port = os.environ.get("DAEMON_PORT") or os.environ.get("PORT") or "8765"
    ips = _detect_lan_ips()
    new_urls = sorted([f"http://{ip}:{port}/" for ip in ips])

    if new_urls == (_last_lan_urls or []):
        return _last_lan_urls or []  # no change, no PATCH

    status, body = _patch_self_info({"lan_urls": new_urls or None})
    if status == 200:
        _last_lan_urls = new_urls
        if new_urls:
            print(f"[openmoney_client] lan_urls 申告成功: {new_urls}")
        else:
            print("[openmoney_client] lan_urls を null に clear (= LAN 未検出)")
        return new_urls
    elif status == 0:
        print(f"[openmoney_client] lan_urls 申告 skip: {body}")
        return None
    else:
        print(f"[openmoney_client] lan_urls 申告失敗 (HTTP {status}): {body[:200]}")
        return None
