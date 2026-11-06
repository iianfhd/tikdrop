@echo off
setlocal
cd /d "%~dp0"
title TikDrop - LAN
if not defined TIKDROP_HOST set "TIKDROP_HOST=0.0.0.0"
if not defined TIKDROP_PORT set "TIKDROP_PORT=8000"
if not exist ".venv\Scripts\python.exe" (
    echo Jalankan setup.bat terlebih dahulu.
    pause
    exit /b 1
)
echo PERHATIAN: Server akan dapat diakses perangkat lain di jaringan lokal.
echo Gunakan hanya pada Wi-Fi pribadi yang kamu percaya. Jangan port-forward ke internet.
echo Buka http://IP-LAPTOP:%TIKDROP_PORT% dari HP yang berada di Wi-Fi yang sama.
echo.
.venv\Scripts\python.exe -m uvicorn backend.main:app --host "%TIKDROP_HOST%" --port "%TIKDROP_PORT%"
pause
