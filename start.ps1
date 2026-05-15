#Requires -Version 5.1
<#
  OpenMoney 起動スクリプト (Windows / PowerShell 版)
  = 初回 setup と日常起動を 1 本に統合 (start.sh の Windows 移植)

  各ステップは「未完了なら実行・完了済みならスキップ」の idempotent 設計:
    - uv / Node / Claude Code バイナリ : 既存ならスキップ、無ければインストール
    - Python 依存 (.venv)             : uv sync (= 最新ならほぼ即時)
    - ペアリング (8 桁コード)         : tools/.claude/settings.local.json の
                                        ANTHROPIC_AUTH_TOKEN + .env の
                                        OPENMONEY_PC_TOKEN がそろっていればスキップ
    - Firebase 設定                   : firebase-service-account.json と
                                        FCM_PROJECT_ID 両方ありならスキップ
    - daemon                          : /api/ping で既起動ならスキップ
    - 初期パスワード設定 (/login)     : .env に🔒(暗号化マーカー)があればスキップ

  環境変数 (= optional override):
    OPENMONEY_BACKEND_URL  : backend URL
    DAEMON_PORT            : daemon ポート (= 既定 8765)

  使い方 (PowerShell):
    powershell -ExecutionPolicy Bypass -File .\start.ps1
  もしくは start.bat をダブルクリック。
#>

$ErrorActionPreference = 'Stop'

