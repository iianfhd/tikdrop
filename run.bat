@echo off
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8:backslashreplace"
cd /d "%~dp0"
title TikDrop
if not defined TIKDROP_HOST set "TIKDROP_HOST=127.0.0.1"
if not defined TIKDROP_PORT set "TIKDROP_PORT=8000"
if not exist ".venv\Scripts\python.exe" (
    echo Jalankan setup.bat terlebih dahulu.
    pause
    exit /b 1
)
echo TikDrop berjalan di http://%TIKDROP_HOST%:%TIKDROP_PORT%
echo Tekan CTRL+C untuk menghentikan server.
.venv\Scripts\python.exe -m uvicorn backend.main:app --host "%TIKDROP_HOST%" --port "%TIKDROP_PORT%"
pause
