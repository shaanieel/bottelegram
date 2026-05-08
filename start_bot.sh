#!/usr/bin/env bash
# =============================================================================
# ZAEIN Automation Bot - Linux launcher
# =============================================================================
# Run: ./start_bot.sh
#
# Yang dilakukan:
#   1. cd ke folder script (tidak peduli di-launch dari mana)
#   2. cek venv dan .env, kasih pesan jelas kalau belum di-setup
#   3. activate venv, jalankan bot
#   4. auto-restart (max 5x) kalau bot crash
#
# Hentikan dengan Ctrl+C.
# =============================================================================
set -uo pipefail

cd "$(dirname "$0")"

echo "============================================================"
echo "  ZAEIN Automation Bot - Linux launcher"
echo "============================================================"
echo

if [ ! -x "venv/bin/python" ]; then
    echo "[ERROR] venv belum dibuat."
    echo "        Jalankan dulu: bash setup_linux.sh"
    exit 1
fi

if [ ! -f ".env" ]; then
    echo "[ERROR] File .env tidak ditemukan."
    echo "        cp .env.example .env  &&  edit .env"
    exit 1
fi

# Forward Ctrl+C to the bot child process so it can shut down cleanly.
trap 'echo; echo "Diterima sinyal stop, keluar..."; exit 0' INT TERM

restart_count=0
max_restarts=5

while :; do
    echo
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Memulai bot..."
    echo "----------------------------------------------------------"
    set +e
    ./venv/bin/python bot.py
    exit_code=$?
    set -e
    echo "----------------------------------------------------------"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Bot berhenti (exit code: $exit_code)."

    if [ "$exit_code" -eq 0 ]; then
        echo "Bot keluar normal."
        exit 0
    fi

    restart_count=$((restart_count + 1))
    if [ "$restart_count" -ge "$max_restarts" ]; then
        echo
        echo "[ERROR] Bot crash $max_restarts kali. Cek logs/bot.log untuk detail."
        exit 1
    fi

    echo "Restart dalam 5 detik (percobaan ke-$restart_count/$max_restarts)..."
    echo "Ctrl+C untuk batalkan."
    sleep 5
done
