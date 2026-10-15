@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
set "LUMABOX_WEB_PASSWORD="
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -m lumabox.launch %*
) else (
    python -m lumabox.launch %*
)
if errorlevel 1 (
    echo.
    echo LumaBox could not start. Check Python, web dependencies and WebData\server.log.
    pause
    exit /b 1
)
exit /b 0
