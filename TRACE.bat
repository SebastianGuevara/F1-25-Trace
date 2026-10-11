@echo off
rem Double-click to start TRACE. Close this window to stop it.
title TRACE
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m f1trace
) else (
    python -m f1trace
)
echo.
pause
