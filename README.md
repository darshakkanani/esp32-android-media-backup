# 📱 ESP32 Android Media Backup

> **One button press → all your Android photos/videos on your computer.**
>
> An ESP32 acts as a Bluetooth keyboard (BLE HID), types a backup script into Termux on the Android device, which then uploads every media file to a Flask server — with SHA-256 integrity verification and duplicate detection.
>
> **The server runs on macOS, Windows, and Linux.**

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
                         Flask Server (port 8019)
                         macOS · Windows · Linux
                         saves to Android_Media_Backup/
                         SHA-256 verified + logged
```

---

## Repository Structure

```
fetch_photos/
├── fetch_photos.ino                    ← Flash to ESP32 (Arduino IDE)
└── server/
    ├── server.py                       ← Python server (macOS / Windows / Linux)
    ├── start.sh                        ← One-click launcher  (macOS / Linux)
    ├── start.bat                       ← One-click launcher  (Windows)
    ├── requirements.txt                ← pip install -r requirements.txt
    ├── .env.example                    ← Copy to .env and add your token
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
| Computer (macOS / Windows / Linux) | Runs the Python server |
| ngrok account (free tier) | Provides the HTTPS tunnel |

**Wiring:** `GPIO 19 → Button → GND` (the sketch uses `INPUT_PULLUP`, no resistor needed).

---

## Server Setup

### Prerequisites by OS

#### macOS
```bash
# Python 3 (comes pre-installed on modern macOS, or install via Homebrew)
brew install python   # optional if already present

# ngrok
brew install ngrok/ngrok/ngrok
# OR download from https://ngrok.com/download
```

