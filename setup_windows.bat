@echo off
REM ============================================================================
REM ZAEIN Automation Bot - Windows setup script
REM ============================================================================
REM Creates a virtualenv, installs dependencies, and prepares runtime folders.
REM
REM Usage:
REM     setup_windows.bat
REM ============================================================================

setlocal ENABLEEXTENSIONS

cd /d "%~dp0"

echo [1/6] Cek versi Python...
python --version
if errorlevel 1 (
    echo Python tidak ditemukan. Install dari https://www.python.org/downloads/ ^(centang "Add Python to PATH"^)
    exit /b 1
)

echo [2/6] Membuat virtualenv .\venv jika belum ada...
if not exist "venv" (
    python -m venv venv
)

call "venv\Scripts\activate.bat"

echo [3/6] Upgrade pip dan install dependencies...
python -m pip install --upgrade pip wheel
if errorlevel 1 exit /b 1
python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

echo [4/6] Membuat folder runtime...
if not exist "downloads" mkdir "downloads"
if not exist "temp" mkdir "temp"
if not exist "logs" mkdir "logs"
if not exist "data" mkdir "data"
type nul > "downloads\.gitkeep" 2>nul
type nul > "temp\.gitkeep" 2>nul
type nul > "logs\.gitkeep" 2>nul
type nul > "data\.gitkeep" 2>nul

echo [5/6] Menyiapkan .env...
if not exist ".env" (
    copy /Y ".env.example" ".env" >nul
    echo       .env dibuat dari .env.example. Edit file tersebut sebelum menjalankan bot.
)

echo [6/6] Cek FFmpeg ^(opsional^)...
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo       FFmpeg tidak ditemukan.
    echo       Download dari https://www.gyan.dev/ffmpeg/builds/ dan tambahkan ke PATH.
) else (
    ffmpeg -version | findstr /B "ffmpeg"
)

echo.
echo Selesai. Edit .env lalu jalankan:
echo     venv\Scripts\activate.bat
echo     python bot.py

endlocal
