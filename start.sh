#!/usr/bin/env bash
#
# OpenMoney 起動スクリプト (= 初回 setup と日常起動を 1 本に統合)
#
# 各ステップは「未完了なら実行・完了済みならスキップ」の idempotent 設計:
#   - uv / Node / Claude Code バイナリ : 既存ならスキップ、 無ければインストール
#   - Linux GTK / WebKit2             : dpkg 個別チェック、 不足分のみ apt-get
#   - Linux 用 venv (.venv-linux)     : 既存ならスキップ
#   - ペアリング (8 桁コード)         : tools/.claude/settings.local.json の
#                                       ANTHROPIC_AUTH_TOKEN + .env の
#                                       OPENMONEY_PC_TOKEN がそろっていればスキップ
#   - Firebase 設定                   : firebase-service-account.json と
#                                       FCM_PROJECT_ID 両方ありならスキップ
#   - daemon                          : /health で既起動ならスキップ
#   - 初期パスワード設定 (/login)     : .env に🔒(暗号化マーカー)があればスキップ
#
# 環境変数 (= optional override):
#   OPENMONEY_BACKEND_URL  : backend URL
#   DAEMON_PORT            : daemon ポート (= 既定 8765)
#

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

DAEMON_PORT="${DAEMON_PORT:-8765}"
TOOLS_DIR="$REPO_ROOT/tools"
OS="$(uname -s)"   # Darwin / Linux

# 埋め込み Claude Code の設定を tools/.claude/ に閉じ込める
# (= 開発用 ~/.claude/ を汚染しない)
export CLAUDE_CONFIG_DIR="$TOOLS_DIR/.claude"
mkdir -p "$CLAUDE_CONFIG_DIR"

# .env に KEY=VALUE を書き込む (既存の同名キーは上書き)
_env_set() {
  local key="$1" val="$2"
  local env_path="${REPO_ROOT}/.env"
  touch "$env_path"
  local tmp
  tmp=$(grep -v "^${key}=" "$env_path" || true)
  printf '%s\n' "$tmp" > "$env_path"
  echo "${key}=${val}" >> "$env_path"
  chmod 600 "$env_path"
}

# ─────────────────────────────────────────────────────────────
# ── 1. uv の確認 / インストール ──────────────────────────────
# ─────────────────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
  echo "→ uv をインストール..."
  if [ "$OS" = "Darwin" ] && command -v brew &>/dev/null; then
    brew install uv
  else
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi

# ─────────────────────────────────────────────────────────────
# ── 2. Linux: GTK / WebKit2 + Linux 用 venv ──────────────────
# ─────────────────────────────────────────────────────────────
if [ "$OS" = "Linux" ]; then
  MISSING_PKGS=()
  for pkg in python3-gi python3-gi-cairo gir1.2-gtk-3.0; do
    dpkg -s "$pkg" &>/dev/null || MISSING_PKGS+=("$pkg")
  done
  if ! dpkg -s gir1.2-webkit2-4.1 &>/dev/null && ! dpkg -s gir1.2-webkit2-4.0 &>/dev/null; then
    MISSING_PKGS+=("gir1.2-webkit2-4.1")
  fi
  if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    echo "→ GTK/WebKit2 パッケージをインストール (sudo)..."
    sudo apt-get install -y -q "${MISSING_PKGS[@]}"
  fi

  # macOS の .venv は Linux で使えない (バイナリ非互換)
  # GTK バインディングを system Python から拾うため --system-site-packages
  LINUX_VENV="${REPO_ROOT}/.venv-linux"
  if [ ! -d "$LINUX_VENV" ]; then
    echo "→ Linux 用 venv 作成 (--system-site-packages)..."
    uv venv --system-site-packages "$LINUX_VENV"
    UV_PROJECT_ENVIRONMENT="$LINUX_VENV" uv sync --frozen
  fi
  export UV_PROJECT_ENVIRONMENT="$LINUX_VENV"
fi

RUN="uv run"

# ─────────────────────────────────────────────────────────────
# ── 3. Node.js (Linux のみ自動インストール) ──────────────────
# ─────────────────────────────────────────────────────────────
if ! command -v npm &>/dev/null; then
  if [ "$OS" = "Darwin" ]; then
    echo "✗ Node.js が見つかりません。 brew install node でインストールしてください。" >&2
    exit 1
  else
    echo "→ Node.js をインストール (sudo)..."
    curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
    sudo apt-get install -y nodejs
  fi
fi

# ─────────────────────────────────────────────────────────────
# ── 4. Claude Code バイナリ ─────────────────────────────────
# ─────────────────────────────────────────────────────────────
CLAUDE_BIN=$(command -v claude 2>/dev/null) || CLAUDE_BIN=""
if [ -z "$CLAUDE_BIN" ] && [ -x "$TOOLS_DIR/node_modules/.bin/claude" ]; then
  CLAUDE_BIN="$TOOLS_DIR/node_modules/.bin/claude"
