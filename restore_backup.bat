@echo off
title Restore a Memoria backup
cd /d "%~dp0"
".venv\Scripts\python.exe" restore_backup.py
pause
