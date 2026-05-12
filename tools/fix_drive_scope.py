#!/usr/bin/env python3
"""Fix Google Drive OAuth scope for upload.

Masalah:
- Error upload:
  Request had insufficient authentication scopes
- Penyebab:
  token lama dibuat dengan scope drive.readonly.
- Solusi:
  ubah scope jadi https://www.googleapis.com/auth/drive
  lalu generate token.pickle ulang.

Jalankan dari root project:
    python tools/fix_drive_scope.py

Setelah itu:
    python tools/generate_drive_token.py

Lalu restart bot.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FILES = [
    (
        ROOT / "modules" / "gdrive_api.py",
        'OAUTH_SCOPE = "https://www.googleapis.com/auth/drive.readonly"',
        'OAUTH_SCOPE = "https://www.googleapis.com/auth/drive"',
    ),
    (
        ROOT / "tools" / "generate_drive_token.py",
        'OAUTH_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]',
        'OAUTH_SCOPES = ["https://www.googleapis.com/auth/drive"]',
    ),
]


def patch_file(path: Path, old: str, new: str) -> bool:
    if not path.exists():
        print(f"[SKIP] Tidak ditemukan: {path}")
        return False

    text = path.read_text(encoding="utf-8")

    if new in text:
        print(f"[OK] Sudah benar: {path.relative_to(ROOT)}")
        return False

    if old not in text:
        print(f"[WARN] Pola lama tidak ditemukan di: {path.relative_to(ROOT)}")
        return False

    backup = path.with_suffix(path.suffix + f".bak-{int(time.time())}")
    shutil.copy2(path, backup)

    text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")

    print(f"[PATCHED] {path.relative_to(ROOT)}")
    print(f"         Backup: {backup.relative_to(ROOT)}")
    return True


def move_old_token() -> None:
    token = ROOT / "secrets" / "token.pickle"
    if not token.exists():
        print("[OK] token.pickle lama tidak ada.")
        return

    old = ROOT / "secrets" / f"token.pickle.old-{int(time.time())}"
    token.rename(old)
    print(f"[MOVED] token lama dipindah ke: {old.relative_to(ROOT)}")


def main() -> int:
    changed = False
    for path, old, new in FILES:
        changed = patch_file(path, old, new) or changed

    move_old_token()

    print()
    print("Selesai.")
    print("Langkah berikutnya:")
    print("1. Jalankan: python tools/generate_drive_token.py")
    print("2. Login Google dan izinkan akses Drive.")
    print("3. Restart bot.")
    print("4. Cek di Telegram: /health")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
