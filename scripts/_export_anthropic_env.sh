#!/usr/bin/env bash
# tools/.claude/settings.local.json の env / model を読み取り、
# 呼び出し元シェルに export するヘルパー。
#
# Claude Code は settings.json の `env` ブロックを子プロセス (Bash / Hooks) 用にしか
# 適用せず、モデル API 認証 (ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL) には
# 起動時のシェル環境変数しか見ない。そのためペアリングで書いた token を
# claude 起動前にここで export しておく必要がある。
#
# 使い方 (source して使う、exec しない):
#   REPO_ROOT="..."; source "$REPO_ROOT/scripts/_export_anthropic_env.sh"

# REPO_ROOT は呼び出し側で定義されている前提
_SETTINGS_PATH="${REPO_ROOT}/tools/.claude/settings.local.json"

if [ -f "$_SETTINGS_PATH" ]; then
  _OPENMONEY_ENV_EXPORTS=$(python3 - "$_SETTINGS_PATH" <<'PYEOF'
import json, shlex, sys
try:
    d = json.loads(open(sys.argv[1]).read())
except Exception:
    sys.exit(0)
for k, v in (d.get("env") or {}).items():
    if not isinstance(v, str):
        continue
    print(f"export {k}={shlex.quote(v)}")
m = d.get("model")
if isinstance(m, str) and m:
    print(f"export ANTHROPIC_MODEL={shlex.quote(m)}")
PYEOF
)
  if [ -n "${_OPENMONEY_ENV_EXPORTS:-}" ]; then
    eval "$_OPENMONEY_ENV_EXPORTS"
  fi
  unset _OPENMONEY_ENV_EXPORTS
fi

unset _SETTINGS_PATH
