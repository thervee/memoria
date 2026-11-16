@echo off
title Register a Memoria agent
cd /d "%~dp0"
set /p AGENT=Type the new agent's ID (lowercase letters, numbers, dashes) and press Enter:
".venv\Scripts\python.exe" register_agent.py %AGENT%
pause
