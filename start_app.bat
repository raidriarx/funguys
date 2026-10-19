@echo off
title FunGuys Mission Control
cd /d "%~dp0"
echo ===================================================
echo   Starting FunGuys Host & Mission Control...
echo ===================================================
echo.
set FUNGUYS_TRUSTED_EXECUTION=1
timeout /t 2 /nobreak >nul
start "" http://localhost:8420
uv run funguys host --host 0.0.0.0 --tunnel
pause
