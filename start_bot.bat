@echo off
REM ============================================================================
REM ZAEIN Automation Bot - Windows launcher
REM ============================================================================
REM Double-click this file untuk menjalankan bot.
REM
REM Yang dilakukan:
REM   1. cd ke folder script (tidak peduli dari mana di-launch)
REM   2. cek venv dan .env, kasih pesan jelas kalau belum di-setup
REM   3. activate venv, jalankan bot
REM   4. auto-restart (max 5x dalam 1 menit) kalau bot crash
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

set RESTART_COUNT=0
set WINDOW_START=%TIME%

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
