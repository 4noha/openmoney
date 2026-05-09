#!/usr/bin/env bash
#
# OpenMoney resume.sh
#
# 用途: 別 PC から `.env` (+ optional `transactions.db`、 `tools/.claude/settings.local.json`)
#       を持ち込んだ後、 既存 `OPENMONEY_PC_TOKEN` がまだ有効か確認する。
#
# シナリオ:
#   - 機種変更 / 再インストール後、 旧 PC で goodbye.sh していなければ pc_token は
#     まだ生きている。 それを再利用すれば再ペアリング不要。
#   - goodbye 済の場合は 401 が返るので setup.sh に誘導。
#
# 環境変数 (= `.env` から読込):
#   OPENMONEY_BACKEND_URL  : backend URL
#   OPENMONEY_PC_TOKEN     : 既存 token
#

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

if [[ ! -f .env ]]; then
  echo "✗ .env がありません。"
  echo "  初回セットアップ: ./setup.sh"
  echo "  別 PC から復旧する場合は scp / rsync / USB 等で .env を運んでから ./resume.sh。"
  exit 1
fi

# .env を読込 (= 機械的に source、 ; や ' に注意)
set -a
# shellcheck disable=SC1091
source .env
set +a

if [[ -z "${OPENMONEY_PC_TOKEN:-}" ]]; then
  echo "✗ .env に OPENMONEY_PC_TOKEN が無い。 ./setup.sh で初回セットアップしてください。"
  exit 1
fi

URL="${OPENMONEY_BACKEND_URL:-http://localhost:8000}"
echo "→ ${URL}/api/auth/me に PC token の生存確認..."

TMP="$(mktemp -t openmoney_resume.XXXXXX)"
trap 'rm -f "$TMP"' EXIT

HTTP=$(curl -sS -o "$TMP" -w "%{http_code}" \
  -H "Authorization: Bearer ${OPENMONEY_PC_TOKEN}" \
  "${URL}/api/auth/me" || echo "000")

case "$HTTP" in
  200)
    UID=$(python3 -c "import json,sys; d=json.load(open('$TMP')); print(d.get('uid','?'))" 2>/dev/null || echo "?")
    PLAN=$(python3 -c "import json,sys; d=json.load(open('$TMP')); print(d.get('plan','?'))" 2>/dev/null || echo "?")
    echo "✓ pc_token 有効 (uid=${UID} plan=${PLAN})"
    echo
    echo "そのまま daemon 起動できます:"
    echo "  pkill -9 -f 'src.daemon' 2>/dev/null; .venv/bin/python3 -u -m src.daemon &"
    echo
    echo "Claude Code の設定 (= tools/.claude/settings.local.json) も持ち込み済みなら:"
    echo "  scripts/run_claude.sh  # 既存 ai_token がそのまま使える"
    ;;
  401)
    echo "✗ pc_token が revoke 済 or 無効です。"
    echo "  → 旧 PC で goodbye.sh された / Android 側で削除された 等が原因。"
    echo "  → ./setup.sh で再ペアリングしてください (= 新しい 8 桁コード入力が必要)。"
    exit 1
    ;;
  000|"")
    echo "! backend に接続できませんでした。 OPENMONEY_BACKEND_URL=${URL} を確認してください。"
    exit 1
    ;;
  *)
    echo "! HTTP ${HTTP}: 想定外の応答"
    cat "$TMP"
    exit 1
    ;;
esac
