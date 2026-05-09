#!/usr/bin/env bash
# Linux デスクトップランチャー インストールスクリプト
#
# 実行:
#   bash scripts/install_desktop.sh
#
# 前提:
#   uv がインストール済み (https://docs.astral.sh/uv/)
#   pywebview のシステム依存パッケージが入っている:
#     sudo apt install python3-gi python3-gi-cairo \
#         gir1.2-gtk-3.0 gir1.2-webkit2-4.1

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESKTOP_DIR="${HOME}/.local/share/applications"
DESKTOP_FILE="${DESKTOP_DIR}/openmoney.desktop"

# uv の確認
if ! command -v uv &>/dev/null; then
    echo "✗ uv が見つかりません。"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "  を実行してから再実行してください。"
    exit 1
fi

# pywebview のシステム依存確認 (GTK/WebKit)
if ! python3 -c "import gi; gi.require_version('WebKit2', '4.1')" 2>/dev/null && \
   ! python3 -c "import gi; gi.require_version('WebKit2', '4.0')" 2>/dev/null; then
    echo "! WebKit2GTK が見つかりません。以下を実行してください:"
    echo "  sudo apt install python3-gi python3-gi-cairo \\"
    echo "      gir1.2-gtk-3.0 gir1.2-webkit2-4.1"
    echo "  (インストール後、このスクリプトを再実行)"
    echo ""
    echo "  → それでも続行しますか？ [y/N]"
    read -r ans
    [[ "$ans" =~ ^[Yy]$ ]] || exit 1
fi

# Python 依存インストール
echo "→ Python 依存パッケージをインストール中..."
cd "$REPO_ROOT"
uv sync
echo "✓ uv sync 完了"

mkdir -p "$DESKTOP_DIR"

sed "s|__REPO_ROOT__|${REPO_ROOT}|g" \
    "${REPO_ROOT}/openmoney.desktop" > "$DESKTOP_FILE"

chmod +x "$DESKTOP_FILE"
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo "✓ ランチャーをインストールしました: ${DESKTOP_FILE}"
echo ""
echo "起動方法:"
echo "  1. アプリメニューから「OpenMoney」を選択"
echo "  2. または: uv run python ${REPO_ROOT}/desktop.py"
