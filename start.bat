@echo off
rem OpenMoney 起動ランチャ (ダブルクリック用)
rem PowerShell の実行ポリシーを回避して start.ps1 を実行する。
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set ERR=%ERRORLEVEL%
if not "%ERR%"=="0" (
  echo.
  echo [start.ps1 が終了コード %ERR% で終了しました]
  pause
)
endlocal
