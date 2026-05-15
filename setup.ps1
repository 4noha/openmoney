#Requires -Version 5.1
#
# OpenMoney — 初回セットアップ用エイリアス (Windows / PowerShell 版)
#
# setup と日常起動は start.ps1 に統合済 (= 各ステップが idempotent)。
# 既存の README / ユーザの muscle memory 互換のためこのファイルを残しているだけで、
# 実体は start.ps1 と同じ。
#
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $here 'start.ps1') @args
exit $LASTEXITCODE
