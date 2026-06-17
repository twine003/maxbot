@echo off
REM Launch maxbot for this deployment. Run from any cmd shell or pin as a
REM Windows Scheduled Task ("Run whether user is logged on or not").

cd /d "%~dp0"

REM Prefer the repo-root .venv created by install.py; fall back to system python.
if exist "..\..\.venv\Scripts\python.exe" (
    set "PY=..\..\.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

"%PY%" -m maxbot --config "%~dp0bot.toml"
