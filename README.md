# ZAEIN Automation Bot

Bot Telegram automation untuk **download dari banyak sumber** lalu **upload ke
Bunny Stream / Player4Me**, dengan **queue system**, log, retry, dan health-check.

Dibuat agar mudah dijalankan di **Windows RDP** maupun **Linux VPS**, modular,
aman, dan siap dipindah-pindah server.

---

## Daftar Isi

1. [Fitur Utama](#fitur-utama)
2. [Struktur Project](#struktur-project)
3. [Persiapan: Install Python](#persiapan-install-python)
4. [Persiapan: Install FFmpeg (opsional)](#persiapan-install-ffmpeg-opsional)
5. [Buat Bot Telegram dari BotFather](#buat-bot-telegram-dari-botfather)
6. [Ambil Telegram User ID Anda](#ambil-telegram-user-id-anda)
7. [Ambil Bunny Stream API Key](#ambil-bunny-stream-api-key)
8. [Setup Google Drive API (opsional, sangat direkomendasikan)](#setup-google-drive-api-opsional-sangat-direkomendasikan)
9. [Setup Player4Me API (opsional)](#setup-player4me-api-opsional)
10. [Install Dependency](#install-dependency)
11. [Isi `.env` dan `config.yaml`](#isi-env-dan-configyaml)
12. [Menjalankan Bot](#menjalankan-bot)
13. [Daftar Command](#daftar-command)
14. [Test Download dan Test Upload Bunny](#test-download-dan-test-upload-bunny)
15. [Melihat Log](#melihat-log)
16. [Memindahkan Server (VPS / RDP baru)](#memindahkan-server-vps--rdp-baru)
17. [Backup Konfigurasi](#backup-konfigurasi)
18. [Auto-start saat Startup](#auto-start-saat-startup)
19. [Troubleshooting](#troubleshooting)
20. [Keamanan](#keamanan)

---

## Fitur Utama

- Download dari **Google Drive**, **Dropbox**, **OneDrive**, **direct link**, dan
  **link yang didukung yt-dlp**.
- **Engine pertama untuk Google Drive: Google Drive API resmi** (lebih stabil
  + bypass quota dengan service account), fallback otomatis ke `gdown` lalu
  `yt-dlp` kalau API tidak dikonfigurasi atau gagal.
- Upload ke **Bunny Stream** menggunakan API resmi (create video, put binary,
  cek status), tanpa hardcode API key.
- Upload ke **Player4Me** lewat protokol **TUS** (chunk 50 MiB, resumable).
- Command pendek **`/m URL`** menampilkan tombol **Bunny Stream**, **Player4Me**,
  atau **Download saja** — satu command untuk dua tujuan upload.
- **Bot command menu**: ketik `/` di Telegram, daftar semua command muncul
  otomatis lewat `setMyCommands`.
- **Queue system** async: kirim banyak job sekaligus, semua antri tertib.
- **Persistensi job** ke `data/jobs.json` (anti hilang setelah restart).
- **Cancel** dan **retry** per job.
- **Auto-delete** file lokal setelah upload sukses (opsional).
- **Health check** lengkap (internet, Bunny API, folder, FFmpeg, Python).
- **Akses hanya admin**: bot menolak siapa pun yang bukan `ADMIN_TELEGRAM_ID`.
- Log rotasi otomatis di `logs/bot.log`, **secrets selalu di-redaksi**.
- **Cross-platform**: Windows + Linux, semua path relatif.
- **Mudah dipindah server**: cukup copy folder, `.env`, `config.yaml`.

---

## Struktur Project

```
bottelegram/
├── bot.py                     # Entrypoint
├── config.yaml                # Konfigurasi non-rahasia
├── .env.example               # Template untuk .env (rahasia)
├── requirements.txt
├── setup_windows.bat          # Sekali jalan: install deps & buat venv (Windows)
├── setup_linux.sh             # Sekali jalan: install deps & buat venv (Linux)
├── start_bot.bat              # Double-click untuk jalankan bot (Windows)
├── start_bot.sh               # ./start_bot.sh untuk jalankan bot (Linux)
├── README.md
├── .gitignore
├── downloads/                 # File hasil download
├── temp/                      # File sementara saat download
├── logs/                      # Log bot (rotasi otomatis)
├── data/
│   └── jobs.json              # State antrean job (auto-generated)
└── modules/
    ├── __init__.py
    ├── config_manager.py      # Load .env + config.yaml
    ├── logger.py              # Rotating log + redaksi secret
    ├── validators.py          # Klasifikasi URL, sanitasi nama file, dll
    ├── storage_manager.py     # Disk stats, listing, delete
    ├── downloader.py          # Google Drive / direct / yt-dlp
    ├── bunny_uploader.py      # Bunny Stream API
    ├── queue_manager.py       # Async queue + persistensi
    └── telegram_handlers.py   # Semua command Telegram
```

> Semua path ditulis **relatif** terhadap root project. Tidak ada path lokal yang
> di-hardcode.

---

## Persiapan: Install Python

Bot ini butuh **Python 3.10+** (rekomendasi 3.11 / 3.12).

### Windows

1. Download dari <https://www.python.org/downloads/>.
2. Saat install, **centang `Add Python to PATH`**.
3. Verifikasi:
   ```cmd
   python --version
   ```

### Linux (Ubuntu / Debian)

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip
python3 --version
```

### Linux (RHEL / Fedora)

```bash
sudo dnf install -y python3 python3-pip
```

---

## Persiapan: Install FFmpeg (opsional)

FFmpeg dibutuhkan oleh **yt-dlp** untuk merge video + audio dari beberapa
platform. **Tidak wajib** untuk Google Drive / direct link.

### Windows

1. Download dari <https://www.gyan.dev/ffmpeg/builds/> (release "essentials").
2. Extract ke `C:\ffmpeg\`.
3. Tambahkan `C:\ffmpeg\bin` ke environment variable `Path`.
4. Verifikasi: `ffmpeg -version`.

### Linux

```bash
sudo apt-get install -y ffmpeg     # Ubuntu / Debian
sudo dnf install -y ffmpeg         # Fedora / RHEL
ffmpeg -version
```

---

## Buat Bot Telegram dari BotFather

1. Buka Telegram, cari `@BotFather`.
2. Kirim `/newbot`.
3. Isi nama bot (boleh apa saja) dan username (harus diakhiri `bot`,
   contoh `zaein_automation_bot`).
4. BotFather akan memberi **token bot** (format
   `123456789:ABCdef...`). **Simpan token ini**.
5. (Opsional) Kirim `/setprivacy` -> pilih bot Anda -> pilih `Disable` jika ingin
   bot membaca semua pesan grup.

---

## Ambil Telegram User ID Anda

1. Buka Telegram, cari `@userinfobot`.
2. Kirim `/start`.
3. Bot akan membalas dengan `Id: 123456789`. Itulah `ADMIN_TELEGRAM_ID` Anda.
4. Boleh isi lebih dari satu admin, pisahkan dengan koma:
   `ADMIN_TELEGRAM_ID=123456789,987654321`.

---

## Ambil Bunny Stream API Key

1. Login ke <https://dash.bunny.net/>.
2. Buka **Stream**, pilih library Anda (ID `656273` di config bawaan).
3. Buka tab **API**.
4. Salin **API Key** dari sana. **Simpan rahasia.**
5. Pastikan **Library ID** sesuai dengan yang Anda pakai (default: `656273`).

> Jangan ubah konfigurasi Pull Zone Bunny secara manual. Bot ini hanya
> menggunakan **Bunny Stream API**.

---

## Setup Player4Me API (opsional)

Kalau ingin pakai command `/m URL` dengan tombol **Player4Me** atau
`/upload_player4me`, bot butuh API token Player4Me.

1. Login ke <https://player4me.com/>.
2. Buka menu **API** di dashboard Anda dan salin API token.
3. Tempel ke `.env`:
   ```
   PLAYER4ME_API_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
4. Restart bot, lalu kirim `/health`. Akan muncul baris seperti:

   ```
   Player4Me API: OK (OK)
   ```

Di `config.yaml` Anda bisa atur:

```yaml
upload_targets:
  player4me:
    enabled: true
    prefer_url_ingest: true   # coba server-side fetch dulu untuk URL publik
    default_folder_id: ""     # opsional: folder ID tujuan di Player4Me
```

**Engine upload Player4Me**: bot mengimplementasikan protokol **TUS 1.0.0**
resmi mereka dengan **chunk 50 MiB** (52,428,800 byte) lewat aiohttp keep-alive
— ini engine paling cepat untuk upload file lokal. Untuk URL ingest (server
player4me yang fetch sendiri dari URL publik), endpoint
`POST /api/v1/video/advance-upload` juga sudah dibungkus di
`modules/player4me_uploader.py`.

---

## Setup Google Drive API (opsional, sangat direkomendasikan)

Untuk semua link Google Drive, bot akan **mencoba Google Drive API resmi
terlebih dahulu** sebelum fallback ke `gdown` / `yt-dlp`. Pakai API resmi punya
kelebihan dibanding scraping HTML lewat `gdown`:

- **Lebih stabil** — tidak rusak saat Google ubah halaman download.
- **Bypass "quota exceeded"** — pakai project / service account sendiri.
- **Akses file private** — file di Drive Anda sendiri / Shared Drive yang
  di-share ke service account bisa di-download tanpa "Anyone with the link".
- **Lebih cepat** untuk file besar.

Ada **tiga** mode auth, pilih sesuai kebutuhan. Prioritas runtime jika lebih
dari satu di-set: **OAuth user > Service Account > API Key**.

### Mode A — OAuth user token (TERBAIK untuk file private Anda sendiri)

Pendekatan ini sama dengan yang dipakai Pikabot / mirror-leech-telegram-bot:
Anda login ke akun Google sekali lewat browser, dan bot disimpan token-nya.
Setelah itu bot bisa baca **semua file Anda** (termasuk private), karena
request ke Drive API dilakukan **sebagai Anda**, bukan sebagai pemegang
API key anonim.

1. Buka <https://console.cloud.google.com/apis/credentials>.
2. **CREATE CREDENTIALS** -> **OAuth client ID**. Kalau diminta, isi OAuth
   consent screen dulu (cukup External -> nama app bebas -> simpan).
3. Application type: **Desktop app**. Beri nama bebas, klik **Create**.
4. Klik **DOWNLOAD JSON** pada client yang baru. Simpan ke
   `secrets/credentials.json` (folder `secrets/` sudah di `.gitignore`).
5. (Sekali saja) jalankan helper untuk login dan generate token:
   ```bash
   python tools/generate_drive_token.py
   ```
   Browser akan terbuka, login ke akun Google Anda, klik **Allow**. Helper
   akan menulis `secrets/token.pickle`. Pakai `--no-browser` kalau VPS
   headless (akan kasih URL untuk dibuka di browser lokal).
6. Tempel path ke `.env`:
   ```
   GOOGLE_DRIVE_OAUTH_TOKEN_PATH=secrets/token.pickle
   ```
7. Restart bot. Kirim `/health`, harus muncul:
   ```
   GDrive API: OK [oauth_user] (OAuth user token OK (akses semua Drive Anda))
   ```

Token otomatis di-refresh oleh bot pakai `refresh_token`. Anda hanya perlu
jalankan `generate_drive_token.py` lagi kalau Anda revoke akses lewat
<https://myaccount.google.com/permissions> atau hapus `token.pickle`.

> Sudah punya `token.pickle` dari Pikabot/MLTB? Bisa langsung pakai. Tinggal
> copy file-nya ke `secrets/token.pickle` dan set
> `GOOGLE_DRIVE_OAUTH_TOKEN_PATH`. Format pickle dan JSON dua-duanya didukung.

### Mode B — API Key (paling cepat setup, cuma untuk file PUBLIC)

1. Buka <https://console.cloud.google.com/apis/credentials>.
2. Buat / pilih sebuah project.
3. Aktifkan **Google Drive API** di
   <https://console.cloud.google.com/apis/library/drive.googleapis.com>.
4. Klik **Create Credentials** -> **API key**. Salin keynya.
5. (Opsional tapi bagus) Klik API key yang baru, lalu di **API restrictions**
   pilih `Restrict key` -> centang `Google Drive API` saja.
6. Tempel ke `.env`:
   ```
   GOOGLE_DRIVE_API_KEY=AIzaSy...
   ```

API key hanya bekerja untuk file dengan akses "Anyone with the link". File
benar-benar private akan return 404.

### Mode C — Service Account (file PUBLIC + PRIVATE yang di-share ke SA)

1. Di Cloud Console yang sama, **IAM & Admin** -> **Service Accounts** ->
   **Create Service Account**.
2. Beri nama `zaein-drive-downloader`, klik Create dan **Done** (tidak perlu
   role IAM project-level).
3. Klik service account yang baru -> tab **Keys** -> **Add Key** -> **Create
   new key** -> **JSON**. File JSON akan otomatis ter-download.
4. Simpan file ke folder project, contoh `secrets/sa-drive.json` (folder
   `secrets/` sudah di `.gitignore`).
5. **Penting**: share file / folder / Shared Drive yang ingin di-download ke
   email service account (alamatnya seperti
   `zaein-drive-downloader@xxx.iam.gserviceaccount.com`) dengan akses minimal
   `Viewer`.
6. Tempel path ke `.env`:
   ```
   GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON=secrets/sa-drive.json
   ```

Library `google-auth` (sudah ada di `requirements.txt`) yang dipakai untuk
sign JWT dan tukar dengan access token.

### Verifikasi

Setelah isi `.env`, jalankan ulang bot dan kirim `/health`. Akan muncul baris
seperti:

```
GDrive API: OK [service_account] (Service Account OK)
```

atau jika tidak dikonfigurasi:

```
GDrive API: tidak dikonfigurasi (fallback ke gdown)
```

> Tidak perlu set semua. Prioritas: OAuth user > Service Account > API Key.
> Kalau semua kosong, bot otomatis pakai `gdown` (lalu fallback ke direct
> uc-download) seperti sebelumnya.

---

## Install Dependency

### Pakai script otomatis (paling cepat)

#### Linux / RDP Linux

```bash
chmod +x setup_linux.sh
./setup_linux.sh
```

#### Windows

```cmd
setup_windows.bat
```

Skrip akan: cek Python, buat virtualenv `venv/`, install requirements,
buat folder runtime, dan menyalin `.env.example` ke `.env` (jika belum ada).

### Manual

#### Linux

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt
cp .env.example .env
```

#### Windows

```cmd
python -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip wheel
pip install -r requirements.txt
copy .env.example .env
```

---

## Isi `.env` dan `config.yaml`

### `.env` - secrets (jangan commit ke git)

```
TELEGRAM_BOT_TOKEN=123456789:ABCdef...
ADMIN_TELEGRAM_ID=123456789
BUNNY_API_KEY=xxxxx-xxxxx-xxxxx
GOOGLE_DRIVE_API_KEY=
GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON=
```

- `TELEGRAM_BOT_TOKEN` -> dari BotFather.
- `ADMIN_TELEGRAM_ID` -> dari `@userinfobot` (boleh lebih dari satu, pisahkan
  koma).
- `BUNNY_API_KEY` -> dari Bunny dashboard.
- `GOOGLE_DRIVE_API_KEY` -> opsional, untuk download file Drive PUBLIC pakai
  Google Drive API resmi (lihat seksi "Setup Google Drive API").
- `GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON` -> opsional, path ke file JSON service
  account untuk akses file PRIVATE / Shared Drive (lihat seksi yang sama).

### `config.yaml` - non-rahasia

Edit secukupnya. Yang sering diubah:

```yaml
app:
  max_parallel_downloads: 1     # naikkan kalau VPS / RDP kuat
  auto_delete_after_upload: true

bunny:
  library_id: "656273"
  cdn_hostname: "vz-eaddacc6-5f8.b-cdn.net"

download:
  max_file_size_gb: 20
```

> `paths` tidak perlu diubah - semuanya relatif ke folder project.

---

## Menjalankan Bot

### Cara cepat — pakai launcher (rekomendasi)

#### Windows

Cukup **double-click `start_bot.bat`** di folder project. Script ini akan:

1. Cek `venv\` dan `.env` sudah ada (kalau belum, kasih instruksi yang harus
   dijalankan).
2. Activate venv otomatis dan jalankan `python bot.py`.
3. Auto-restart sampai 5x kalau bot crash (delay 5 detik antar restart).
4. Tetap di terminal supaya log bot kelihatan; tutup window untuk stop.

#### Linux

```bash
chmod +x start_bot.sh
./start_bot.sh
```

(`chmod +x` cukup sekali.) Script `.sh` punya fitur sama seperti `.bat`:
auto-restart, cek venv & `.env`, dan trap Ctrl+C untuk shutdown bersih.

### Cara manual

#### Windows

```cmd
cd bottelegram
venv\Scripts\activate
python bot.py
```

#### Linux

```bash
cd bottelegram
source venv/bin/activate
python bot.py
```

Setelah ada log `Bot ready, mulai polling...`, buka Telegram, kirim `/start` ke
bot Anda. Hanya `ADMIN_TELEGRAM_ID` yang dibalas.

---

## Daftar Command

| Command | Deskripsi |
| --- | --- |
| `/start` | Info bot dan panduan singkat. |
| `/help` | Tampilkan semua command. |
| `/m URL` | **Mirror**: download dari URL, lalu pilih tombol **Bunny Stream / Player4Me / Download saja**. Judul otomatis diambil dari nama file di sumber. |
| `/upload_bunny [Judul \|] URL` | Download + upload ke Bunny Stream. Judul opsional. |
| `/upload_player4me [Judul \|] URL` | Download + upload ke Player4Me (TUS). Judul opsional. |
| `/download [Judul \|] URL` | Download saja (alias `/download_only`). |
| `/download_only [Judul \|] URL` | Download saja, tanpa upload. |
| `/status VIDEO_ID` | Cek status video di Bunny. |
| `/queue` | Lihat antrean job. |
| `/cancel JOB_ID` | Batalkan job. |
| `/retry JOB_ID` | Ulangi job gagal. |
| `/history` | 10 job terakhir. |
| `/list_files` | Isi folder downloads. |
| `/delete_file nama_file` | Hapus file tertentu di downloads. |
| `/clear_downloads` | Hapus semua file di downloads. |
| `/storage` | Total / terpakai / sisa storage. |
| `/health` | Cek bot, internet, Bunny API, Player4Me API, GDrive API, folder, FFmpeg, Python. |
| `/config` | Tampilkan konfigurasi aktif (tanpa secret). |
| `/backup_config` | Kirim file backup konfigurasi (tanpa API key). |

---

## Test Download dan Test Upload Bunny

1. Pastikan bot sudah jalan dan Anda sudah dibalas saat `/start`.
2. Tes mirror dengan tombol pilihan upload:
   ```
   /m https://drive.google.com/file/d/<FILE_ID>/view
   ```
   Bot akan reply dengan tombol **Bunny Stream**, **Player4Me**, dan **Batal**.
   Klik salah satu untuk memulai job. Judul otomatis diambil dari nama file di
   Drive (lewat metadata API kalau dikonfigurasi).
3. Tes download saja:
   ```
   /download_only Test Video | https://drive.google.com/file/d/<FILE_ID>/view
   ```
   Bot akan menambahkan job, mengirim progress, lalu memberitahu hasil
   download.
4. Tes download + upload Bunny:
   ```
   /upload_bunny The Drama 2026 | https://drive.google.com/file/d/<FILE_ID>/view
   ```
   Setelah upload selesai bot akan mengirim:

   ```
   Upload Bunny Berhasil
   Judul: The Drama 2026
   Video ID: <guid>
   Status: <encoding/processing/finished>
   Embed URL: https://iframe.mediadelivery.net/embed/656273/<guid>
   CDN Hostname: vz-eaddacc6-5f8.b-cdn.net
   ```

5. Cek progress encoding kapan saja:
   ```
   /status <guid>
   ```

6. Tes upload ke Player4Me langsung tanpa tombol:
   ```
   /upload_player4me https://drive.google.com/file/d/<FILE_ID>/view
   ```
   Setelah upload selesai bot akan kirim ringkasan dengan engine yang dipakai
   (`TUS upload (lokal)` untuk file lokal).

---

## Melihat Log

- File log utama: `logs/bot.log`.
- Rotasi otomatis: `bot.log`, `bot.log.1`, `bot.log.2`, ... (ukuran & jumlah
  diatur di `config.yaml` -> `logging`).

Linux:
```bash
tail -f logs/bot.log
```
Windows:
```cmd
type logs\bot.log
```

Bot **tidak pernah** menulis `TELEGRAM_BOT_TOKEN` atau `BUNNY_API_KEY` ke log.

---

## Memindahkan Server (VPS / RDP baru)

1. Copy seluruh folder project ke server baru (boleh via `scp`, RDP file copy,
   atau `git clone` lalu replace `.env`).
2. Salin file rahasia: `.env` dan jika perlu `config.yaml`.
3. Install Python (lihat seksi Persiapan).
4. Jalankan ulang `setup_linux.sh` atau `setup_windows.bat`.
5. `python bot.py` -> selesai.

> Karena semua path **relatif**, tidak perlu mengubah konfigurasi setelah
> pindah.

---

## Backup Konfigurasi

- Lewat Telegram: kirim `/backup_config` ke bot. Bot akan mengirim file
  `config-backup-<timestamp>.yaml` **tanpa** token / API key.
- Restore: copy nilai dari file backup ke `config.yaml`, lalu isi ulang `.env`
  secara manual (token & API key memang sengaja tidak di-backup).

---

## Auto-start saat Startup

### Linux (systemd)

Buat file `/etc/systemd/system/zaein-bot.service`:

```ini
[Unit]
Description=ZAEIN Automation Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/bottelegram
ExecStart=/home/ubuntu/bottelegram/venv/bin/python /home/ubuntu/bottelegram/bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Aktifkan:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now zaein-bot.service
sudo systemctl status zaein-bot.service
journalctl -u zaein-bot.service -f
```

### Windows (Task Scheduler)

1. Buka **Task Scheduler** -> **Create Task**.
2. Tab **General**: centang `Run whether user is logged on or not` dan
   `Run with highest privileges`.
3. Tab **Triggers** -> **New** -> `At log on` (atau `At startup`).
4. Tab **Actions** -> **New** ->
   - Program/script: `C:\path\ke\bottelegram\venv\Scripts\python.exe`
   - Add arguments: `bot.py`
   - Start in: `C:\path\ke\bottelegram`
5. Tab **Conditions**: matikan `Start the task only if the computer is on AC
   power` jika di RDP.
6. Tab **Settings**: centang `If the task fails, restart every 1 minute`.
7. OK -> masukkan password user -> simpan.

---

## Troubleshooting

| Masalah | Solusi |
| --- | --- |
| `TELEGRAM_BOT_TOKEN is empty` | Isi `.env`, jangan commit. Pastikan `.env` di root project. |
| Bot tidak membalas | User Anda bukan admin, tambahkan ID di `ADMIN_TELEGRAM_ID`. |
| Download Google Drive 0 byte | File terlalu populer / private. Set `GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON` lalu share file ke email SA, atau bot fallback ke yt-dlp. |
| `gdown error: Permission denied` | File GDrive butuh login / private. Pakai mode Service Account (lihat seksi GDrive API), atau share link "Anyone with the link". |
| `GDriveAPI ... HTTP 403 (storageQuotaExceeded)` | Quota harian project Cloud habis. Tunggu reset, naikkan kuota, atau pakai service account lain. |
| `/health` GDrive API FAIL `google-auth tidak terinstall` | Jalankan `pip install -r requirements.txt` ulang setelah update bot. |
| `HTTP 401` saat upload Bunny | API key salah / library ID salah. Cek dashboard Bunny. |
| Player4Me HTTP 401 | `PLAYER4ME_API_TOKEN` salah / kedaluwarsa. Generate ulang di dashboard player4me. |
| Player4Me "belum dikonfigurasi" | Isi `PLAYER4ME_API_TOKEN` di `.env`, lalu restart bot. |
| Tombol /m kadaluarsa | Kirim ulang `/m URL`. Pending button ID disimpan in-memory — hilang setelah restart. |
| Command `/` tidak muncul saran | Tunggu beberapa detik setelah bot start (Telegram cache `setMyCommands`), atau tutup-buka chat bot. |
| Upload mentok di "Processing" | Wajar untuk video besar. Cek dengan `/status VIDEO_ID`. |
| Storage penuh | Jalankan `/clear_downloads` atau aktifkan `auto_delete_after_upload`. |
| `yt-dlp` butuh FFmpeg | Install FFmpeg (lihat seksi di atas). |
| Windows: `python` tidak dikenal | Saat install Python, centang `Add Python to PATH`. |
| Linux systemd: `status=203/EXEC` | Cek path Python di service file (`venv/bin/python`). |
| Banyak job sekaligus tertumpuk | Naikkan `app.max_parallel_downloads` di `config.yaml`. |

---

## Keamanan

- `.env` **selalu** di `.gitignore`. Jangan pernah commit secret.
- Token bot & API key Bunny tidak pernah ditampilkan ke user dan tidak ditulis
  ke log (filter redaksi otomatis).
- Hanya `ADMIN_TELEGRAM_ID` yang bisa menjalankan command. User lain
  ditolak dan dicatat di log.
- Nama file disanitasi (`safe_filename`) sebelum disimpan ke disk.
- Bot **tidak pernah** mengeksekusi shell command dari user.
- Path traversal pada `/delete_file` dicegah secara eksplisit.
- Validasi ekstensi sebelum upload ke Bunny: hanya `.mp4`, `.mkv`, `.mov`,
  `.avi`, `.webm`, `.m4v`.

---

Selamat menggunakan, dan semoga lancar otomasinya. Jika ada error yang perlu
investigasi cepat, sertakan isi `logs/bot.log` (yang sudah ter-redaksi
otomatis) saat meminta bantuan.
