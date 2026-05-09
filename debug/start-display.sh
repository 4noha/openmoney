#!/usr/bin/env bash
# Lima VM 内で実行するスクリプト。
# Xvfb (仮想ディスプレイ) + noVNC を起動し、
# Mac ブラウザから http://localhost:6080/vnc.html でデスクトップを表示できるようにする。
#
# 使い方:
#   limactl shell openmoney
#   bash /path/to/openmoney/debug/start-display.sh
#   # → 別ターミナルで: DISPLAY=:99 uv run python /path/to/openmoney/desktop.py

set -euo pipefail

DISPLAY_NUM=99
SCREEN_RES="1280x900x24"
VNC_PORT=5900
NOVNC_PORT=6080

# Xvfb が既に動いていたら再起動
pkill Xvfb 2>/dev/null || true
pkill x11vnc 2>/dev/null || true
pkill websockify 2>/dev/null || true
sleep 0.5

echo "→ Xvfb (仮想ディスプレイ :${DISPLAY_NUM}) を起動..."
Xvfb :${DISPLAY_NUM} -screen 0 ${SCREEN_RES} &
sleep 1

echo "→ openbox (ウィンドウマネージャ) を起動..."
DISPLAY=:${DISPLAY_NUM} openbox --replace &
sleep 0.5

echo "→ x11vnc を起動..."
x11vnc -display :${DISPLAY_NUM} \
    -nopw -forever -shared -quiet \
    -rfbport ${VNC_PORT} \
    -bg

echo "→ noVNC を起動 (port ${NOVNC_PORT})..."
websockify --web /usr/share/novnc \
    ${NOVNC_PORT} localhost:${VNC_PORT} \
    --daemon 2>/dev/null \
|| websockify --web /usr/share/novnc/ \
    ${NOVNC_PORT} localhost:${VNC_PORT} &

sleep 1

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✓ 準備完了"
echo "  Mac ブラウザで開く:"
echo "    http://localhost:6080/vnc.html"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
REPO_ROOT="$(dirname "$(dirname "$(realpath "$0")")")"
echo "アプリ起動コマンド (このターミナルで実行):"
echo "  export DISPLAY=:${DISPLAY_NUM}"
echo "  export UV_PROJECT_ENVIRONMENT=/tmp/openmoney-linux-venv"
echo "  cd ${REPO_ROOT}"
echo "  # 初回のみ (system-site-packages で gi を参照)"
echo "  uv venv --system-site-packages /tmp/openmoney-linux-venv && uv sync --frozen"
echo "  # 起動"
echo "  /tmp/openmoney-linux-venv/bin/python desktop.py"
