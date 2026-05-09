#!/usr/bin/env bash
# OpenMoney — Claude Code 起動 (= setup.sh 完了後の日常起動用)
#
# ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / model は
# tools/.claude/settings.local.json に保存され、_export_anthropic_env.sh で
# シェル env に展開してから claude を起動する (= settings.json の env ブロックは
# 子プロセス専用で、モデル API 認証には起動時シェル env が必要なため)。
# CLAUDE_CONFIG_DIR を tools/.claude に向けて開発用 ~/.claude/ を汚染しない。
#
# 使い方:
#   scripts/run_claude.sh           # 対話モード
#   scripts/run_claude.sh -p "..."  # 1-shot プロンプト
#
# ※ 初回は setup.sh を使うこと。

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOLS_DIR="$REPO_ROOT/tools"
CLAUDE_BIN="$TOOLS_DIR/node_modules/.bin/claude"

if [ ! -x "$CLAUDE_BIN" ]; then
  echo "Claude Code が見つかりません。先に setup.sh を実行してください:"
  echo "  bash setup.sh"
  exit 1
fi

export CLAUDE_CONFIG_DIR="$TOOLS_DIR/.claude"

# settings.local.json の env (ANTHROPIC_AUTH_TOKEN / BASE_URL / model) を
# シェル env に export してから claude を起動する。
source "$REPO_ROOT/scripts/_export_anthropic_env.sh"

exec "$CLAUDE_BIN" "$@"
