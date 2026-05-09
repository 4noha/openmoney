#!/usr/bin/env bash
#
# OpenMoney goodbye.sh
#
# 用途: 旧 PC を廃棄 / 機種変更で退役させる前に呼ぶ self-revoke。
#       backend で pc_token + 紐付き ai_token を revoke + ローカル sensitive
#       ファイルの削除確認 (= y/N で個別)。
#
# 実行内容:
#   1. daemon 停止 (= pkill src.daemon)
#   2. DELETE /api/pair/self で backend から登録解除 + 紐付き ai_token 連動 revoke
#   3. .env / tools/.claude/settings.local.json / transactions.db / .pw_data の削除確認
#
# 注意: transactions.db / receipts/ / config/*.local.toml を新 PC に移行したい場合
#      は **このスクリプトを実行する前に** scp / rsync / USB で転送しておくこと。
#

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

if [[ ! -f .env ]]; then
  echo "✗ .env がありません。 既に解除済 or 別ディレクトリで実行している?"
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

if [[ -z "${OPENMONEY_PC_TOKEN:-}" ]]; then
  echo "✗ .env に OPENMONEY_PC_TOKEN が無い。"
  exit 1
fi

URL="${OPENMONEY_BACKEND_URL:-http://localhost:8000}"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  OpenMoney 退役処理 (= 旧 PC リプレイス用 goodbye)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "実行内容:"
echo "  1. daemon 停止"
echo "  2. backend で pc_token + ai_token を revoke (= DELETE /api/pair/self)"
echo "  3. ローカル sensitive ファイルの削除確認 (= 個別 y/N)"
echo
echo "⚠️  transactions.db / receipts/ / config/*.local.toml を新 PC に持っていく場合"
echo "   は既に転送済か確認してください (= goodbye 後に転送するのは可能ですが、"
echo "   pc_token は revoke されるので Claude Code は使えなくなります)。"
echo
read -r -p "続けますか? [y/N] " ans
[[ "${ans:-}" != "y" && "${ans:-}" != "Y" ]] && { echo "中止。"; exit 1; }

# ── 1. daemon 停止 ──────────────────────────────────────
echo
echo "→ 1. daemon を停止"
pkill -9 -f 'src.daemon' 2>/dev/null || true
sleep 0.3
echo "  done"

# ── 2. backend で revoke ────────────────────────────────
echo
echo "→ 2. backend で revoke"

TMP="$(mktemp -t openmoney_goodbye.XXXXXX)"
trap 'rm -f "$TMP"' EXIT

HTTP=$(curl -sS -o "$TMP" -w "%{http_code}" -X DELETE \
  -H "Authorization: Bearer ${OPENMONEY_PC_TOKEN}" \
  "${URL}/api/pair/self" || echo "000")

if [[ "$HTTP" == "200" ]]; then
  echo "  ✓ revoke 成功:"
  python3 -m json.tool < "$TMP" 2>/dev/null | sed 's/^/    /' || sed 's/^/    /' "$TMP"
elif [[ "$HTTP" == "401" ]]; then
  echo "  ! pc_token は既に無効でした (= 既に revoke 済 or backend 側で削除済)"
elif [[ "$HTTP" == "000" || "$HTTP" == "" ]]; then
  echo "  ! backend に接続できず。 オフライン廃棄なら Android アプリ /"
  echo "    管理画面から後ほど削除してください。"
else
  echo "  ! HTTP ${HTTP}: 想定外の応答"
  cat "$TMP"
  echo
  echo "  Android アプリ / 管理画面から手動削除してください。"
fi

# ── 3. ローカルファイル削除 ───────────────────────────
echo
echo "→ 3. ローカル sensitive ファイル の削除"

CANDIDATES=(
  ".env"
  "tools/.claude/settings.local.json"  # 埋め込み Claude の ai_token (= revoke 済)
  "transactions.db"
  "transactions.db-shm"
  "transactions.db-wal"
  ".pw_data"
)

for path in "${CANDIDATES[@]}"; do
  if [[ -e "$path" ]]; then
    read -r -p "    $path を削除しますか? [y/N] " ans
    if [[ "${ans:-}" == "y" || "${ans:-}" == "Y" ]]; then
      rm -rf "$path"
      echo "      ✓ removed: $path"
    fi
  fi
done

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✓ goodbye 完了。"
echo
echo "  新 PC で:"
echo "    git clone <openmoney repo> && cd openmoney"
echo "    ./setup.sh   (= 新 8 桁コードを Android アプリで発行 → 入力)"
echo "  → 新しい pc_token + ai_token が降り、 Claude Code が起動します。"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
