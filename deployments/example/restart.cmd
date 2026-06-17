@echo off
REM Restart helper the bot can call when it needs to bounce itself
REM (e.g. after editing its own code). Delays a few seconds so the current
REM Telegram reply flushes first.

set CHAT_ID=%1
if "%CHAT_ID%"=="" (
    echo Usage: restart.cmd ^<chat_id^>
    exit /b 1
)

echo %CHAT_ID% > "%~dp0restart_marker"

REM Kill any python process running maxbot for THIS deployment folder.
for /f "tokens=*" %%P in ('powershell -NoProfile -Command "$d=Split-Path -Leaf '%~dp0'.TrimEnd('\'); Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match '-m maxbot' -and $_.CommandLine -match $d } | Select-Object -ExpandProperty ProcessId"') do (
    taskkill /PID %%P /F
)

timeout /t 3 /nobreak >nul
start "" /b "%~dp0start.cmd"
