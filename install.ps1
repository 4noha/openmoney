#Requires -Version 5.1
<#
  OpenMoney Windows インストーラ (PowerShell・install.sh の Windows 版)

  使い方 — 新しい PC の PowerShell に以下を貼るだけ:

    irm https://raw.githubusercontent.com/4noha/openmoney/main/install.ps1 | iex

  SSH で clone したい場合 (事前に GitHub の SSH 鍵を設定):

    $env:REPO_URL='git@github.com:4noha/openmoney.git'; irm https://raw.githubusercontent.com/4noha/openmoney/main/install.ps1 | iex

  実行内容:
    1. winget の確認 (Windows 10/11 標準: アプリ インストーラー)
    2. Git (Git.Git) を winget で導入 — git 本体 + Git Bash (Claude Code が利用)
    3. PowerShell 7 (Microsoft.PowerShell) を winget で導入 (Claude Code 用)
    4. リポジトリを clone (既存なら git pull --ff-only)
    5. start.ps1 を実行 (uv / Node.js / Claude Code / daemon / ペアリング)

  WSL / Cygwin などの Linux サブシステムは一切使いません (純 PowerShell + winget)。
#>

$ErrorActionPreference = 'Stop'
try {
  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

$RepoUrl    = if ($env:REPO_URL)    { $env:REPO_URL }    else { 'https://github.com/4noha/openmoney.git' }
$InstallDir = if ($env:INSTALL_DIR) { $env:INSTALL_DIR } else { Join-Path $HOME 'openmoney' }

function Write-Rule($m) {
  Write-Host ""
  Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  Write-Host "  $m"
  Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  Write-Host ""
}
function Test-Cmd($n) { [bool](Get-Command $n -ErrorAction SilentlyContinue) }
function Update-Path {
  # winget 直後は同一セッションへ PATH 未反映 → Machine+User を再読込
  $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
              [System.Environment]::GetEnvironmentVariable('Path', 'User')
  $gitCmd = Join-Path $env:ProgramFiles 'Git\cmd'
  if ((Test-Path $gitCmd) -and ($env:Path -notlike "*$gitCmd*")) { $env:Path = "$gitCmd;$env:Path" }
}

Write-Rule "OpenMoney インストーラ (Windows)"
Write-Host "  インストール先: $InstallDir"
Write-Host "  リポジトリ    : $RepoUrl"
Write-Host ""
$ans = Read-Host "続けますか? [Y/n]"
if ($ans -match '^[Nn]') { Write-Host "中止。"; return }

# ────────────────────────────────────────────────
# 1. winget
# ────────────────────────────────────────────────
if (-not (Test-Cmd 'winget')) {
  Write-Host "✗ winget が見つかりません。Microsoft Store から「アプリ インストーラー」を入れて再実行してください。" -ForegroundColor Red
  return
}

# ────────────────────────────────────────────────
# 2. Git (= git 本体 + Git Bash。Claude Code の Bash ツールが Git Bash を使う)
# ────────────────────────────────────────────────
if (-not (Test-Cmd 'git')) {
  Write-Host "→ Git を winget で導入..." -ForegroundColor Cyan
  winget install -e --id Git.Git --source winget --accept-source-agreements --accept-package-agreements
  Update-Path
}
if (-not (Test-Cmd 'git')) {
  Write-Host "✗ git がまだ PATH にありません。PowerShell を開き直して再実行してください。" -ForegroundColor Red
  return
}

# ────────────────────────────────────────────────
# 3. PowerShell 7 (= Claude Code 用)
# ────────────────────────────────────────────────
if (-not (Test-Cmd 'pwsh')) {
  Write-Host "→ PowerShell 7 を winget で導入..." -ForegroundColor Cyan
  winget install -e --id Microsoft.PowerShell --source winget --accept-source-agreements --accept-package-agreements
  Update-Path
}

# ────────────────────────────────────────────────
# 4. clone / pull
# ────────────────────────────────────────────────
Write-Host ""
if (Test-Path (Join-Path $InstallDir '.git')) {
  Write-Host "→ 既存リポジトリを更新 (git pull --ff-only)..." -ForegroundColor Cyan
  git -C $InstallDir pull --ff-only
  Write-Host "✓ 更新完了" -ForegroundColor Green
} else {
  Write-Host "→ リポジトリを clone..." -ForegroundColor Cyan
  git clone $RepoUrl $InstallDir
  Write-Host "✓ clone 完了" -ForegroundColor Green
}

# ────────────────────────────────────────────────
# 5. start.ps1 (= pwsh があれば pwsh、無ければ Windows PowerShell)
# ────────────────────────────────────────────────
Write-Host ""
Write-Host "→ start.ps1 を実行..." -ForegroundColor Cyan
Write-Host ""
Set-Location $InstallDir
$shell = if (Test-Cmd 'pwsh') { 'pwsh' } else { 'powershell' }
& $shell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $InstallDir 'start.ps1')
