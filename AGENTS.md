# AGENTS.md — bottelegram

Panduan untuk AI coding agent (Devin, Cursor, Claude, dst.) yang akan bekerja
di repo ini. **Wajib** dibaca sebelum mulai melakukan perubahan supaya tidak
mengulangi pitfall yang sudah pernah terjadi.

---

## TL;DR

- Bot Telegram untuk **download video → upload** ke Bunny Stream / Player4Me /
  Google Drive (mirror dari Telegram Index).
- Bahasa Python 3.10+, framework `python-telegram-bot[ext]>=21.6`, queue async
  in-process dengan persistensi JSON.
- Entrypoint: `bot.py` → `BotApp(cfg, application)` di
  `modules/telegram_handlers.py`.
- **Semua command harus terdaftar di** `BotApp._register_handlers` **dan**
  `BotApp._publish_bot_commands`. Jangan pernah tinggalkan handler di file
  terpisah dengan instruksi "paste manual ke BotApp" — itu pernah kejadian
  dan command tidak muncul di Telegram (lihat _Pitfall #1_ di bawah).
- Setiap perubahan **harus** dibuat sebagai PR ke `main` lewat branch baru,
  dengan deskripsi jelas dan test/verifikasi singkat.

---

## Struktur Repo

```
bottelegram/
├── bot.py                          # Entrypoint
├── config.yaml                     # Config publik (non-secret)
├── .env.example                    # Template secret env vars
├── requirements.txt
├── start_bot.sh / start_bot.bat    # Launcher (auto sync deps)
├── setup_linux.sh / setup_windows.bat
├── data/                           # jobs.json (queue persistence)
├── downloads/                      # File hasil download (auto-cleanup)
├── temp/                           # Working dir (.part files, sub extraction)
├── logs/                           # Rotating logs
└── modules/
    ├── config_manager.py           # Load .env + config.yaml → AppConfig
    ├── logger.py                   # setup_logging + register_secrets
    ├── validators.py               # is_url, classify_link, parse_command_args
    ├── storage_manager.py          # human_bytes, safe_unlink, dir helpers
    ├── downloader.py               # gdown / yt-dlp / direct downloader
    ├── gdrive_api.py               # Drive API client (download metadata)
    ├── gdrive_uploader.py          # Drive resumable upload (untuk /mirror_gdrive)
    ├── tgindex_downloader.py       # Scrape zindex.aioarea.us.kg HTML + auto-login (TGIndexClient)
    ├── bunny_uploader.py           # Bunny Stream uploader
    ├── player4me_uploader.py       # Player4Me TUS + URL ingest
    ├── subtitle_extractor.py       # ffprobe + ffmpeg sub extract
    ├── queue_manager.py            # Async job queue + JSON persistence
    ├── telegram_handlers.py        # BotApp — semua command + dispatcher
    └── telegram_handlers_gdrive.py # Helper /gdrive_list /mirror_gdrive
```

### Konvensi penambahan modul/handler baru

1. **Logika berat** (scraping, IO, integrasi 3rd party) → modul terpisah di
   `modules/`. Test-able tanpa Telegram.
2. **Command Telegram baru** → method di kelas `BotApp` (atau wrapper modul
   yang di-register lewat helper, contoh: `register_gdrive_handlers`). Wajib:
   - Daftar di `BotApp._register_handlers`.
   - Daftar di `BotApp._publish_bot_commands` (supaya muncul di autocomplete
     menu Telegram).
   - Update teks `cmd_help` di `telegram_handlers.py`.
   - Pakai decorator `@admin_only` (atau `_is_admin` di helper modul).
3. **Tipe job baru** → tambahkan ke enum `JobType` di
   `modules/queue_manager.py` **dan** dispatch case di `BotApp._run_job`
   **dan** branch di `BotApp._send_completion`. Kalau lupa salah satu, job
   akan stuck atau notifikasi completion-nya generic "Download Selesai".
4. **Data baru di Job** (mis. `gdrive_file_id`) → field di dataclass `Job`
   di `queue_manager.py` (auto persist ke `jobs.json`).
5. **Config field baru** → dataclass section di `config_manager.py` +
   parsing di `load_config()` + entry default di `config.yaml`. Tambahkan
   juga di `safe_view()` kalau perlu muncul di `/config`.

---

## Command Reference (Telegram)

| Command | Tipe Job | Catatan |
| ------- | -------- | ------- |
| `/start`, `/help` | — | Info dan daftar command |
| `/m URL` | UPLOAD_BUNNY / UPLOAD_PLAYER4ME / UPLOAD_PLAYER4ME_SUBS / UPLOAD_PLAYER4ME_FOLDER / DOWNLOAD_ONLY | Picker tombol generik (Bunny / Player4Me / Player4Me + sub embed / Player4Me + sub folder / download saja) |
| `/upload_bunny [Judul \| ] URL` | UPLOAD_BUNNY | Direct |
| `/upload_player4me [Judul \| ] URL` | UPLOAD_PLAYER4ME / UPLOAD_PLAYER4ME_SUBS / UPLOAD_PLAYER4ME_FOLDER | **Picker** — selalu tampilkan tombol pilih mode |
| `/upload_player4me_subs [Judul \| ] URL` | UPLOAD_PLAYER4ME_SUBS | Skip picker, langsung mode "auto-extract sub embed (mkv/mp4)" |
| `/upload_player4me_folder [Judul \| ] URL_FOLDER_DRIVE` | UPLOAD_PLAYER4ME_FOLDER | Skip picker, mode "folder Drive + sub file sidecar" |
| `/download URL` | DOWNLOAD_ONLY | Download saja, tanpa upload |
| `/gdrive_list URL_INDEX [\| keyword]` | — | Preview (read-only) file di Telegram Index |
| `/mirror_gdrive URL_INDEX [\| keyword] [\| folder_id]` | MIRROR_GDRIVE | Bulk download dari index → upload ke Drive |
| `/queue` `/cancel` `/retry` `/history` `/status` | — | Manajemen antrean |
| `/list_files` `/delete_file` `/clear_downloads` `/storage` | — | Manajemen storage lokal |
| `/health` `/config` `/backup_config` | — | Diagnostik |

### 2 mode subtitle Player4Me (penting — sering ditanyakan user)

User selalu memilih salah satu dari 2 mode subtitle dengan picker
`/upload_player4me URL` (atau lewat tombol `/m URL`):

1. **Mode A — sub embed (`UPLOAD_PLAYER4ME_SUBS`)** — input adalah URL **file
   tunggal** (.mkv / .mp4 dengan subtitle embed). Bot:
   - Download file dari source (Drive, direct link, dst).
   - `ffprobe` untuk enumerate stream subtitle.
   - `ffmpeg` extract setiap subtitle text-based (subrip / ass / vtt /
     mov_text) ke `temp/subs-<job_id>/`. Bitmap codec (PGS / VobSub / DVB)
     di-skip dengan warning.
   - TUS upload video ke Player4Me, recover `videoId` lewat snapshot+diff
     `/video/manage`.
   - PUT setiap file `.srt` / `.ass` ke `/video/manage/{id}/subtitle`.
   - Bahasa subtitle: prefer ffprobe metadata `language` (ISO 639-2 →
     dipetakan ke ISO 639-1), fallback heuristic dari nama file (mis.
     `Movie.id.srt`), fallback final ke
     `upload_targets.player4me.default_subtitle_language` (`id`).

2. **Mode B — sub file folder (`UPLOAD_PLAYER4ME_FOLDER`)** — input adalah
   URL **folder Google Drive** yang berisi 1 video utama + N file subtitle
   sidecar (.srt / .ass / .ssa / .vtt). Bot:
   - Enumerate isi folder lewat `GDriveAPIClient.list_folder()` (perlu OAuth
     atau Service Account — **tidak bisa** cuma dengan API key publik).
   - Pilih video terbesar sebagai main file, download semua sidecar
     individu ke `downloads/folder-<job_id>/`.
   - TUS upload video → recover videoId → upload tiap subtitle.
   - Cleanup `downloads/folder-<job_id>/` kalau
     `auto_delete_after_upload=true`.

### Mode mirror Telegram Index (`MIRROR_GDRIVE`)

`/mirror_gdrive URL_INDEX | keyword | folder_id` — ketiga argumen dipisah `|`,
2 yang terakhir optional.

- `URL_INDEX`: halaman HTML Telegram Index, contoh
  `https://zindex.aioarea.us.kg/zEu6c`.
- `keyword`: filter case-insensitive di nama file. Contoh `vmx` cuma
  ambil file yang mengandung `vmx` di nama.
- `folder_id`: ID folder Drive tujuan (override `gdrive_upload.default_folder_id`
  di `config.yaml`). Kosongkan untuk root Drive.

Setiap match → 1 job `MIRROR_GDRIVE`. Pipeline-nya **tidak** lewat
`modules/downloader.py` (generic) karena halaman index biasanya butuh login
dulu. Sebagai gantinya, `BotApp._run_job` mendispatch `MIRROR_GDRIVE` ke
`run_mirror_gdrive_full` di `telegram_handlers_gdrive.py` (sama pola dengan
`UPLOAD_PLAYER4ME_FOLDER`):

1. Ambil/buat `TGIndexClient` (cached di `bot_app._tgindex_client`) dengan
   `TGIndexCredentials` dari `.env` (`TGINDEX_USERNAME` /
   `TGINDEX_PASSWORD`).
2. Auto-login ke `<host>/login` saat cookie session belum ada / 401 / 302
   ke `/login`. Cookie `TG_INDEX_SESSION` di-cache per origin.
3. Download file dari `job.source_url` lewat `client.download_file()` —
   ini yang menggantikan `download()` standar; auth cookie disertakan di
   request, plus auto-retry 1x kalau di-redirect ke `/login` di tengah jalan.
4. Upload file lokal ke Drive lewat `GDriveUploader` (resumable upload Drive
   API v3, butuh OAuth atau Service Account — API key tidak bisa upload).
5. Auto delete file lokal kalau `app.auto_delete_after_upload=true`.

`MIRROR_GDRIVE` **tidak** lewat pengecekan `is_uploadable_video` — file apapun
boleh masuk (sub, .nfo, sample, dll), karena user mungkin filter by keyword
yang tidak mesti video.

Preview tanpa download: `/gdrive_list URL_INDEX | keyword`. Command preview
memakai `TGIndexClient` yang sama, jadi auto-login juga.

#### Telegram Index auth (`TGINDEX_USERNAME` / `TGINDEX_PASSWORD`)

Banyak instance Telegram Index sekarang wajib login. Login form-nya selalu
`POST /login` dengan field `username`, `password`, `remember`, `redirect_to`.
Setelah login server set cookie `TG_INDEX_SESSION` (HttpOnly). Bot:

- Pakai `aiohttp.ClientSession(cookies=...)` saat request page maupun file.
- Cache cookie di `TGIndexClient._cookies[origin]`. Reset on auth failure.
- Detect login wall via 2 cara:
  1. HTTP redirect (`302/303 Location: /login`).
  2. Halaman 200-OK yang isinya form `<form action="/login">` (beberapa
     instance render form login alih-alih redirect).
- `TGIndexAuthError` (subclass `TGIndexError`) di-raise dengan pesan jelas
  yang minta user set `TGINDEX_USERNAME` / `TGINDEX_PASSWORD`.

Kalau user belum set credentials di `.env`:
- `/gdrive_list` membalas error pesan auth yang ramah.
- `/mirror_gdrive` job per file akan FAILED dengan error yang sama (tanpa
  silent retry loop).

---

## Secrets & Konfigurasi

Semua secret hidup di `.env` (dibaca lewat `python-dotenv` di
`config_manager.py`). **Jangan pernah commit file `.env` asli.** Template ada
di `.env.example`.

| Env Var | Wajib? | Untuk apa |
| ------- | ------ | --------- |
| `TELEGRAM_BOT_TOKEN` | ✓ | Bot Telegram (BotFather) |
| `ADMIN_TELEGRAM_ID` | ✓ | Comma-separated user ID admin |
| `BUNNY_API_KEY` | optional | Upload ke Bunny Stream |
| `PLAYER4ME_API_TOKEN` | optional | Upload ke Player4Me |
| `GOOGLE_DRIVE_API_KEY` | optional | Download Drive (file public) |
| `GOOGLE_DRIVE_OAUTH_TOKEN_PATH` | optional | Download/upload Drive (file private user) — **direkomendasikan untuk Mode B & MIRROR_GDRIVE** |
| `GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON` | optional | Alternatif OAuth (akses via SA) |
| `TGINDEX_USERNAME` | optional | Username login Telegram Index (untuk `/gdrive_list` & `/mirror_gdrive`) |
| `TGINDEX_PASSWORD` | optional | Password login Telegram Index. Wajib kalau halaman index butuh login. |

Prioritas auth Drive: `OAuth token > Service Account > API key`. Untuk
**upload** ke Drive (mirror_gdrive) atau **list folder** (mode B), API key
tidak cukup — wajib OAuth atau SA.

Konfigurasi non-secret ada di `config.yaml`. Section penting:
- `app.auto_delete_after_upload` — auto hapus file lokal habis upload.
- `app.max_parallel_downloads` / `max_parallel_uploads` — concurrency queue.
- `download.max_file_size_gb` — limit ukuran file download (default 20 GB).
- `upload_targets.player4me.default_subtitle_language` — fallback bahasa sub.
- `gdrive_upload.default_folder_id` — folder Drive default untuk
  `/mirror_gdrive`.

---

## Cara Run Lokal

```bash
# Linux / WSL / macOS
chmod +x setup_linux.sh start_bot.sh
./setup_linux.sh                    # buat .venv + pip install -r requirements.txt
cp .env.example .env && $EDITOR .env
./start_bot.sh                      # auto sync deps + run bot.py

# Windows (PowerShell / cmd)
setup_windows.bat
copy .env.example .env  &&  notepad .env
start_bot.bat
```

Hanya **1 instance** bot yang boleh jalan per token Telegram. Kalau ada 2
proses jalan dengan token sama, polling akan loop `Conflict: terminated by
other getUpdates request` (sudah di-throttle ke 1 line/menit di logger).

`ffmpeg` dan `ffprobe` **wajib ada di PATH** untuk mode `UPLOAD_PLAYER4ME_SUBS`
dan `UPLOAD_PLAYER4ME_FOLDER` (sub extraction). Cek dengan `ffmpeg -version`.

---

## Pitfalls (Sudah Pernah Kejadian)

### Pitfall #1 — "Manual paste" handler files

Pernah ada modul `telegram_handlers_gdrive.py` yang berisi command function
dengan signature `async def cmd_xxx(self, update, context)` — `self`
mengisyaratkan method, tapi modul itu **bukan** kelas. Docstring di atas file
minta user "paste manual" body method ke `BotApp`. Hasilnya: command tidak
pernah di-register, user mengeluh "perintahnya gaada di Telegram".

**Aturan**: modul handler tambahan harus expose `register_xxx_handlers(app,
bot_app)` yang langsung daftar `CommandHandler` ke `Application`, dan command
function-nya pakai signature `async def cmd_xxx(bot_app, update, context)`
(bukan `self`). `BotApp._register_handlers` cukup panggil `register_xxx_handlers(app, self)`.

### Pitfall #2 — `JobType` baru tanpa update dispatch

Kalau menambah `JobType` baru, **wajib**:
- Tambah branch di `BotApp._run_job` (kalau tidak, dispatch fall through ke
  `else: raise RuntimeError("Tipe job tidak dikenali")`).
- Tambah branch di `BotApp._send_completion` (kalau tidak, notifikasi user
  jadi generic "Download Selesai" yang bingungin).
- Pertimbangkan apakah `is_uploadable_video` check perlu di-skip
  (`MIRROR_GDRIVE` skip, `UPLOAD_PLAYER4ME_*` tidak).

### Pitfall #3 — Player4Me TUS tidak return videoId

Player4Me TUS endpoint create video tapi tidak return `videoId` di response.
Solusinya (lihat `Player4MeUploader.find_recent_video_by_name`): snapshot
`/video/manage` **sebelum** TUS upload, snapshot lagi sesudahnya, diff
ID-nya. Kalau filename mengandung karakter khusus, name-match-nya pakai
`safe_filename`.

### Pitfall #4 — Drive folder URL di endpoint single-file

`UPLOAD_PLAYER4ME_FOLDER` **tidak** lewat `BotApp._run_job` standar (yang
panggil `download()` untuk single URL). `_run_job` cek `JobType.UPLOAD_PLAYER4ME_FOLDER` di awal
dan delegasi ke `_run_upload_player4me_folder` yang punya pipeline sendiri.
Jangan paksa folder URL ke `download()` — gdown akan error.

### Pitfall #5 — Auto-delete double-call

`_run_job` panggil `safe_unlink(result.path)` setelah job selesai (kalau
`auto_delete_after_upload=true`). Worker job tertentu (mis. `run_mirror_gdrive`)
**tidak boleh** panggil `safe_unlink` lagi — `safe_unlink` idempotent, tapi
kebiasaan double-call bikin log noisy dan masking real bug.

### Pitfall #6 — Notifikasi completion duplikat

Worker job **tidak boleh** kirim `_safe_send` notifikasi completion sendiri.
QueueManager akan trigger `BotApp._on_job_status_change` → `_send_completion`
otomatis ketika job di-mark `COMPLETED`. Cukup update job status, biar
QueueManager yang notify.

### Pitfall #7 — Generic `download()` tidak tahu cookie auth

`modules/downloader.py` `download()` adalah pipeline generic untuk URL
public. Kalau host butuh **session cookie** (mis. Telegram Index pasca-update
login), generic downloader akan mendarat di halaman `/login` dan return
HTML alih-alih file. Solusi yang sudah dipakai untuk `MIRROR_GDRIVE`:
bypass `download()` di `_run_job`, dispatch ke worker dedicated
(`run_mirror_gdrive_full`) yang pakai `TGIndexClient` dengan cookie cache.
Kalau ada source baru lain yang butuh cookie auth, pakai pola yang sama:
modul khusus + dispatch dedicated di `_run_job` paling atas (sebelum panggil
`download()`).

---

## Workflow Pengembangan

1. **Branch**: `devin/<timestamp>-<short-description>`. Contoh:
   `devin/1778357067-fix-gdrive-mirror-and-p4m-picker`.
2. **Commit message**: 1 baris ringkas (max 72 char) + body kalau perlu.
3. **PR ke `main`** dengan deskripsi:
   - Apa yang diubah & kenapa.
   - Cara test (kalau bisa, sertakan screenshot/log).
   - Catatan kompatibilitas (breaking changes, migrations).
4. **CI**: cek tab Actions di GitHub. Tidak ada test suite resmi (yet) — paling
   tidak `python -c "import modules.telegram_handlers"` harus sukses.
5. **Verifikasi end-to-end**: jalankan `./start_bot.sh` dengan `.env` lengkap
   dan test command di group/private chat dengan akun admin.

---

## Testing Checklist

Sebelum merge ke `main`, minimal:

- [ ] `python -m py_compile bot.py modules/*.py` sukses.
- [ ] `python -c "from modules.telegram_handlers import BotApp"` sukses.
- [ ] Smoke test: jalankan bot, cek `/help` muncul dengan command baru.
- [ ] Cek `_publish_bot_commands` mendaftarkan command baru ke autocomplete
  menu Telegram (test dengan `python -c` + mock `set_my_commands`).
- [ ] Kalau ada perubahan worker — coba 1 job real (Drive single file kecil
  → Player4Me / Bunny / Drive mirror) dan pastikan progress bar update + job
  selesai dengan notifikasi yang benar.

---

## Kontak

Maintainer: Sholehhuddin Alfaruq (@shaanieel) —
sholehhuddin21@gmail.com.

PR diterima. Untuk perubahan besar (refactor, replace dependency), buka issue
dulu untuk diskusi.
