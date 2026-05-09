@echo off
REM ============================================================================
REM ZAEIN Automation Bot - Windows launcher
REM ============================================================================
REM Double-click this file untuk menjalankan bot.
REM
REM Yang dilakukan:
REM   1. cd ke folder script (tidak peduli dari mana di-launch)
REM   2. cek venv dan .env, kasih pesan jelas kalau belum di-setup
REM   3. sync requirements.txt ke venv (idempotent, cepat kalau sudah up-to-date)
REM   4. jalankan bot pakai venv\Scripts\python.exe (tidak butuh `activate`)
REM   5. auto-restart (max 5x) kalau bot crash
REM
REM Tutup jendela ini untuk stop bot.
REM ============================================================================

setlocal ENABLEEXTENSIONS ENABLEDELAYEDEXPANSION

cd /d "%~dp0"

title ZAEIN Automation Bot

echo ============================================================
echo   ZAEIN Automation Bot - Windows launcher
echo ============================================================
echo.

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] venv belum dibuat.
    echo         Jalankan dulu: setup_windows.bat
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [ERROR] File .env tidak ditemukan.
    echo         Salin .env.example ke .env lalu isi token dan API key:
    echo             copy .env.example .env
    echo         Lalu edit .env pakai Notepad.
    echo.
    pause
    exit /b 1
)

if exist "requirements.txt" (
    echo [%DATE% %TIME%] Sync dependency dari requirements.txt...
    "venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo [WARN] pip install gagal. Bot tetap dijalankan dengan dependency yang sudah ada.
        echo        Kalau bot error 'ModuleNotFoundError', jalankan manual:
        echo            venv\Scripts\activate
        echo            pip install -r requirements.txt
    ) else (
        echo [OK] Dependency siap.
    )
)

set RESTART_COUNT=0

:run
echo.
echo [%DATE% %TIME%] Memulai bot...
echo ----------------------------------------------------------
"venv\Scripts\python.exe" bot.py
set EXIT_CODE=%ERRORLEVEL%
echo ----------------------------------------------------------
echo [%DATE% %TIME%] Bot berhenti (exit code: %EXIT_CODE%).

if %EXIT_CODE% EQU 0 (
    echo Bot keluar normal. Tekan tombol apa saja untuk close.
    pause >nul
    exit /b 0
)

set /a RESTART_COUNT+=1
if !RESTART_COUNT! GEQ 5 (
    echo.
    echo [ERROR] Bot crash 5 kali. Cek logs\bot.log untuk detail.
    echo         Tekan tombol apa saja untuk close.
    pause >nul
    exit /b 1
)

echo Restart dalam 5 detik (percobaan ke-!RESTART_COUNT!/5)...
echo Tekan Ctrl+C untuk batalkan.
timeout /t 5 /nobreak >nul
goto run
