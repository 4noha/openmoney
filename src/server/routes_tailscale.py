"""Tailscale 検知 API。 settings.html から fetch されて URL 表示用。

認証: 不要 (= localhost or Tailscale 経由でしか到達できない局所 daemon、
かつ返却内容が「自 PC の Tailscale URL」 という低 sensitivity 情報)。
"""
from __future__ import annotations

from src.server import app
from src.tailscale import detect_or_none


@app.get("/api/tailscale/info")
async def api_tailscale_info():
    """Tailscale が動いていれば self の URL + dns_name + ipv4 を返す。

    Returns:
        - 動作中:  {"available": True, "url": ..., "dns_name": ..., "ipv4": ..., ...}
        - 未動作:  {"available": False, "reason": "..."}
    """
    info = detect_or_none()
    if info is None:
        return {
            "available": False,
            "reason": (
                "Tailscale CLI が見つからないか、 起動していません。 "
                "https://tailscale.com から install + login してください。"
            ),
        }
    return {
        "available": True,
        "url": info.get("url"),
        "dns_name": info.get("dns_name") or None,
        "ipv4": info.get("ipv4") or None,
        "online": info.get("online", False),
        "tailnet": info.get("tailnet") or None,
    }
