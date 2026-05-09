#!/usr/bin/env bash
# OpenMoney — Claude Code 初回セットアップ
#
# やること:
#   1. Node.js 18+ 確認
#   2. @anthropic-ai/claude-code をプロジェクトローカルに npm install
#   3. .env に ANTHROPIC_API_KEY があるか確認
#   4. run_claude.sh に実行権限を付与
#
# 使い方:
#   bash scripts/setup_claude.sh

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*" >&2; }

echo ""
echo "=== OpenMoney — Claude Code セットアップ ==="
echo ""

# ── 1. Node.js ────────────────────────────────
if ! command -v node &>/dev/null; then
    err "Node.js が見つかりません。https://nodejs.org から v18+ をインストールしてください。"
    exit 1
fi

NODE_VER=$(node -e "process.stdout.write(String(process.versions.node.split('.')[0]))")
if [ "$NODE_VER" -lt 18 ]; then
    err "Node.js v${NODE_VER} は古すぎます。v18 以上が必要です。"
    exit 1
fi
ok "Node.js v$(node --version)"

# ── 2. npm (package.json がなければ作成) ──────
if [ ! -f "$REPO_ROOT/package.json" ]; then
    warn "package.json がないため作成します。"
    cat > "$REPO_ROOT/package.json" <<'EOF'
{
  "private": true,
  "description": "OpenMoney — Claude Code local dev",
  "devDependencies": {}
}
EOF
fi

# Claude Code がすでに入っていれば skip
CLAUDE_BIN="$REPO_ROOT/node_modules/.bin/claude"
if [ -x "$CLAUDE_BIN" ]; then
    ok "Claude Code はすでにインストール済みです → $CLAUDE_BIN"
else
    echo "→ @anthropic-ai/claude-code をインストール中..."
    npm install --save-dev @anthropic-ai/claude-code
    ok "Claude Code インストール完了 → $CLAUDE_BIN"
fi

CLAUDE_VER=$("$CLAUDE_BIN" --version 2>/dev/null || echo "unknown")
ok "バージョン: $CLAUDE_VER"

# ── 3. .env の ANTHROPIC_API_KEY 確認 ────────
ENV_FILE="$REPO_ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
    err ".env ファイルが見つかりません。.env.example をコピーして ANTHROPIC_API_KEY を設定してください:"
    echo "    cp .env.example .env && nano .env"
    exit 1
fi

if grep -q '^ANTHROPIC_API_KEY=.\+' "$ENV_FILE"; then
    ok "ANTHROPIC_API_KEY が .env に設定されています"
else
    warn "ANTHROPIC_API_KEY が .env に設定されていません。"
    warn "mock proxy を動かすには .env に以下を追加してください:"
    warn "    ANTHROPIC_API_KEY=sk-ant-..."
    warn "(設定後に再度 setup_claude.sh を実行してください)"
    MISSING_KEY=1
fi

# ── 4. run_claude.sh に実行権限 ───────────────
chmod +x "$REPO_ROOT/scripts/run_claude.sh"
ok "run_claude.sh に実行権限を付与しました"

# ── 5. .gitignore に node_modules / package-lock を追加 ───
GITIGNORE="$REPO_ROOT/.gitignore"
if [ -f "$GITIGNORE" ]; then
    for ENTRY in "node_modules/" "package-lock.json"; do
        if ! grep -qxF "$ENTRY" "$GITIGNORE"; then
            echo "$ENTRY" >> "$GITIGNORE"
            ok ".gitignore に $ENTRY を追加しました"
        fi
    done
else
    printf "node_modules/\npackage-lock.json\n" > "$GITIGNORE"
    ok ".gitignore を作成しました"
fi

echo ""
echo "=== セットアップ完了 ==="
echo ""
if [ -z "${MISSING_KEY:-}" ]; then
    echo "次のコマンドで Claude Code を起動できます:"
    echo ""
    echo "    scripts/run_claude.sh"
    echo ""
    echo "  (Haiku 4.5 専用の mock proxy が自動で起動します)"
else
    echo ".env に ANTHROPIC_API_KEY を設定後、以下で起動:"
    echo ""
    echo "    scripts/run_claude.sh"
fi
echo ""
