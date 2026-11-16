@echo off
title Start Memoria
cd /d "%~dp0"
echo Starting Memoria in the background...
wscript.exe "%~dp0start_memoria_hidden.vbs"
powershell -NoProfile -Command "for ($i=0; $i -lt 45; $i++) { if (Test-NetConnection 127.0.0.1 -Port 8765 -InformationLevel Quiet -WarningAction SilentlyContinue) { 'Memoria is running. You can close this window.'; exit } ; Start-Sleep 2 }; 'Memoria did not start. Look in memory_store\server.log for the reason.'"
pause
