#!/usr/bin/env python3
"""One-time OAuth login to generate a Google Drive token file for the bot.

Run this **once** on the same machine where the bot runs:

    python tools/generate_drive_token.py

Prerequisites:

1. Create an **OAuth 2.0 Client ID** in Google Cloud Console:
   https://console.cloud.google.com/apis/credentials
   Choose application type **Desktop app**, then download the JSON.
2. Save the file as ``secrets/credentials.json`` in this repo (or pass
   ``--credentials path/to/credentials.json``).
3. Run this script. It will open a browser for you to log in to Google
   and grant Drive access. The resulting token is saved at
   ``secrets/token.pickle`` (override with ``--token``).
4. Add ``GOOGLE_DRIVE_OAUTH_TOKEN_PATH=secrets/token.pickle`` to ``.env``.
5. Restart the bot. It will now download files using your Google
   identity, which means it can access **your private Drive files**
   without sharing them publicly.

The token is automatically refreshed by the bot at runtime; you only
need to re-run this script if you revoke access, change scopes, or
delete the token file.
"""

from __future__ import annotations

import argparse
import os
import pickle
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CREDENTIALS = REPO_ROOT / "secrets" / "credentials.json"
DEFAULT_TOKEN = REPO_ROOT / "secrets" / "token.pickle"

OAUTH_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a Google Drive OAuth token for the bot (one-time login)."
        )
    )
    parser.add_argument(
        "--credentials",
        "-c",
        type=Path,
        default=DEFAULT_CREDENTIALS,
        help=(
            "Path to the OAuth Client ID JSON downloaded from Google Cloud "
            f"Console (default: {DEFAULT_CREDENTIALS.relative_to(REPO_ROOT)})"
        ),
    )
    parser.add_argument(
        "--token",
        "-t",
        type=Path,
        default=DEFAULT_TOKEN,
        help=(
            "Where to write the resulting token "
            f"(default: {DEFAULT_TOKEN.relative_to(REPO_ROOT)})"
        ),
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help=(
            "Do not auto-open the browser; print the URL instead (useful when "
            "running on a headless VPS — copy the URL to your local browser, "
            "log in, then paste the resulting code back here)."
        ),
    )
    parser.add_argument(
        "--scope",
        action="append",
        default=None,
        help=(
            "Override OAuth scopes (repeatable). Default: drive.readonly. "
            "Use 'drive' (full read/write) only if you need it."
        ),
    )
    args = parser.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    except ImportError:
        print(
            "ERROR: google-auth-oauthlib is not installed.\n"
            "Run: pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2

    creds_path: Path = args.credentials
    if not creds_path.is_file():
        print(
            f"ERROR: OAuth credentials file not found at {creds_path}\n"
            "1. Buka https://console.cloud.google.com/apis/credentials\n"
            "2. CREATE CREDENTIALS -> OAuth client ID -> Desktop app\n"
            "3. Download JSON, simpan ke 'secrets/credentials.json'\n"
            "   (atau pakai --credentials <path>)",
            file=sys.stderr,
        )
        return 2

    scopes = args.scope or OAUTH_SCOPES
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), scopes)

    if args.no_browser:
        print(
            "Headless mode. Browser TIDAK akan otomatis dibuka.\n"
            "Salin URL berikut ke browser di komputer Anda, "
            "lalu paste kembali authorization code-nya di sini.\n"
        )
        creds = flow.run_console()  # type: ignore[attr-defined]
    else:
        # ``run_local_server`` spins up a tiny localhost callback server so
        # the user just has to click "Allow" in the browser; the redirect
        # carries the auth code back automatically. Port 0 = pick any free.
        creds = flow.run_local_server(port=0, open_browser=True)

    token_path: Path = args.token
    token_path.parent.mkdir(parents=True, exist_ok=True)
    with token_path.open("wb") as f:
        pickle.dump(creds, f)
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        # Best-effort on Windows; ignore.
        pass

    rel = token_path.relative_to(REPO_ROOT) if token_path.is_relative_to(
        REPO_ROOT
    ) else token_path
    print(
        "\nOK. Token tersimpan ke:",
        rel,
        "\n\nTambah ke .env:",
        f"\n    GOOGLE_DRIVE_OAUTH_TOKEN_PATH={rel.as_posix()}",
        "\n\nLalu restart bot. Engine 'GDriveAPI' sekarang pakai identitas",
        "Google Anda — bot bisa akses semua file private Anda.",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
