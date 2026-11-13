# Android Media Backup — Complete Setup Guide

## Project Overview

Physical button on ESP32 GPIO 19 → BLE HID types into Termux on Android →  
Python backup client discovers media → user approves → files transfer via  
ngrok HTTPS tunnel → macOS server validates + stores with SHA-256 verification.

```
fetch_photos/
├── fetch_photos.ino                        ← Flash to ESP32
└── server/
    ├── server.py                           ← Run on macOS  (port 8019)
    ├── requirements.txt
    ├── .env                                ← Create this (auth token)
    ├── payloads/
    │   ├── media_backup.py                 ← Copy to ~/media_backup.py on Android
    │   └── fetch_photos.py                 ← Legacy (kept for reference)
    └── Android_Media_Backup/               ← All received files land here
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

## Step 1 — macOS: Install dependencies

```bash
cd /path/to/fetch_photos/server
pip3 install flask
```

---

## Step 2 — macOS: Set auth token

```bash
# Generate a strong token
python3 -c "import secrets; print(secrets.token_hex(32))"

# Save it (never commit this file to git)
echo 'UPLOAD_TOKEN=paste_your_token_here' > .env
```

The same token must be set on the Android device:

```bash
# In Termux on Android:
echo "paste_your_token_here" > ~/.backup_token
chmod 600 ~/.backup_token
```

---

## Step 3 — macOS: Start ngrok tunnel

```bash
# Tab 1
ngrok http 8019
```

Copy the `https://xxxx.ngrok-free.app` URL printed by ngrok.

---

## Step 4 — macOS: Start server

```bash
# Tab 2
cd fetch_photos/server
python3 server.py
```

The server prints the detected ngrok URL. Confirm it matches Tab 1.

---

## Step 5 — ESP32: Flash firmware

1. Open `fetch_photos.ino` in Arduino IDE.
2. Replace `WAN_SERVER_URL`:
   ```cpp
   #define WAN_SERVER_URL "https://xxxx.ngrok-free.app"
   ```
3. Flash to ESP32.

---

## Step 6 — Android: One-time Termux setup

Install Termux from **F-Droid** (not Play Store — the Play Store build lacks permissions).

```bash
# In Termux
pkg update && pkg upgrade -y
pkg install python
termux-setup-storage    # tap "Allow" in the system dialog
```

Copy the backup client to the home directory:

```bash
# Option A — copy from server via curl (run once after server is started)
curl -s -L -H "ngrok-skip-browser-warning: true" \
     -o ~/media_backup.py \
     https://xxxx.ngrok-free.app/payloads/media_backup.py

# Option B — copy manually via USB / Bluetooth file transfer
cp /sdcard/Download/media_backup.py ~/media_backup.py
```

Set the auth token:

```bash
echo "paste_your_token_here" > ~/.backup_token
chmod 600 ~/.backup_token
```

Verify it works:

```bash
python3 ~/media_backup.py https://xxxx.ngrok-free.app
```

---

## Step 7 — Run a backup (button trigger)

1. Android must be paired to ESP32 via Bluetooth.
2. Press the button on **GPIO 19**.
3. ESP32 types into Termux — Termux opens and `media_backup.py` launches.
4. The script shows discovered media by category and asks for confirmation.
5. Type `all` or a category name (e.g. `DCIM`, `Screenshots`) and press Enter.
6. Type `y` to confirm.
7. Files upload with live progress output in Termux.

---

## Viewing results on macOS

| URL | Description |
|-----|-------------|
| `http://localhost:8019/health` | Server status |
| `http://localhost:8019/files` | Photo/video gallery (grouped by category) |
| `http://localhost:8019/report` | JSON acquisition report |
| `http://localhost:8019/download/DCIM/<filename>` | Download individual file |

All files are saved to:
```
fetch_photos/server/Android_Media_Backup/
```

---

## SHA-256 Verification

The manifest file at `Android_Media_Backup/Reports/sha256_manifest.txt`  
contains one line per file: `<sha256>  <stored_filename>`

Verify all received files from macOS Terminal:

```bash
cd fetch_photos/server/Android_Media_Backup

# Verify every file against the manifest
while IFS='  ' read -r hash fname; do
  # find the file in any subdirectory
  path=$(find . -name "$fname" 2>/dev/null | head -1)
  if [ -z "$path" ]; then
    echo "MISSING: $fname"
  else
    actual=$(shasum -a 256 "$path" | awk '{print $1}')
    if [ "$actual" = "$hash" ]; then
      echo "OK      $fname"
    else
      echo "CORRUPT $fname  (expected $hash  got $actual)"
    fi
  fi
done < Reports/sha256_manifest.txt
```

---

## API Reference

### GET /health
No authentication required.
```json
{"status": "ok", "files": 42, "max_file_mb": 500}
```

### POST /upload
Requires `X-Backup-Token` header.  
Multipart/form-data fields: `file`, `sha256`, `category`, `mtime`.
```json
{"ok": true, "saved": "20261113_120000_abc12345_IMG_1234.jpg",
 "category": "DCIM", "size_kb": 3421, "sha256": "...", "file_id": "..."}
```
Returns `409` if SHA-256 already in manifest (duplicate).  
Returns `422` if server-computed SHA-256 does not match client-sent value (corruption).  
Returns `401` if token is wrong.  
Returns `415` if file extension is unsupported.

### GET /files
HTML gallery — no authentication required.  
Groups files by category. Auto-refreshes every 15 s.

### GET /download/\<category\>/\<filename\>
Download a file. No path traversal allowed (`..` rejected).

### GET /report
JSON acquisition report with per-category counts, sizes, and last upload time.

### GET /payloads/\<filename\>
Serves `media_backup.py` and other scripts to Termux. No authentication required.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| ESP32 opens wrong app | Check launcher: Pixel needs WIN alone (not WIN+S). Increase `openSearch` delay to 1800. |
| Termux types in wrong app | Increase `openApp("Termux", 4000)` wait time. |
| `No media found` | Run `termux-setup-storage` and tap Allow. |
| `Auth error` | Check `~/.backup_token` on Android matches `.env` on macOS. |
| `SHA-256 mismatch` | Network corruption — script retries automatically up to 3 times. |
| Server not reachable | Check ngrok is running (`Tab 1`), server is running (`Tab 2`), URL in `.ino` matches. |
| Large files fail | ngrok free tier has a 1 GB/month limit. For large libraries use LAN mode (skip ngrok, use `http://MAC_IP:8019`). |
| Duplicate uploads | Incremental state in `~/.media_backup_state.json` tracks uploaded SHAs. Delete to reset. |

---

## Security Notes

- Auth token is never printed to the terminal log by the server.
- Token comparison uses `hmac.compare_digest` to prevent timing attacks.
- Filenames are sanitised with a strict allowlist regex before saving.
- All download paths are verified to be inside `BACKUP_DIR` using `Path.resolve()`.
- `termux-setup-storage` grants access only to Android shared storage (`/sdcard`).
  Application-private data (`/data/data/...`) is never accessible from Termux.
- The script explicitly asks for user confirmation before transferring any file.
- No files are transferred without explicit `y` confirmation.

---

## Incremental Backup

The client stores a SHA-256 state file at `~/.media_backup_state.json` on Android.  
Files already uploaded are detected by hash before uploading and skipped.  
The server also performs a fast-path duplicate check against the manifest.  
Running the backup again only transfers new or changed files.
