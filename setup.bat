@echo off
setlocal
cd /d "%~dp0"
title TikDrop - Setup

echo ======================================
echo       TikDrop - Install Dependencies
echo ======================================

where py >nul 2>nul
if %errorlevel%==0 (
    if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv
) else (
    if not exist ".venv\Scripts\python.exe" python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
    echo GAGAL: Python tidak ditemukan. Install Python 3.10 atau lebih baru.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
if errorlevel 1 goto failed
python -m pip install -r requirements.txt
if errorlevel 1 goto failed

if not exist downloads mkdir downloads
if not exist .tmp mkdir .tmp
if not exist .env copy .env.example .env >nul

echo.
echo Setup berhasil. Selanjutnya jalankan run.bat.
echo Catatan: FFmpeg dibutuhkan untuk membuat MP3 dan menggabungkan/remux video.
pause
exit /b 0

:failed
echo.
echo Instalasi gagal. Periksa koneksi internet dan versi Python.
pause
exit /b 1