fi
if [ -z "$CLAUDE_BIN" ]; then
  echo "→ Claude Code を ./tools にインストール..."
  npm install --prefix "$TOOLS_DIR" @anthropic-ai/claude-code
  CLAUDE_BIN="$TOOLS_DIR/node_modules/.bin/claude"
fi

# ── 4b. Claude Code 初回ウィザード回避 (idempotent) ─────────
#   $CLAUDE_CONFIG_DIR/.claude.json + settings.json に
#   onboarding 完了 / trust accepted / bypass-warn skip を書き込む
_CLAUDE_VER=$("$CLAUDE_BIN" --version 2>/dev/null | grep -oE "[0-9]+\.[0-9]+\.[0-9]+" | head -1 || true)
[ -z "$_CLAUDE_VER" ] && _CLAUDE_VER="2.1.138"

python3 - "$CLAUDE_CONFIG_DIR/.claude.json" "$_CLAUDE_VER" "$REPO_ROOT" <<'PYEOF'
import json, sys, os, time
path, ver, repo_root = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = {}
if os.path.exists(path):
    try: cfg = json.loads(open(path).read())
    except Exception: cfg = {}
cfg["hasCompletedOnboarding"] = True
cfg["lastOnboardingVersion"] = ver
cfg.setdefault("autoUpdates", False)
cfg.setdefault("firstStartTime", time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()))
cfg.setdefault("theme", "dark")
cfg.setdefault("projects", {})
cfg["projects"].setdefault(repo_root, {})
cfg["projects"][repo_root]["hasTrustDialogAccepted"] = True
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
os.chmod(path, 0o600)
PYEOF

python3 - "$CLAUDE_CONFIG_DIR/settings.json" <<'PYEOF'
import json, sys, os
path = sys.argv[1]
cfg = {}
if os.path.exists(path):
    try: cfg = json.loads(open(path).read())
    except Exception: cfg = {}
cfg.setdefault("theme", "dark")
cfg["skipDangerousModePermissionPrompt"] = True
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
PYEOF

# ─────────────────────────────────────────────────────────────
# ── 5. 既存 .env から設定値を引き継ぐ ─────────────────────────
#       (= 再実行 / 別 OS での利用時に値が残るように)
# ─────────────────────────────────────────────────────────────
if [ -f "$REPO_ROOT/.env" ]; then
  if [ -z "${OPENMONEY_BACKEND_URL:-}" ]; then
    _url=$(grep "^OPENMONEY_BACKEND_URL=" "$REPO_ROOT/.env" | head -1 | cut -d= -f2- || true)
    [ -n "$_url" ] && export OPENMONEY_BACKEND_URL="$_url"
  fi
  if [ -z "${FCM_PROJECT_ID:-}" ]; then
    _fcm=$(grep "^FCM_PROJECT_ID=" "$REPO_ROOT/.env" | head -1 | cut -d= -f2- || true)
    [ -n "$_fcm" ] && export FCM_PROJECT_ID="$_fcm"
  fi
  if [ -z "${FCM_SERVICE_ACCOUNT_PATH:-}" ]; then
    _sa=$(grep "^FCM_SERVICE_ACCOUNT_PATH=" "$REPO_ROOT/.env" | head -1 | cut -d= -f2- || true)
    [ -n "$_sa" ] && export FCM_SERVICE_ACCOUNT_PATH="$_sa"
  fi
fi
: "${FCM_PROJECT_ID:=openmoney-app-prod}"

