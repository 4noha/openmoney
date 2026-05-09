#!/usr/bin/env bash
# OpenMoney — Linux デバッグ VM セットアップ
#
# 実行:
#   bash debug/setup-linux-debug.sh
#
# 必要なもの: macOS 13+ (Apple Silicon 推奨), Homebrew

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VM_NAME="openmoney"
LIMA_YAML="${REPO_ROOT}/debug/lima-openmoney.yaml"
GENERATED_YAML="/tmp/lima-openmoney-generated.yaml"

# ── 1. Lima インストール ──────────────────────────────────────
if ! command -v limactl &>/dev/null; then
    echo "→ Lima をインストール中..."
    brew install lima
    echo "✓ Lima インストール完了"
else
    echo "✓ Lima: $(limactl --version)"
fi

# ── 2. YAML にプロジェクトパスを埋め込む ─────────────────────
sed "s|OPENMONEY_ROOT_PLACEHOLDER|${REPO_ROOT}|g" \
    "${LIMA_YAML}" > "${GENERATED_YAML}"

# ── 3. VM 作成 / 起動 ────────────────────────────────────────
if limactl list --format '{{.Name}}' 2>/dev/null | grep -qx "${VM_NAME}"; then
    STATUS=$(limactl list --format '{{.Name}} {{.Status}}' | grep "^${VM_NAME} " | awk '{print $2}')
    if [ "${STATUS}" = "Running" ]; then
        echo "✓ VM「${VM_NAME}」は起動済みです"
    else
        echo "→ VM「${VM_NAME}」を起動中..."
        limactl start "${VM_NAME}"
    fi
else
    echo "→ VM「${VM_NAME}」を作成中（初回は Ubuntu イメージのダウンロードがあります）..."
    limactl start --name "${VM_NAME}" "${GENERATED_YAML}"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✓ VM 起動完了"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "【次のステップ】"
echo ""
echo "  1. VM のシェルを開く:"
echo "     limactl shell ${VM_NAME}"
echo ""
echo "  2. VM 内で仮想ディスプレイを起動:"
echo "     bash ${REPO_ROOT}/debug/start-display.sh"
echo ""
echo "  3. Mac ブラウザでデスクトップを開く:"
echo "     http://localhost:6080/vnc.html"
echo ""
echo "  4. VM 内でアプリを起動:"
echo "     export DISPLAY=:99"
echo "     cd ${REPO_ROOT}"
echo "     uv run python desktop.py"
echo ""
echo "  VM を停止したいとき:"
echo "     limactl stop ${VM_NAME}"
