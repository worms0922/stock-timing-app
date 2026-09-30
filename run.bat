@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   [Naver Finance Realtime] Triple Timing System
echo ============================================================
echo.

where python >nul 2>&1
if %ERRORLEVEL% equ 0 (
    python app.py
    goto end
)

where py >nul 2>&1
if %ERRORLEVEL% equ 0 (
    py app.py
    goto end
)

echo [ERROR] Python is not installed. Please install Python from https://www.python.org
echo.

:end
pause