# コンソール / パイプを UTF-8 に (日本語・絵文字マーカー対応)
try {
  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

$DaemonPort      = if ($env:DAEMON_PORT) { $env:DAEMON_PORT } else { '8765' }
$ToolsDir        = Join-Path $RepoRoot 'tools'
$ClaudeConfigDir = Join-Path $ToolsDir '.claude'
$EnvPath         = Join-Path $RepoRoot '.env'

# 埋め込み Claude Code の設定を tools\.claude\ に閉じ込める
# (= 開発用 ~\.claude\ を汚染しない)
$env:CLAUDE_CONFIG_DIR = $ClaudeConfigDir
New-Item -ItemType Directory -Force -Path $ClaudeConfigDir | Out-Null

function Write-Step($m) { Write-Host "→ $m"  -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "✓ $m"  -ForegroundColor Green }
function Write-Fail($m) { Write-Host "✗ $m"  -ForegroundColor Red }
function Write-Rule($m) {
  Write-Host ""
  Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  Write-Host "  $m"
  Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  Write-Host ""
}

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
function Read-Text($p) { if (Test-Path $p) { [System.IO.File]::ReadAllText($p, $Utf8NoBom) } else { '' } }
function Test-Cmd($n)  { [bool](Get-Command $n -ErrorAction SilentlyContinue) }

# .env から KEY の値を読む (無ければ $null)
function Get-EnvValue($key) {
  if (-not (Test-Path $EnvPath)) { return $null }
  foreach ($line in [System.IO.File]::ReadAllLines($EnvPath, $Utf8NoBom)) {
    if ($line -match ('^' + [regex]::Escape($key) + '=(.*)$')) { return $Matches[1] }
  }
  return $null
}

# .env に KEY=VALUE を書き込む (既存の同名キーは上書き、UTF-8 BOM なし)
function Set-EnvValue($key, $val) {
  $lines = @()
  if (Test-Path $EnvPath) {
    $lines = @([System.IO.File]::ReadAllLines($EnvPath, $Utf8NoBom) |
              Where-Object { $_ -notmatch ('^' + [regex]::Escape($key) + '=') })
  }
  $lines += "$key=$val"
  [System.IO.File]::WriteAllLines($EnvPath, [string[]]$lines, $Utf8NoBom)
}

# 一時 .py を作って uv run python で実行 (start.sh の python3 ヒアドキュメント相当)
function Invoke-Py([string]$code, [string[]]$pyArgs) {
  $tmp = Join-Path $env:TEMP ("om_" + [guid]::NewGuid().ToString('N') + ".py")
  [System.IO.File]::WriteAllText($tmp, $code, $Utf8NoBom)
  try { & $UvExe run python $tmp @pyArgs } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
}

# ─────────────────────────────────────────────────────────────
# ── 1. uv の確認 / インストール ──────────────────────────────
# ─────────────────────────────────────────────────────────────
$LocalBin = Join-Path $env:USERPROFILE '.local\bin'
if (-not (Test-Cmd 'uv')) {
  if (Test-Path (Join-Path $LocalBin 'uv.exe')) {
    $env:Path = "$LocalBin;$env:Path"
  } else {
    Write-Step 'uv をインストール...'
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$LocalBin;$env:Path"
  }
}
if (-not (Test-Cmd 'uv')) {
  Write-Fail 'uv のインストールに失敗しました。https://docs.astral.sh/uv/ を参照し、PowerShell を再起動して再実行してください。'
  exit 1
}
$UvExe = (Get-Command uv).Source

# ─────────────────────────────────────────────────────────────
# ── 2. Python 依存関係を同期 (.venv 作成 / 更新) ─────────────
#       Linux の GTK/WebKit2 + .venv-linux は Windows では不要
# ─────────────────────────────────────────────────────────────
Write-Step 'Python 依存関係を同期 (uv sync)...'
& $UvExe sync --frozen

# ─────────────────────────────────────────────────────────────
# ── 3. Node.js (= npm が無ければ winget で導入を試みる) ──────
# ─────────────────────────────────────────────────────────────
if (-not (Test-Cmd 'npm')) {
  if (Test-Cmd 'winget') {
    Write-Step 'Node.js をインストール (winget)...'
    winget install -e --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
    # 同一セッションへ PATH を反映
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [System.Environment]::GetEnvironmentVariable('Path', 'User')
  }
}
if (-not (Test-Cmd 'npm')) {
  Write-Fail 'Node.js が見つかりません。https://nodejs.org/ から LTS を入れ、PowerShell を再起動して再実行してください。'
  exit 1
}

# ─────────────────────────────────────────────────────────────
# ── 4. Claude Code バイナリ ─────────────────────────────────
# ─────────────────────────────────────────────────────────────
$ClaudeBin = $null
if (Test-Cmd 'claude') { $ClaudeBin = (Get-Command claude).Source }
$LocalClaude = Join-Path $ToolsDir 'node_modules\.bin\claude.cmd'
if (-not $ClaudeBin -and (Test-Path $LocalClaude)) { $ClaudeBin = $LocalClaude }
if (-not $ClaudeBin) {
  Write-Step 'Claude Code を .\tools にインストール...'
  & npm install --prefix $ToolsDir '@anthropic-ai/claude-code'
  $ClaudeBin = $LocalClaude
}
if (-not (Test-Path $ClaudeBin)) {
  Write-Fail "Claude Code バイナリが見つかりません: $ClaudeBin"
  exit 1
}

# ── 4b. Claude Code 初回ウィザード回避 (idempotent) ─────────
$ClaudeVer = '2.1.138'
try {
  $vout = & $ClaudeBin --version 2>$null
  if ("$vout" -match '([0-9]+\.[0-9]+\.[0-9]+)') { $ClaudeVer = $Matches[1] }
} catch {}

$ClaudeJsonPy = @'
import json, sys, os, time
path, ver, repo_root = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = {}
if os.path.exists(path):
    try: cfg = json.loads(open(path, encoding="utf-8").read())
    except Exception: cfg = {}
cfg["hasCompletedOnboarding"] = True
cfg["lastOnboardingVersion"] = ver
cfg.setdefault("autoUpdates", False)
cfg.setdefault("firstStartTime", time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()))
cfg.setdefault("theme", "dark")
cfg.setdefault("projects", {})
cfg["projects"].setdefault(repo_root, {})
cfg["projects"][repo_root]["hasTrustDialogAccepted"] = True
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)
'@
Invoke-Py $ClaudeJsonPy @((Join-Path $ClaudeConfigDir '.claude.json'), $ClaudeVer, $RepoRoot)

$SettingsPy = @'
import json, sys, os
path = sys.argv[1]
cfg = {}
if os.path.exists(path):
    try: cfg = json.loads(open(path, encoding="utf-8").read())
    except Exception: cfg = {}