#### Windows
1. Install **Python 3.8+** from [python.org/downloads](https://www.python.org/downloads/)  
   ⚠️ Tick **"Add Python to PATH"** during installation.
2. Install **ngrok** from [ngrok.com/download](https://ngrok.com/download) — extract `ngrok.exe` somewhere on your `PATH` (e.g. `C:\Windows\System32\` or add its folder to PATH).

#### Linux (Ubuntu/Debian)
```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip curl

# ngrok
curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
  | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
echo "deb https://ngrok-agent.s3.amazonaws.com buster main" \
  | sudo tee /etc/apt/sources.list.d/ngrok.list
sudo apt update && sudo apt install ngrok
# OR just download the binary from https://ngrok.com/download
```

#### Linux (Fedora/RHEL)
```bash
sudo dnf install python3 python3-pip curl
# Install ngrok binary from https://ngrok.com/download
```

---

### Step 1 — Install Python dependencies

#### macOS / Linux — use the one-click launcher (recommended)
```bash
cd fetch_photos/server
chmod +x start.sh   # one-time
./start.sh          # creates .venv, installs deps, starts server
```
The script handles everything. Skip to **Step 3** if you use it.

#### Windows — use the one-click launcher (recommended)
Double-click `start.bat` inside `fetch_photos/server/`  
OR in a cmd / PowerShell window:
```bat
cd fetch_photos\server
start.bat
```
The script handles everything. Skip to **Step 3** if you use it.

#### Manual (all platforms)
```bash
cd fetch_photos/server
python3 -m venv .venv          # macOS / Linux
# python  -m venv .venv        # Windows

source .venv/bin/activate      # macOS / Linux
# .venv\Scripts\activate       # Windows (cmd)
# .venv\Scripts\Activate.ps1   # Windows (PowerShell)

pip install -r requirements.txt
```

---

### Step 2 — Set the auth token

```bash
# Generate a strong random token
python3 -c "import secrets; print(secrets.token_hex(32))"   # macOS / Linux
python  -c "import secrets; print(secrets.token_hex(32))"   # Windows
```

Create a `.env` file (copy from the example):

**macOS / Linux:**
```bash
cp .env.example .env
# Then edit .env and replace the placeholder with your token
```

**Windows (cmd):**
```bat
copy .env.example .env
notepad .env
```

The `.env` file looks like:
```
UPLOAD_TOKEN=your_generated_token_here
```

The same token must be set on Android — see [Android Setup](#android-setup).

---

### Step 3 — Start the ngrok tunnel

**macOS / Linux:**
```bash
# Terminal window 1
ngrok http 8019
```

**Windows:**
```bat
REM  Command Prompt / PowerShell window 1
ngrok http 8019
```

Copy the `https://xxxx.ngrok-free.app` URL printed by ngrok.

---

### Step 4 — Start the server

> Skip this step if you used `start.sh` / `start.bat` — they start the server automatically.

**macOS / Linux:**
```bash
# Terminal window 2
cd fetch_photos/server
source .venv/bin/activate
python3 server.py
```

**Windows:**
```bat
REM  Window 2
cd fetch_photos\server
.venv\Scripts\activate
python server.py
```

The server prints the detected ngrok URL and all endpoint addresses.

---

### Step 5 — Flash the ESP32

1. Open `fetch_photos.ino` in **Arduino IDE**.
2. Install the **ESP32 BLE Keyboard** library (Library Manager → search "ESP32 BLE Keyboard" by T-vK).
3. Replace `WAN_SERVER_URL` on line 32:
   ```cpp
   #define WAN_SERVER_URL "https://xxxx.ngrok-free.app"
   ```
4. Select your ESP32 board and upload.

---

## Android Setup

Install **Termux from F-Droid** ([f-droid.org/packages/com.termux](https://f-droid.org/packages/com.termux/)) — the Play Store build lacks storage permissions.

```bash
# In Termux on Android
pkg update && pkg upgrade -y
pkg install python curl
termux-setup-storage    # tap "Allow" when Android prompts
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

Set the auth token (must match the one in `.env` on your computer):

```bash
echo "your_generated_token_here" > ~/.backup_token
chmod 600 ~/.backup_token
```

Verify it works:
```bash
python3 ~/media_backup.py https://xxxx.ngrok-free.app
```

---

## Running a Backup

1. Pair the Android phone to the ESP32 via Bluetooth.
2. Press the **button on GPIO 19**.
3. ESP32 opens Termux via BLE keyboard and types the upload script.
4. Termux shows a summary of discovered media by category and asks for confirmation.
5. Type `all` (or a category name: `DCIM`, `Screenshots`, `Pictures`, `Movies`, `Downloads`) and press Enter.
6. Type `y` to confirm.
7. Files upload with live progress. `====== ALL DONE ======` is printed when complete.

---

## Web Interface

After the server is running, open these in your browser on any OS:

| URL | Description |
|-----|-------------|
| `http://localhost:8019/health` | Server liveness probe |
| `http://localhost:8019/files` | Photo/video gallery (grouped by category, auto-refreshes every 15 s) |
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
Returns `401` if token is wrong · `415` if extension unsupported · `422` if SHA-256 mismatch.

### `GET /files`
HTML photo/video gallery — no auth required.

### `GET /download/<category>/<filename>`
Download a received file. Path traversal (`..`) is rejected.

### `GET /report`
JSON report with per-category counts, sizes, and last upload timestamps.

### `GET /payloads/<filename>`
Serves `media_backup.py` and other scripts to Termux — no auth required.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `UPLOAD_TOKEN` | _(empty — auth disabled)_ | Secret token clients must send in `X-Backup-Token` header |
| `BACKUP_DIR` | `./Android_Media_Backup` | Root directory for received files |

Set via `.env` file (recommended) or as shell environment variables:

| OS | Syntax |
|----|--------|
| macOS / Linux | `export UPLOAD_TOKEN=your_secret` |
| Windows cmd | `set UPLOAD_TOKEN=your_secret` |
| Windows PowerShell | `$env:UPLOAD_TOKEN="your_secret"` |

---

## Incremental Backup & Deduplication

- The Android client stores state at `~/.media_backup_state.json` — files already uploaded are skipped by SHA-256 before uploading.
- The server verifies SHA-256 **after** saving each file (corruption detection).
- Running backup again only transfers new or changed files.
- To re-upload everything: `rm ~/.media_backup_state.json` on Android.

---

## Security

- Auth token compared with `hmac.compare_digest` (constant-time, timing-attack safe).
- Token is never printed to server logs.
- Uploaded filenames sanitised with allowlist regex before writing to disk.
- All download paths verified to stay inside `BACKUP_DIR` using `Path.resolve()`.
- User must explicitly type `y` before any file is transferred.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| ESP32 opens wrong app | Increase `openApp("Termux", 4000)` wait time in `.ino`. |
| Termux types in wrong app | Re-pair Bluetooth; ensure Android is unlocked. |
| `No media found` in Termux | Run `termux-setup-storage` and tap **Allow**. |
| `Auth error` in Termux | Check `~/.backup_token` on Android matches `UPLOAD_TOKEN` in `.env`. |
| `SHA-256 mismatch` | Network corruption — script retries up to 3× automatically. |
| Server not reachable | Confirm ngrok (window 1) and server (window 2) are both running; URL in `.ino` matches ngrok output. |
| Large files fail | ngrok free tier: 1 GB/month. Skip ngrok for big libraries — use `http://YOUR_LAN_IP:8019` and open port 8019 in your firewall. |
| Windows: `python not found` | Re-install Python and tick **"Add Python to PATH"**. |
| Windows: `flask not found` | Activate the venv first: `.venv\Scripts\activate`, then `pip install flask`. |
| Linux: permission denied on `.sh` | Run `chmod +x start.sh` once. |

---

## License

MIT — do whatever you want, attribution appreciated.
