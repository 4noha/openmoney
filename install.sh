#!/usr/bin/env bash
#
# OpenMoney ターミナルインストーラ
#
# 使い方 — 新しい PC のターミナルに以下を貼るだけ:
#
#   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/4noha/openmoney/main/install.sh)"
#
# または SSH で clone したい場合:
#
#   REPO_URL=git@github.com:4noha/openmoney.git \
#     /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/4noha/openmoney/main/install.sh)"
#
# 実行内容:
#   1. Homebrew インストール (macOS・未インストール時)
#   2. git インストール (未インストール時)
#   3. リポジトリを clone (既存ディレクトリがあれば git pull)
#   4. start.sh を実行 (uv / Node.js / Claude Code / daemon / ペアリング)
#

set -euo pipefail

OS="$(uname -s)"   # Darwin / Linux
REPO_URL="${REPO_URL:-https://github.com/4noha/openmoney.git}"
DEFAULT_DIR="$HOME/openmoney"

# ────────────────────────────────────────────────
# ヘッダー
# ────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  OpenMoney インストーラ"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  インストール先: ${DEFAULT_DIR}"
echo "  リポジトリ    : ${REPO_URL}"
echo ""
read -r -p "続けますか? [Y/n] " _ans
case "${_ans:-Y}" in
  [Nn]*) echo "中止。"; exit 0 ;;
esac

# ────────────────────────────────────────────────
# 1. Homebrew (macOS のみ)
# ────────────────────────────────────────────────
if [ "$OS" = "Darwin" ] && ! command -v brew &>/dev/null; then
  echo ""
  echo "→ Homebrew をインストール..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if [ -x /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [ -x /usr/local/bin/brew ]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
  echo "✓ Homebrew インストール完了"
fi

# ────────────────────────────────────────────────
# 2. git
# ────────────────────────────────────────────────
if ! command -v git &>/dev/null; then
  echo ""
  echo "→ git をインストール..."
  if [ "$OS" = "Darwin" ]; then
    brew install git
  else
    sudo apt-get install -y -q git
  fi
  echo "✓ git インストール完了"
fi

# ────────────────────────────────────────────────
# 3. clone / pull
# ────────────────────────────────────────────────
echo ""
if [ -d "$DEFAULT_DIR/.git" ]; then
  echo "→ 既存リポジトリを更新 (git pull)..."
  git -C "$DEFAULT_DIR" pull --ff-only
  echo "✓ 更新完了"
else
  echo "→ リポジトリを clone..."
  git clone "$REPO_URL" "$DEFAULT_DIR"
  echo "✓ clone 完了"
fi

# ────────────────────────────────────────────────
# 4. start.sh
# ────────────────────────────────────────────────
echo ""
echo "→ start.sh を実行..."
echo ""
cd "$DEFAULT_DIR"
exec ./start.sh
