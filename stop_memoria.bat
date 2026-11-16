@echo off
title Stop Memoria
cd /d "%~dp0"
echo Stopping Memoria...
rem First stop the watchdog (and Memoria with it), so it doesn't restart Memoria.
if not exist "memory_store\watchdog.pid" goto others
set /p WPID=<"memory_store\watchdog.pid"
taskkill /T /F /PID %WPID% >nul 2>&1
del "memory_store\watchdog.pid" >nul 2>&1
:others
rem Then stop any Memoria that was started some other way.
powershell -NoProfile -Command "Start-Sleep 1; $c = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue; if ($c) { $c | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force } }; Start-Sleep 1; if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) { 'Memoria could not be stopped.' } else { 'Memoria stopped.' }"
pause
