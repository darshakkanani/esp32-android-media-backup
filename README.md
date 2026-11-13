# 📱 ESP32 Android Media Backup

One button press on an ESP32 → all your Android photos and videos land on your computer.

The ESP32 pretends to be a Bluetooth keyboard, opens Termux on your Android phone, and types a script that uploads every media file to a Python server running on your computer — through an ngrok HTTPS tunnel.

**Works on macOS, Windows, and Linux.**

---

## What You Need

| Item | Notes |
|------|-------|
| ESP32 dev board | Any variant — WROOM-32, S2, S3, C3 |
| Momentary push button | Wired between **GPIO 19** and **GND** (no resistor needed) |
| Android phone | With **Termux** installed from [F-Droid](https://f-droid.org/packages/com.termux/) |
| Computer | macOS, Windows, or Linux |
| Free ngrok account | Sign up at [ngrok.com](https://ngrok.com) |

---

## How It Works

```
Press button (GPIO 19)
        │
        ▼
  ESP32 → Bluetooth keyboard → opens Termux on Android
                                        │
                               types upload script
                                        │
                               curl sends media files
                                        │
                               ngrok HTTPS tunnel
                                        │
                                        ▼
                             Python server on your PC
                             (macOS / Windows / Linux)
                             files saved + SHA-256 verified
```

---

## Full Setup — Step by Step

---

### PART 1 — Get the Code

**Step 1 — Install Git** (skip if already installed)

| OS | Command |
|----|---------|
| macOS | `brew install git` or install Xcode Command Line Tools |
| Windows | Download from [git-scm.com](https://git-scm.com/download/win) |
| Linux | `sudo apt install git` or `sudo dnf install git` |

**Step 2 — Clone the repository**

```bash
git clone https://github.com/darshakkanani/esp32-android-media-backup.git
cd esp32-android-media-backup
```

---

### PART 2 — Install Prerequisites

**Step 3 — Install Python 3**

| OS | How |
|----|-----|
| macOS | Comes pre-installed. Or: `brew install python` |
| Windows | Download from [python.org/downloads](https://python.org/downloads) — ⚠️ tick **"Add Python to PATH"** |
| Ubuntu/Debian | `sudo apt install python3 python3-venv python3-pip` |
| Fedora/RHEL | `sudo dnf install python3 python3-pip` |

**Step 4 — Install ngrok**

| OS | How |
|----|-----|
| macOS | `brew install ngrok/ngrok/ngrok` |
| Windows | Download `ngrok.exe` from [ngrok.com/download](https://ngrok.com/download), add its folder to PATH |
| Linux | Download from [ngrok.com/download](https://ngrok.com/download) or `sudo apt install ngrok` |

Then connect your ngrok account (one-time):
```bash
ngrok config add-authtoken YOUR_NGROK_TOKEN
```
Get your token from [dashboard.ngrok.com/get-started/your-authtoken](https://dashboard.ngrok.com/get-started/your-authtoken).

---

### PART 3 — Set Up the Server

**Step 5 — Go into the server folder**

```bash
cd esp32-android-media-backup/server
```

**Step 6 — Create a virtual environment and install dependencies**

macOS / Linux:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows (cmd):
```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Windows (PowerShell):
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> **Shortcut:** instead of Steps 6–8, just run `./start.sh` (macOS/Linux) or double-click `start.bat` (Windows). The launcher does everything automatically.

---

**Step 7 — Create a secret auth token**

```bash
# macOS / Linux
python3 -c "import secrets; print(secrets.token_hex(32))"

# Windows
python -c "import secrets; print(secrets.token_hex(32))"
```

Copy the printed string — you will need it in the next step and again on Android.

**Step 8 — Create your `.env` file**

macOS / Linux:
```bash
cp .env.example .env
# Open .env in any text editor and replace the placeholder with your token
```

Windows:
```bat
copy .env.example .env
notepad .env
```

The `.env` file should look like this:
```
UPLOAD_TOKEN=abc123def456...your_token_here
```

---

**Step 9 — Start the ngrok tunnel**

Open a **new terminal window** and run:
```bash
ngrok http 8019
```

You will see output like:
```
Forwarding  https://abcd-1234.ngrok-free.app -> http://localhost:8019
```

**Copy that `https://...ngrok-free.app` URL** — you need it in Steps 11 and 13.

---

**Step 10 — Start the server**

In a **second terminal window**:

macOS / Linux:
```bash
cd esp32-android-media-backup/server
source .venv/bin/activate
python3 server.py
```

Windows:
```bat
cd esp32-android-media-backup\server
.venv\Scripts\activate
python server.py
```

The server prints the ngrok URL and confirms it is running. Keep this window open.

---

### PART 4 — Flash the ESP32

**Step 11 — Set up Arduino IDE**

1. Download and install [Arduino IDE 2](https://www.arduino.cc/en/software).
2. In Arduino IDE → **File → Preferences → Additional Board URLs**, add:
   ```
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
   ```
3. Open **Tools → Board → Board Manager**, search `esp32`, install **"esp32 by Espressif Systems"**.
4. Open **Tools → Manage Libraries**, search **"ESP32 BLE Keyboard"**, install it (by T-vK).

**Step 12 — Open and configure the sketch**

1. Open `fetch_photos.ino` in Arduino IDE.
2. On **line 32**, replace the URL placeholder with your ngrok URL from Step 9:
   ```cpp
   #define WAN_SERVER_URL "https://abcd-1234.ngrok-free.app"
   ```

**Step 13 — Upload to ESP32**

1. Connect your ESP32 via USB.
2. In Arduino IDE → **Tools → Board** → select your ESP32 model.
3. **Tools → Port** → select the COM port (Windows) or `/dev/cu.usbserial-...` (macOS/Linux).
4. Click **Upload** (▶ button).

> ⚠️ Every time ngrok restarts it gives a new URL. Update line 32 and re-upload the sketch whenever this happens.

---

### PART 5 — Set Up Android (one-time)

**Step 14 — Install Termux**

Install from **F-Droid only** — [f-droid.org/packages/com.termux](https://f-droid.org/packages/com.termux/)  
The Play Store version is outdated and lacks storage permissions.

**Step 15 — Set up Termux**

Open Termux on your Android phone and run:
```bash
pkg update && pkg upgrade -y
pkg install python curl
termux-setup-storage
```
When Android asks for storage permission, tap **Allow**.

**Step 16 — Download the backup script to Android**

In Termux, run (replace with your ngrok URL):
```bash
curl -s -L -H "ngrok-skip-browser-warning: true" \
     -o ~/media_backup.py \
     https://abcd-1234.ngrok-free.app/payloads/media_backup.py
```

**Step 17 — Set the auth token on Android**

```bash
echo "your_token_here" > ~/.backup_token
chmod 600 ~/.backup_token
```

Use the **same token** you put in `.env` on your computer (Step 8).

**Step 18 — Test it manually**

```bash
python3 ~/media_backup.py https://abcd-1234.ngrok-free.app
```

You should see your media discovered by category. Type `all` and `y` to test an upload.

---

### PART 6 — Wire the Button and Run

**Step 19 — Wire the button**

```
ESP32 GPIO 19  ──────[Button]────── GND
```

No resistor needed — the firmware uses the ESP32's internal pull-up.

**Step 20 — Pair Bluetooth**

1. On Android, go to **Settings → Bluetooth**.
2. The ESP32 appears as **"ESP32 Keyboard"** — pair it.

**Step 21 — Run a backup**

1. Make sure the server (Step 10) and ngrok (Step 9) are running on your computer.
2. Make sure Android is unlocked and Bluetooth is connected.
3. **Press the button** on GPIO 19.
4. The ESP32 automatically opens Termux and types the upload script.
5. Termux shows the media it found. Type `all` and press Enter, then `y` to confirm.
6. Files upload. `====== ALL DONE ======` appears when finished.

---

## View Your Files

Open any of these in a browser while the server is running:

| URL | What you see |
|-----|-------------|
| `http://localhost:8019/files` | Photo and video gallery, grouped by category |
| `http://localhost:8019/report` | Counts, sizes, and last upload time per category |
| `http://localhost:8019/health` | Quick server status check |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| ESP32 opens the wrong app | Edit `.ino`: change `openApp("Termux", 3000)` → `openApp("Termux", 5000)` and re-upload |
| Termux types in the wrong window | Re-pair Bluetooth; make sure Android is unlocked |
| "No media found" in Termux | Re-run `termux-setup-storage` and tap **Allow** |
| "Auth error" | The token in `~/.backup_token` on Android must exactly match `UPLOAD_TOKEN` in `.env` |
| Server not reachable | Check ngrok window (Step 9) and server window (Step 10) are both still running |
| ngrok URL changed | Update line 32 in `fetch_photos.ino` and re-upload |
| Windows: `python` not found | Re-install Python and tick **"Add Python to PATH"** |
| Linux: `permission denied` on start.sh | Run `chmod +x start.sh` once |

---

## Project Files

```
esp32-android-media-backup/
├── fetch_photos.ino          ← ESP32 firmware (flash this)
└── server/
    ├── server.py             ← Python server
    ├── start.sh              ← One-click start for macOS/Linux
    ├── start.bat             ← One-click start for Windows
    ├── requirements.txt      ← Python dependencies (just Flask)
    ├── .env.example          ← Copy to .env, add your token
    └── payloads/
        └── media_backup.py   ← Script that runs on Android in Termux
```

---

## License

MIT — free to use, modify, and share.
