@echo off
title Set Memoria dashboard password
cd /d "%~dp0"
".venv\Scripts\python.exe" set_dashboard_password.py
echo.
pause