cfg.setdefault("theme", "dark")
cfg["skipDangerousModePermissionPrompt"] = True
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)
'@
Invoke-Py $SettingsPy @((Join-Path $ClaudeConfigDir 'settings.json'))

# ─────────────────────────────────────────────────────────────
# ── 5. 既存 .env から設定値を引き継ぐ ─────────────────────────
# ─────────────────────────────────────────────────────────────
if (-not $env:OPENMONEY_BACKEND_URL) {
  $v = Get-EnvValue 'OPENMONEY_BACKEND_URL'; if ($v) { $env:OPENMONEY_BACKEND_URL = $v }
}
if (-not $env:FCM_PROJECT_ID) {
  $v = Get-EnvValue 'FCM_PROJECT_ID'; if ($v) { $env:FCM_PROJECT_ID = $v }
}
if (-not $env:FCM_SERVICE_ACCOUNT_PATH) {
  $v = Get-EnvValue 'FCM_SERVICE_ACCOUNT_PATH'; if ($v) { $env:FCM_SERVICE_ACCOUNT_PATH = $v }
}
if (-not $env:FCM_PROJECT_ID) { $env:FCM_PROJECT_ID = 'openmoney-app-prod' }

# ─────────────────────────────────────────────────────────────
# ── 6. ペアリング (= 未済ならインタラクティブ実行) ───────────
# ─────────────────────────────────────────────────────────────
function Test-PairingDone {
  $s = Join-Path $ToolsDir '.claude\settings.local.json'
  if (-not (Test-Path $s)) { return $false }
  if (-not (Test-Path $EnvPath)) { return $false }
  try {
    $d = Read-Text $s | ConvertFrom-Json
    if (-not ($d.env -and $d.env.ANTHROPIC_AUTH_TOKEN)) { return $false }
  } catch { return $false }
  return [bool](Get-EnvValue 'OPENMONEY_PC_TOKEN')
}

if (Test-PairingDone) {
  Write-Ok 'ペアリング済み'
} else {
  Write-Rule 'ペアリング (= Android アプリ「OpenMoney Eclipse」 との初回接続)'
  Write-Host '  Android アプリで表示された 8 桁コードを入力してください。'
  $PairCode = Read-Host '  ペアリングコード (= 8 桁)'
  Write-Host ''
  & $UvExe run python -m scripts.openmoney_pair $PairCode
}

# ─────────────────────────────────────────────────────────────
# ── 7. Firebase 設定 (= OTP 中継・レシート受信) ──────────────
# ─────────────────────────────────────────────────────────────
$SaDefault = if ($env:FCM_SERVICE_ACCOUNT_PATH) { $env:FCM_SERVICE_ACCOUNT_PATH }
             else { Join-Path $RepoRoot 'firebase-service-account.json' }

if (-not (Test-Path $SaDefault)) {
  $BackendSa = Join-Path (Split-Path -Parent $RepoRoot) 'openmoney-backend\firebase-service-account.json'
  if (Test-Path $BackendSa) {
    Copy-Item $BackendSa (Join-Path $RepoRoot 'firebase-service-account.json') -Force
    $SaDefault = Join-Path $RepoRoot 'firebase-service-account.json'
    Write-Ok 'firebase-service-account.json を openmoney-backend からコピー'
  }
}

if ((Test-Path $SaDefault) -and $env:FCM_PROJECT_ID) {
  Write-Ok "Firebase 設定済み (FCM_PROJECT_ID=$($env:FCM_PROJECT_ID))"
  Set-EnvValue 'FCM_PROJECT_ID' $env:FCM_PROJECT_ID
} else {
  Write-Rule 'Firebase 設定 (OTP 中継・レシート受信) — Enter でスキップ可'
  $SaInput = Read-Host "  firebase-service-account.json のパス (= 省略時 $SaDefault)"
  $SaPath  = if ($SaInput) { $SaInput } else { $SaDefault }
  if (Test-Path $SaPath) {
    if ($SaPath -ne (Join-Path $RepoRoot 'firebase-service-account.json')) {
      Set-EnvValue 'FCM_SERVICE_ACCOUNT_PATH' $SaPath
    }
    $FcmIdInput = Read-Host '  Firebase プロジェクト ID'
    if ($FcmIdInput) {
      Set-EnvValue 'FCM_PROJECT_ID' $FcmIdInput
      Write-Ok 'Firebase 設定を .env に保存'
    }
  } else {
    Write-Step 'Firebase 設定をスキップ (後で .env に手動追記可)'
  }
}

