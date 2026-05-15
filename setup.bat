@echo off
rem OpenMoney 初回セットアップランチャ (ダブルクリック用) — 実体は start.bat と同じ。
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set ERR=%ERRORLEVEL%
if not "%ERR%"=="0" (
  echo.
  echo [setup.ps1 が終了コード %ERR% で終了しました]
  pause
)
endlocal
