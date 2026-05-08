#!/usr/bin/env bash
# =============================================================================
# ZAEIN Automation Bot - Linux setup script
# =============================================================================
# Creates a virtualenv, installs dependencies, and prepares runtime folders.
# Run from the project root:
#
#     bash setup_linux.sh
#
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

echo "[1/6] Cek versi Python…"
"$PYTHON" --version

echo "[2/6] Membuat virtualenv (./venv) jika belum ada…"
if [ ! -d "venv" ]; then
    "$PYTHON" -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate

echo "[3/6] Upgrade pip & install dependencies…"
pip install --upgrade pip wheel
pip install -r requirements.txt

echo "[4/6] Membuat folder runtime…"
mkdir -p downloads temp logs data
touch downloads/.gitkeep temp/.gitkeep logs/.gitkeep data/.gitkeep

echo "[5/6] Menyiapkan .env…"
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "      .env dibuat dari .env.example. Edit file tersebut sebelum menjalankan bot."
fi

echo "[6/6] Cek FFmpeg (opsional)…"
if command -v ffmpeg >/dev/null 2>&1; then
    ffmpeg -version | head -n 1
else
    echo "      FFmpeg tidak ditemukan. Install dengan:"
    echo "        sudo apt-get install -y ffmpeg     # Debian/Ubuntu"
    echo "        sudo dnf install -y ffmpeg         # Fedora/RHEL"
fi

echo
echo "Selesai. Edit .env, lalu jalankan:"
echo "    source venv/bin/activate"
echo "    python bot.py"
