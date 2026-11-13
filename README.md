# 📱 ESP32 Android Media Backup

> **One button press → all your Android photos/videos on your Mac.**
>
> An ESP32 acts as a Bluetooth keyboard (BLE HID), types a backup script into Termux on the Android device, which then uploads every media file to a Flask server on macOS via an ngrok HTTPS tunnel — with SHA-256 integrity verification and duplicate detection.

---

## How It Works

```
[Button on GPIO 19]
        │
        ▼
  ESP32 (BLE HID)  ──Bluetooth──▶  Android (Termux)
                                         │
                              bash heredoc script runs
                              curl uploads media files
                                         │
                                    ngrok HTTPS tunnel
                                         │
                                         ▼
                              macOS Flask Server (port 8019)
                              saves to Android_Media_Backup/
                              SHA-256 verified + logged
```

---

## Repository Structure

```
fetch_photos/
├── fetch_photos.ino                    ← Flash to ESP32 (Arduino IDE)
└── server/
    ├── server.py                       ← Run on macOS  (port 8019)
    ├── requirements.txt                ← pip install -r requirements.txt
    ├── .env                            ← Create this locally (auth token, never commit)
    ├── payloads/
    │   ├── media_backup.py             ← Python backup client for Termux on Android
    │   └── fetch_photos.py             ← Legacy curl-based client (reference only)
    └── Android_Media_Backup/           ← All received files land here (git-ignored)
        ├── DCIM/
        ├── Pictures/
        ├── Screenshots/
        ├── Movies/
        ├── Downloads/
        ├── Other/
        └── Reports/
            ├── acquisition_report.csv
            ├── acquisition_report.json
            ├── sha256_manifest.txt
            └── transfer.log
```

---

## Hardware Required

| Component | Notes |
|-----------|-------|
| ESP32 dev board (any variant) | WROOM-32, S2, S3, C3 all work |
| Momentary push button | Wired between **GPIO 19** and **GND** |
| Android phone with Termux | Install from **F-Droid** (not Play Store) |
| Mac (macOS) | Runs the Python server |
| ngrok account (free) | Provides the HTTPS tunnel |

**Wiring:** `GPIO 19 → Button → GND` (the sketch uses `INPUT_PULLUP`, no resistor needed).

---

## Quick Start

### Step 1 — Arduino IDE: install BLE keyboard library

1. In Arduino IDE open **Library Manager** and install **ESP32 BLE Keyboard** by T-vK.
2. Set board to your ESP32 variant and upload `fetch_photos.ino`.  
   _(Update `WAN_SERVER_URL` with your ngrok URL first — see Step 4.)_

### Step 2 — macOS: install server dependencies

```bash
cd fetch_photos/server
pip3 install -r requirements.txt
```

### Step 3 — macOS: create auth token

```bash
# Generate a strong random token
python3 -c "import secrets; print(secrets.token_hex(32))"

# Save it to .env  (this file is git-ignored)
echo 'UPLOAD_TOKEN=paste_your_token_here' > .env
```

### Step 4 — macOS: start ngrok tunnel

```bash
# Terminal tab 1
ngrok http 8019
```

Copy the `https://xxxx.ngrok-free.app` URL printed by ngrok.

### Step 5 — macOS: start the server

```bash
# Terminal tab 2
cd fetch_photos/server
python3 server.py
```

The server prints the detected ngrok URL and all endpoint addresses.

### Step 6 — ESP32: set the ngrok URL and re-flash

Open `fetch_photos.ino`, update line 32:

```cpp
#define WAN_SERVER_URL "https://xxxx.ngrok-free.app"
```

Re-upload to the ESP32.

### Step 7 — Android: one-time Termux setup

