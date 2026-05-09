"""Tailscale CLI 検知 + MagicDNS / IP 取得。

Tailscale が install されていれば self ノードの dns_name + ip を取得して、
ローカル daemon (= port 8765) を Tailscale 経由でアクセスする URL を返す。
未 install / 未起動 / not running なら None。

外部依存: `tailscale` CLI のみ (= `tailscale status --json`)。 Python ライブラリ追加なし。

使い方:
    from src.tailscale import detect_or_none
    info = detect_or_none()
    if info:
        print(info["url"])  # http://machine.tailnet.ts.net:8765/
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Optional, TypedDict


class TailscaleInfo(TypedDict, total=False):
    url: str          # http://<host>:<port>/
    dns_name: str     # MagicDNS フル名 (= "machine.tailnet.ts.net" or "")
    ipv4: str         # 100.x.x.x or ""
    online: bool
    tailnet: str      # MagicDNSSuffix (= "tailnet.ts.net" or "")


def _daemon_port() -> int:
    """OpenMoney daemon が listen している port (= DAEMON_PORT env、 default 8765)。"""
    raw = os.environ.get("DAEMON_PORT", "8765")
    try:
        return int(raw)
    except ValueError:
        return 8765


# Tailscale CLI 候補パス (= 上から順に試す)。 PATH にあればそれが最優先。
_TAILSCALE_CLI_CANDIDATES = (
    "tailscale",  # Linux / Windows / macOS Homebrew (= PATH 経由)
    # macOS Mac App Store 版 (= CLI が PATH に出ない、 install-cli する人もいるが しない人が多い)
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    # Linux 旧 deb / 一部 distro
    "/usr/bin/tailscale",
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
)


def _run_tailscale_status() -> Optional[str]:
    """tailscale status --json を試行。 stdout (str) or None。"""
    for cli in _TAILSCALE_CLI_CANDIDATES:
        try:
            r = subprocess.run(
                [cli, "status", "--json"],
                capture_output=True,
                timeout=3,
                text=True,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0:
            return r.stdout
    return None


def detect_self(daemon_port: Optional[int] = None) -> Optional[TailscaleInfo]:
    """Tailscale 検知。 動いていれば dict、 そうでなければ None。

    判定順:
      1. `tailscale` CLI 候補 (= PATH or macOS app bundle) のどれも実行不可 → None
      2. JSON parse 失敗 → None
      3. Self.DNSName / TailscaleIPs どちらも空 → None (= 未起動 / 未認証)

    成功時:
      - dns_name 優先 (= MagicDNS 有効時)
      - dns_name 空なら ipv4 (= 100.x.x.x、 MagicDNS 無効でも到達可)
      - URL は HTTP scheme (= tailscale serve --https は MVP では未対応)
    """
    port = daemon_port or _daemon_port()
    raw = _run_tailscale_status()
    if raw is None:
        return None
    try:
        s = json.loads(raw)
    except json.JSONDecodeError:
        return None

    self_node = s.get("Self") or {}
    dns_name = (self_node.get("DNSName") or "").rstrip(".")
    ips = self_node.get("TailscaleIPs") or []
    ipv4 = next((ip for ip in ips if "." in ip and ":" not in ip), "")
    online = bool(self_node.get("Online", False))
    tailnet = (s.get("MagicDNSSuffix") or "").rstrip(".")

    host = dns_name or ipv4
    if not host:
        return None  # MagicDNS 無効 + IP 取得失敗

    return TailscaleInfo(
        url=f"http://{host}:{port}/",
        dns_name=dns_name,
        ipv4=ipv4,
        online=online,
        tailnet=tailnet,
    )


def detect_or_none() -> Optional[TailscaleInfo]:
    """例外を握りつぶして None 返却。 routes / template から呼ぶ用。"""
    try:
        return detect_self()
    except Exception:
        return None