# ─────────────────────────────────────────────────────────────
# ── 6. ペアリング (= 未済ならインタラクティブ実行) ───────────
#       判定: settings.local.json に ANTHROPIC_AUTH_TOKEN +
#             .env に OPENMONEY_PC_TOKEN がそろっていれば完了扱い
# ─────────────────────────────────────────────────────────────
_pairing_done() {
  local s="$TOOLS_DIR/.claude/settings.local.json"
  local e="$REPO_ROOT/.env"
  [ -f "$s" ] || return 1
  [ -f "$e" ] || return 1
  python3 -c "
import json, sys
try:
    d = json.load(open('$s'))
    sys.exit(0 if (d.get('env') or {}).get('ANTHROPIC_AUTH_TOKEN') else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null || return 1
  grep -q "^OPENMONEY_PC_TOKEN=" "$e"
}

if _pairing_done; then
  echo "✓ ペアリング済み"
else
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  ペアリング (= Android アプリ「OpenMoney Eclipse」 との初回接続)"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  echo "  Android アプリで表示された 8 桁コードを入力してください。"
  read -r -p "  ペアリングコード (= 8 桁): " _PAIR_CODE
  echo ""
  $RUN -m scripts.openmoney_pair "$_PAIR_CODE"
fi

# ─────────────────────────────────────────────────────────────
# ── 7. Firebase 設定 (= OTP 中継・レシート受信) ──────────────
#       判定: SA JSON + FCM_PROJECT_ID 両方ありならスキップ
# ─────────────────────────────────────────────────────────────
_SA_DEFAULT="${FCM_SERVICE_ACCOUNT_PATH:-${REPO_ROOT}/firebase-service-account.json}"
if [ ! -f "$_SA_DEFAULT" ]; then
  _BACKEND_SA="$(dirname "$REPO_ROOT")/openmoney-backend/firebase-service-account.json"
  if [ -f "$_BACKEND_SA" ]; then
    cp "$_BACKEND_SA" "${REPO_ROOT}/firebase-service-account.json"
    _SA_DEFAULT="${REPO_ROOT}/firebase-service-account.json"
    echo "  ✓ firebase-service-account.json を openmoney-backend からコピー"
  fi
fi

if [ -f "$_SA_DEFAULT" ] && [ -n "${FCM_PROJECT_ID:-}" ]; then
  echo "✓ Firebase 設定済み (FCM_PROJECT_ID=${FCM_PROJECT_ID})"
  _env_set "FCM_PROJECT_ID" "$FCM_PROJECT_ID"
else
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  Firebase 設定 (OTP 中継・レシート受信) — Enter でスキップ可"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  read -r -p "  firebase-service-account.json のパス (= 省略時 ${_SA_DEFAULT}): " SA_INPUT
  SA_PATH="${SA_INPUT:-${_SA_DEFAULT}}"
  if [ -f "$SA_PATH" ]; then
    if [ "$SA_PATH" != "${REPO_ROOT}/firebase-service-account.json" ]; then
      _env_set "FCM_SERVICE_ACCOUNT_PATH" "$SA_PATH"
    fi
    read -r -p "  Firebase プロジェクト ID: " FCM_PROJECT_ID_INPUT
    if [ -n "$FCM_PROJECT_ID_INPUT" ]; then
      _env_set "FCM_PROJECT_ID" "$FCM_PROJECT_ID_INPUT"
      echo "✓ Firebase 設定を .env に保存"
    fi
  else
    echo "→ Firebase 設定をスキップ (後で .env に手動追記可)"
  fi
fi

# ─────────────────────────────────────────────────────────────
# ── 8. daemon 起動 (= 既起動ならスキップ) ────────────────────
# ─────────────────────────────────────────────────────────────
daemon_up() { curl -sf "http://localhost:${DAEMON_PORT}/api/ping" &>/dev/null; }

if daemon_up; then
  echo "✓ daemon は既に起動中 (port ${DAEMON_PORT})"
else
  echo "→ daemon を起動中..."
  $RUN -m src.daemon > "$REPO_ROOT/.daemon.log" 2>&1 &
  DAEMON_PID=$!
  echo "$DAEMON_PID" > "$REPO_ROOT/.daemon.pid"
  for _ in $(seq 1 30); do
    if daemon_up; then break; fi
    sleep 0.5
  done
  if daemon_up; then
    echo "✓ daemon 起動完了 (pid ${DAEMON_PID})"
  else
    echo "✗ daemon の起動に失敗しました。 ログ: .daemon.log" >&2
    exit 1
  fi
fi

# ─────────────────────────────────────────────────────────────
# ── 9. 初期パスワード設定 (= .env に🔒マーカーが無ければ /login へ誘導) ─
#       security.py の bootstrap で *_EMAIL を含む全平文値が暗号化され
#       🔒 サフィックスが付与される。 これがあれば設定済み。
# ─────────────────────────────────────────────────────────────
_password_set() {
  [ -f "$REPO_ROOT/.env" ] && grep -qF '🔒' "$REPO_ROOT/.env"
}

if ! _password_set; then
  LOGIN_URL="http://localhost:${DAEMON_PORT}/login"
  echo ""
  echo "→ 初期パスワード設定が必要です。 ブラウザを開きます: ${LOGIN_URL}"
  sleep 0.3
  if [ "$OS" = "Darwin" ]; then
    open "$LOGIN_URL" || true
  elif command -v xdg-open &>/dev/null && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    xdg-open "$LOGIN_URL" || true
  else
    echo "  ブラウザで ${LOGIN_URL} を開いてパスワードを設定してください。"
  fi
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  ブラウザで初期パスワードを設定したら Enter を押してください。"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  read -r -p "  [Enter で Claude Code を起動] "
fi

# ─────────────────────────────────────────────────────────────
# ── 10. Claude Code 起動 ─────────────────────────────────────
# ─────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✓ Server (localhost:${DAEMON_PORT}) + Claude Code を起動します"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

cd "$REPO_ROOT"

# settings.local.json の env (ANTHROPIC_AUTH_TOKEN / BASE_URL / model) を
# シェル env に export する。 Claude Code の env ブロックは子プロセス用で、
# モデル API 認証には起動時のシェル env しか見ないため。
source "$REPO_ROOT/scripts/_export_anthropic_env.sh"

exec "$CLAUDE_BIN" --dangerously-skip-permissions
