# ESP32 BLE HID — Android Media Backup Lab

> **CTF / Hardware Hacking Lab — Authorized Devices Only**

A hardware security research project: an **ESP32 acts as a Bluetooth HID keyboard**, silently navigates to Termux on an Android device, and runs a Python script that uploads all media files to a **Flask server on your computer** — all triggered from a **web dashboard** with no physical button press needed.

Built for **controlled lab environments**: CTF challenges, security awareness demos, red-team training, and personal hardware experimentation on devices you own.

---

## ⚠️ Legal Disclaimer

```
THIS TOOL IS FOR AUTHORIZED SECURITY RESEARCH AND EDUCATIONAL USE ONLY.

You may ONLY use this tool on:
  - Devices you personally own, OR
  - Devices for which you have explicit written authorization from the owner.

Unauthorized use is a criminal offense under the IT Act (India), CFAA (USA),
Computer Misuse Act (UK), and equivalent laws in every other jurisdiction.

The author accepts no liability for misuse. By using this project you confirm
that you have full legal authorization for every device you target.
```

---

## What This Does

When triggered from the web dashboard (or by pressing the ESP32 button), the ESP32:

1. Opens **Termux** on the connected Android phone via BLE keystrokes
2. Clears the terminal prompt cleanly
3. Types and executes a **self-installing bash heredoc** that:
   - Downloads `media_backup.py` from the server if not present
   - Fetches and stores an auth token automatically
   - Runs `media_backup.py --auto` — fully non-interactive
4. `media_backup.py` scans all storage, computes SHA-256 hashes, and POSTs every file to the server via `curl` over an ngrok HTTPS tunnel
5. Files appear instantly in the **web gallery** at `http://localhost:8019/files`

---

## Attack Surface Demonstrated

| Technique | Description |
|-----------|-------------|
| BLE HID spoofing | ESP32 advertises as "ESP32 Keyboard" — a legitimate Bluetooth HID device |
| Keystroke injection | Commands typed at 35 ms/char — below human notice threshold |
| Self-installing payload | Heredoc downloads and sets up the exfiltration script on first run |
| Remote trigger | Web dashboard queues a command; ESP32 polls every 2 s and fires autonomously |
| Out-of-band exfiltration | Files leave via HTTPS tunnel — bypasses most network monitoring |
| SHA-256 integrity | Server verifies every file on receipt |
| Acquisition logging | CSV + JSON report + manifest — mirrors forensic acquisition workflow |

---

## Architecture

```
  ┌─────────────────────────┐
  │    Web Dashboard         │  http://localhost:8019/dashboard
  │  (Remote Trigger page)   │
  └────────────┬────────────┘
               │  POST /trigger
               ▼
  ┌─────────────────────────┐
  │   Flask Server           │  port 8019  (macOS / Windows / Linux)
  │   server/server.py       │  ← /api/poll, /upload, /files, /dashboard
  └────────────┬────────────┘
               │  ngrok HTTPS tunnel
               │
  ┌────────────┴────────────┐
  │   ESP32 (WiFi + BLE)    │  polls /api/poll every 2 s
  │   fetch_photos.ino      │
  └────────────┬────────────┘
               │  Bluetooth HID (keystrokes)
               ▼
  ┌─────────────────────────┐
  │   Android Device        │  your lab phone
  │   (Termux)              │
  └────────────┬────────────┘
               │  curl  HTTPS → ngrok → server
               ▼
  Android_Media_Backup/
  ├── DCIM/        ← camera photos & videos
  ├── Pictures/    ← gallery images
  ├── Screenshots/
  ├── Movies/
  ├── Downloads/
  └── Reports/    ← transfer.log, sha256_manifest.txt, .csv, .json
```

---

## Lab Requirements