# ─────────────────────────────────────────────────────────────
# ── 8. daemon 起動 (= 既起動ならスキップ) ────────────────────
# ─────────────────────────────────────────────────────────────
function Test-DaemonUp {
  try {
    Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:$DaemonPort/api/ping" -TimeoutSec 2 | Out-Null
    return $true
  } catch { return $false }
}

if (Test-DaemonUp) {
  Write-Ok "daemon は既に起動中 (port $DaemonPort)"
} else {
  Write-Step 'daemon を起動中...'
  $LogOut = Join-Path $RepoRoot '.daemon.log'
  $LogErr = Join-Path $RepoRoot '.daemon.err.log'
  $proc = Start-Process -FilePath $UvExe `
            -ArgumentList 'run', 'python', '-m', 'src.daemon' `
            -WorkingDirectory $RepoRoot `
            -RedirectStandardOutput $LogOut `
            -RedirectStandardError $LogErr `
            -WindowStyle Hidden -PassThru
  $proc.Id | Out-File -FilePath (Join-Path $RepoRoot '.daemon.pid') -Encoding ascii
  for ($i = 0; $i -lt 30; $i++) {
    if (Test-DaemonUp) { break }
    Start-Sleep -Milliseconds 500
  }
  if (Test-DaemonUp) {
    Write-Ok "daemon 起動完了 (pid $($proc.Id))"
  } else {
    Write-Fail 'daemon の起動に失敗しました。ログ: .daemon.log / .daemon.err.log'
    exit 1
  }
}

# ─────────────────────────────────────────────────────────────
# ── 9. 初期パスワード設定 (= .env に🔒マーカーが無ければ /login へ誘導) ─
# ─────────────────────────────────────────────────────────────
function Test-PasswordSet {
  if (-not (Test-Path $EnvPath)) { return $false }
  $lock = [char]::ConvertFromUtf32(0x1F512)   # 🔒 (暗号化マーカー)
  return (Read-Text $EnvPath).Contains($lock)
}

if (-not (Test-PasswordSet)) {
  $LoginUrl = "http://localhost:$DaemonPort/login"
  Write-Step "初期パスワード設定が必要です。ブラウザを開きます: $LoginUrl"
  Start-Sleep -Milliseconds 300
  try { Start-Process $LoginUrl | Out-Null }
  catch { Write-Host "  ブラウザで $LoginUrl を開いてパスワードを設定してください。" }
  Write-Rule 'ブラウザで初期パスワードを設定したら Enter を押してください。'
  Read-Host '  [Enter で Claude Code を起動]' | Out-Null
}

# ─────────────────────────────────────────────────────────────
# ── 10. settings.local.json の env / model をシェル env へ展開 ─
#       (= _export_anthropic_env.sh の PowerShell 版)
#       Claude Code はモデル API 認証に起動時シェル env しか見ないため
# ─────────────────────────────────────────────────────────────
$SettingsLocal = Join-Path $ToolsDir '.claude\settings.local.json'
if (Test-Path $SettingsLocal) {
  try {
    $d = Read-Text $SettingsLocal | ConvertFrom-Json
    if ($d.env) {
      foreach ($prop in $d.env.PSObject.Properties) {
        if ($prop.Value -is [string]) { Set-Item -Path "Env:$($prop.Name)" -Value $prop.Value }
      }
    }
    if (($d.model -is [string]) -and $d.model) { $env:ANTHROPIC_MODEL = $d.model }
  } catch {}
}

# ─────────────────────────────────────────────────────────────
# ── 11. Claude Code 起動 ─────────────────────────────────────
# ─────────────────────────────────────────────────────────────
Write-Rule "✓ Server (localhost:$DaemonPort) + Claude Code を起動します"
Set-Location $RepoRoot
& $ClaudeBin --dangerously-skip-permissions
exit $LASTEXITCODE
