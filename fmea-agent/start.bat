@echo off
title FMEA AgentOS
echo ============================================================
echo   FMEA AgentOS Server Launcher
echo ============================================================
echo   Config: .env
echo   Port: 7777
echo ============================================================

REM Kill old process on port 7777
echo   [1/3] Releasing port 7777...
powershell -NoProfile -Command "$conns=Get-NetTCPConnection -LocalPort 7777 -ErrorAction SilentlyContinue|Where-Object {$_.OwningProcess -gt 0 -and $_.State -eq 'Listen'}|Select -First 1; if($conns){Stop-Process -Id $conns.OwningProcess -Force -ErrorAction SilentlyContinue; Write-Host '    Killed PID:'$conns.OwningProcess} else{Write-Host '    Port is free'}"

REM Start server in a new window
echo   [2/3] Starting server...
start "FMEA AgentOS Server" D:\agent-fmea\agno\.venv\Scripts\python.exe main.py

REM Wait for server to be ready, then open Chrome
echo   [3/3] Waiting for server...
powershell -NoProfile -Command "$u='http://localhost:7777/health'; for($i=1;$i -le 15;$i++){try{$r=Invoke-WebRequest -Uri $u -TimeoutSec 2 -UseBasicParsing; if($r.StatusCode -eq 200){Write-Host '    Server ready!'; Start-Process 'chrome' ('http://localhost:7777/?_t='+(Get-Date -Format 'HHmmss')); exit 0}}catch{Write-Host ('    Waiting... ('+$i+'/15)')}; Start-Sleep 2}; Write-Host '    Timeout - please visit http://localhost:7777/ manually'"

echo.
echo   Press any key to open management page
pause >nul
start http://localhost:7777/