| Item | Notes |
|------|-------|
| ESP32 dev board | Any variant — WROOM-32, S2, S3, C3 |
| Android phone (**yours**) | Termux installed from [F-Droid](https://f-droid.org/packages/com.termux/) |
| Computer | macOS, Windows, or Linux — runs the receiver server |
| Free ngrok account | Tunnel from [ngrok.com](https://ngrok.com) |
| WiFi network | ESP32 and your computer must share the same ngrok-reachable URL |

---

## Project Structure

```
esp32-android-media-backup/
├── fetch_photos.ino          ← ESP32 firmware (BLE HID + WiFi polling)
├── README.md
├── LICENSE
├── .gitignore
└── server/
    ├── server.py             ← Flask server — all endpoints + dashboard HTML + gallery
    ├── start.sh              ← One-click launcher (macOS / Linux)
    ├── start.bat             ← One-click launcher (Windows)
    ├── requirements.txt      ← Flask + python-dotenv
    ├── .env.example          ← Config template
    └── payloads/
        └── media_backup.py   ← Exfiltration client — runs inside Termux
```

---

## Full Setup — Step by Step

---

### PART 1 — Get the Code

**Step 1 — Clone the repository**

```bash
git clone https://github.com/darshakkanani/esp32-android-media-backup.git
cd esp32-android-media-backup
```

---

### PART 2 — Set Up the Server

**Step 2 — Install Python 3**

| OS | How |
|----|-----|
| macOS | Pre-installed, or `brew install python` |
| Windows | [python.org/downloads](https://python.org/downloads) — ⚠️ tick **"Add Python to PATH"** |
| Ubuntu/Debian | `sudo apt install python3 python3-venv python3-pip` |
| Fedora/RHEL | `sudo dnf install python3 python3-pip` |

**Step 3 — Install ngrok**

| OS | How |
|----|-----|
| macOS | `brew install ngrok/ngrok/ngrok` |
| Windows | Download `ngrok.exe` from [ngrok.com/download](https://ngrok.com/download), add to PATH |
| Linux | Download binary from [ngrok.com/download](https://ngrok.com/download) |

Authenticate (one-time):
```bash
ngrok config add-authtoken YOUR_NGROK_TOKEN
```
Get your token from [dashboard.ngrok.com/get-started/your-authtoken](https://dashboard.ngrok.com/get-started/your-authtoken).

**Step 4 — Install Python dependencies**

macOS / Linux:
```bash
cd server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows (cmd):
```bat
cd server
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

> **Shortcut:** `./start.sh` (macOS/Linux) or double-click `start.bat` (Windows) to do steps 4–7 automatically.

**Step 5 — Generate a secret upload token**

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"   # macOS / Linux
python  -c "import secrets; print(secrets.token_hex(32))"   # Windows
```

Save this string — you need it again in Step 13 (Android) and in the firmware (Step 10).

**Step 6 — Generate a setup password**

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(16))"
```

This is a separate short password used by the ESP32 to fetch the token automatically.

**Step 7 — Create your `.env` file**

```bash
cp .env.example .env   # macOS / Linux
# copy .env.example .env   # Windows
```

Edit `server/.env`:
```ini
UPLOAD_TOKEN=your_token_from_step_5_here
SETUP_PASSWORD=your_password_from_step_6_here
```

**Step 8 — Start the ngrok tunnel**

Open a **new terminal window**:
```bash
ngrok http 8019
```

Copy the `https://xxxx.ngrok-free.app` URL — needed in Steps 10 and 12.

**Step 9 — Start the server**

In a **second terminal window**:

macOS / Linux:
```bash
cd server
source .venv/bin/activate
python3 server.py
```

Windows:
```bat
cd server
.venv\Scripts\activate
python server.py
```

The server prints: `Running on http://0.0.0.0:8019`. Keep this window open during the lab.  
Open the dashboard: **http://localhost:8019/dashboard**

---

### PART 3 — Flash the ESP32

**Step 10 — Set up Arduino IDE**

1. Download [Arduino IDE 2](https://www.arduino.cc/en/software).
2. **File → Preferences → Additional Board URLs**, add:
   ```
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
   ```
3. **Tools → Board → Board Manager** → search `esp32` → install **"esp32 by Espressif Systems"**.
4. **Tools → Manage Libraries** → search **"ESP32 BLE Keyboard"** → install (by T-vK).
5. **Tools → Manage Libraries** → search **"ArduinoJson"** → install (by Benoit Blanchon).

**Step 11 — Edit firmware credentials**

Open `fetch_photos.ino` and update the top section:

```cpp
#define WAN_SERVER_URL  "https://xxxx.ngrok-free.app"  // ← your ngrok URL (Step 8)
#define WIFI_SSID       "YourWiFiName"                  // ← your WiFi SSID
#define WIFI_PASSWORD   "YourWiFiPassword"              // ← your WiFi password
#define SETUP_PASSWORD  "your_password_from_step_6"    // ← matches server .env
```

**Step 12 — Upload to ESP32**

1. Connect ESP32 via USB.
2. **Tools → Board** → select your ESP32 model.
3. **Tools → Port** → select the correct COM port.
4. Click **Upload** (Ctrl+U).

> ⚠️ ngrok generates a new URL every restart. Update `WAN_SERVER_URL` and re-upload when this happens.

---

### PART 4 — Prepare the Android Device (your device)

**Step 13 — Install Termux from F-Droid**

Install from **F-Droid only**: [f-droid.org/packages/com.termux](https://f-droid.org/packages/com.termux/)

The Play Store build is outdated and will not work.

**Step 14 — Set up Termux**

```bash
pkg update && pkg upgrade -y
pkg install python curl
termux-setup-storage
```

Tap **Allow** when Android asks for storage permission.

> The script downloads `media_backup.py` and fetches the auth token automatically on first run. No manual file download required.

---

### PART 5 — Pair Bluetooth and Run

**Step 15 — Pair the ESP32**

1. Power on the ESP32.
2. On your Android device: **Settings → Bluetooth** → scan.
3. Pair **"ESP32 Keyboard"**.

The dashboard's Remote Trigger page shows:
- 🟢 **ESP32 — Online** when the ESP32 is polling
- 🟢 **Android — Paired & Connected** when Bluetooth is active

**Step 16 — Fire the payload**

1. Open the dashboard: **http://localhost:8019/dashboard → Remote Trigger**
2. Select the file types you want to collect.
3. Click **🚀 Fire Payload**.

The ESP32 picks up the command within 2 seconds, opens Termux on the Android device, injects the heredoc, and uploads everything. Files appear in the gallery at `http://localhost:8019/files`.

---

## Web Dashboard Pages

| Page | URL path | Description |
|------|----------|-------------|
| **Overview** | `/dashboard` | KPIs, upload sparkline, storage donut, category breakdown |
| **Backup History** | `/dashboard` → History | Per-file upload log with timestamps, size, category, speed |
| **Storage** | `/dashboard` → Storage | Per-category size bars |
| **Devices** | `/dashboard` → Devices | Per-IP device table (first seen, files, bytes) |
| **Errors** | `/dashboard` → Errors | Failed uploads log |
| **Remote Trigger** | `/dashboard` → Remote Trigger | File type selector, Fire Payload button, trigger log |
| **Gallery** | `/files` | Masonry photo/video grid, lightbox, category filter, search |
| **Report** | `/report` | JSON acquisition report |
| **Health** | `/health` | Server liveness check |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Termux doesn't open | In `.ino`, increase `APP_LOAD_MS` from 5000 to 8000 and re-upload |
| Keystrokes land in wrong window | Re-pair Bluetooth; ensure Android is unlocked, on home screen |
| "No media found" in Termux | Re-run `termux-setup-storage` and tap **Allow** |
| Auth error on upload | `UPLOAD_TOKEN` in `.env` must match what's stored in `~/.backup_token` |
| Server unreachable | Check both ngrok (Step 8) and server (Step 9) terminals are running |
| ngrok URL changed | Update `WAN_SERVER_URL` in `.ino` and re-upload |
| Windows: `python` not found | Re-install Python, tick **"Add Python to PATH"** |
| Linux: `chmod +x start.sh` | `chmod +x server/start.sh` |
| ESP32 not showing online | Check WiFi credentials in `.ino`, confirm ESP32 and server share the ngrok URL |
| Fire button queued but nothing fires | ESP32 must be powered and on WiFi — command waits until it connects |

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/upload` | Receive a file from Termux |
| `GET` | `/files` | Web gallery of all files |
| `GET` | `/health` | `{"status":"ok"}` |
| `GET` | `/report` | JSON acquisition report |
| `GET` | `/dashboard` | Web dashboard |
| `GET` | `/api/stats` | Live stats polled by dashboard (JSON) |
| `GET` | `/api/poll?ble=0\|1` | ESP32 heartbeat + command pick-up |
| `POST` | `/trigger` | Queue a payload command for the ESP32 |
| `GET/POST` | `/api/config` | Get/set active file extensions |
| `GET` | `/setup-token` | Termux token bootstrap (requires `X-Setup-Password` header) |
| `GET` | `/payloads/<file>` | Serve `media_backup.py` to Termux |

---

## References

- [ESP32 BLE Keyboard library](https://github.com/T-vK/ESP32-BLE-Keyboard) — T-vK
- [BadUSB / BLE HID attack research](https://www.hak5.org/blogs/usb-rubber-ducky) — Hak5
- [Termux](https://termux.dev) — Android terminal emulator
- [ngrok](https://ngrok.com) — HTTPS tunneling
- [Flask](https://flask.palletsprojects.com) — Python web framework

---

## License

MIT License — free to use, modify, and share for authorized purposes.

> This project is provided for educational and research purposes only.  
> The author is not responsible for any misuse.  
> Use only on devices you own or have explicit written authorization to test.
