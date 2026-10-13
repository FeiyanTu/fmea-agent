@echo off
rem ============================================================
rem  FMEA-Agent Local One-Click Deploy (CMD version)
rem  Run in cmd.exe directly:  deploy-fast.bat
rem  Pack code -> scp upload -> server auto update -> verify
rem  Requires: Windows built-in tar / ssh / scp (Win10 1809+)
rem ============================================================
chcp 65001 >nul
setlocal

rem ---------- EDIT THESE ----------
set SERVER_USER=appuser
set SERVER_HOST=10.180.17.11
set SERVER_DIR=/data/demo/agent-fmea
rem --------------------------------

cd /d D:\agent-fmea

echo [1/4] Packing code (excluding .venv/data/knowledge/.env/logs)...
if exist fmea-agent-code.tar.gz del /q fmea-agent-code.tar.gz
tar -czf fmea-agent-code.tar.gz ^
  --exclude=fmea-agent/.venv ^
  --exclude=fmea-agent/__pycache__ ^
  --exclude=fmea-agent/logs ^
  --exclude=fmea-agent/tmp ^
  --exclude=fmea-agent/data ^
  --exclude=fmea-agent/knowledge/*/* ^
  --exclude=fmea-agent/.env ^
  --exclude=fmea-agent/.envbat ^
  fmea-agent
if errorlevel 1 goto FAIL

echo [2/4] Uploading to %SERVER_USER%@%SERVER_HOST% ...
scp fmea-agent-code.tar.gz update-server.sh %SERVER_USER%@%SERVER_HOST%:%SERVER_DIR%/
if errorlevel 1 goto FAIL

echo [3/4] Updating on server (stop-backup-overwrite-start-check)...
ssh %SERVER_USER%@%SERVER_HOST% "cd %SERVER_DIR% && bash update-server.sh"
if errorlevel 1 goto FAIL

echo [4/4] Done! Browser: http://%SERVER_HOST%:7777/  (Ctrl+Shift+R to refresh)
pause
exit /b 0

:FAIL
echo.
echo *** DEPLOY FAILED - check the error above ***
pause
exit /b 1
