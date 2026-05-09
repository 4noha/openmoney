#!/usr/bin/env bash
#
# OpenMoney — 初回セットアップ用エイリアス。
#
# setup と日常起動は start.sh に統合済 (= 各ステップが idempotent)。
# 既存の README / ユーザの muscle memory 互換のためこのファイルを残しているだけで、
# 実体は start.sh と同じ。
#
set -euo pipefail
exec "$(dirname "${BASH_SOURCE[0]}")/start.sh" "$@"
