@echo off
title ZAEIN Bot - Update, Install Requirements, Run
color 0A

echo ==========================================
echo   ZAEIN BOT - UPDATE + INSTALL + RUN
echo ==========================================
echo.

REM Masuk ke folder tempat file .bat ini berada
cd /d "%~dp0"

echo [1/5] Folder sekarang:
cd
echo.

echo [2/5] Update kode dari GitHub...
git pull
if errorlevel 1 (
    echo.
    echo [PERINGATAN] git pull gagal.
    echo Kalau kamu update manual dari ZIP, abaikan saja.
    echo Tekan tombol apa saja untuk lanjut install requirements...
    pause >nul
)

echo.
echo [3/5] Cek virtual environment...
if not exist "venv\Scripts\activate.bat" (
    echo venv belum ada, membuat venv baru...
    python -m venv venv
    if errorlevel 1 (
        echo.
        echo [ERROR] Gagal membuat venv. Pastikan Python sudah terinstall.
        pause
        exit /b 1
    )
)

echo.
echo [4/5] Aktifkan venv dan install requirements...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Install requirements gagal.
    pause
    exit /b 1
)

echo.
echo [5/5] Jalankan bot...
echo Kalau mau stop bot, tekan CTRL + C
echo.
python bot.py

echo.
echo Bot berhenti.
pause
