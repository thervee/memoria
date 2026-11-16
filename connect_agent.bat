@echo off
title Connect an agent to Memoria
cd /d "%~dp0"
set /p AGENT=Type the agent's ID (lowercase letters, numbers, dashes) and press Enter:
".venv\Scripts\python.exe" connect_agent.py %AGENT%
pause