Install **Termux from F-Droid** ([f-droid.org/packages/com.termux](https://f-droid.org/packages/com.termux/)), then in Termux:

```bash
pkg update && pkg upgrade -y
pkg install python curl
termux-setup-storage          # tap "Allow" when Android prompts
```

Copy the backup client to Termux home:

```bash
# Option A — download directly from your running server
curl -s -L -H "ngrok-skip-browser-warning: true" \
     -o ~/media_backup.py \
     https://xxxx.ngrok-free.app/payloads/media_backup.py

# Option B — copy manually via USB / Bluetooth file transfer
cp /sdcard/Download/media_backup.py ~/media_backup.py
```

Set the auth token (must match the one in `.env` on macOS):

```bash
echo "paste_your_token_here" > ~/.backup_token
chmod 600 ~/.backup_token
```

---

## Running a Backup

1. Pair the Android phone to the ESP32 via Bluetooth.
2. Press the **button on GPIO 19**.
3. ESP32 opens Termux via BLE keyboard and types the upload script.
4. Termux shows a summary of discovered media by category and asks for confirmation.
5. Type `all` (or a category name: `DCIM`, `Screenshots`, `Pictures`, `Movies`, `Downloads`) and press Enter.
6. Type `y` to confirm.
7. Files upload with live progress. When done, `====== ALL DONE ======` is printed.

---

## Web Interface (macOS)

After the server is running, open these in your browser:

| URL | Description |
|-----|-------------|
| `http://localhost:8019/health` | Server liveness probe |
| `http://localhost:8019/files` | Photo/video gallery (grouped by category, auto-refreshes) |
| `http://localhost:8019/report` | JSON acquisition report |
| `http://localhost:8019/download/DCIM/<filename>` | Download a specific file |

---

## API Reference

### `GET /health`
No authentication required.
```json
{"status": "ok", "files": 42, "max_file_mb": 500}
```

### `POST /upload`
Requires `X-Backup-Token` header.  
Multipart/form-data: `file`, `sha256` (hex), `category`, `mtime` (ISO-8601, optional).
```json
{"ok": true, "saved": "20261113_120000_abc12345_IMG_1234.jpg",
 "category": "DCIM", "size_kb": 3421, "sha256": "...", "file_id": "..."}
```

### `GET /files`
HTML photo/video gallery — no auth required.

### `GET /download/<category>/<filename>`
Download a received file. Path traversal (`..`) is rejected.

### `GET /report`
JSON report with per-category counts, sizes, and last upload timestamps.

### `GET /payloads/<filename>`
Serves `media_backup.py` and other scripts to Termux — no auth required.

---

## Incremental Backup & Deduplication

- The Android client stores a local state file at `~/.media_backup_state.json`.
- Files already uploaded are detected by SHA-256 **before** uploading and skipped.
- The server also verifies SHA-256 **after** saving each file (corruption detection).
- Running the backup again only transfers new or changed files.
- To reset and re-upload everything: `rm ~/.media_backup_state.json` on Android.

---

## Security

- Auth token is compared with `hmac.compare_digest` (constant-time, timing-attack safe).
- The token is never printed to server logs.
- Uploaded filenames are sanitised (allowlist regex) before writing to disk.
- All download paths are verified to stay inside `BACKUP_DIR` using `Path.resolve()`.
- `termux-setup-storage` grants access only to Android shared storage (`/sdcard`); app-private data is never accessible.
- The user must explicitly type `y` before any file is transferred.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| ESP32 opens wrong app | Increase `openApp("Termux", 4000)` wait time in `.ino`. |
| Termux types in wrong app | Re-pair Bluetooth; ensure Android is unlocked. |
| `No media found` in Termux | Run `termux-setup-storage` and tap **Allow**. |
| `Auth error` in Termux | Check `~/.backup_token` matches `UPLOAD_TOKEN` in `.env`. |
| `SHA-256 mismatch` | Network corruption — script retries up to 3 times automatically. |
| Server not reachable | Confirm ngrok (Tab 1) and server (Tab 2) are both running; URL in `.ino` matches ngrok output. |
| Large files fail | ngrok free tier: 1 GB/month. For large libraries skip ngrok and use `http://MAC_LAN_IP:8019`. |
| Duplicate uploads every run | Delete `~/.media_backup_state.json` on Android to reset the incremental state. |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `UPLOAD_TOKEN` | _(empty — auth disabled)_ | Secret token clients must send in `X-Backup-Token` header |
| `BACKUP_DIR` | `./Android_Media_Backup` | Root directory for received files |

---

## License

MIT — do whatever you want, attribution appreciated.
