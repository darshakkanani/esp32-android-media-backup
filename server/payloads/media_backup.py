#!/usr/bin/env python3
"""
media_backup.py — Android Termux media backup client
=====================================================
Triggered by ESP32 BLE HID (GPIO 19 button press).

Usage:
    python3 ~/media_backup.py <SERVER_URL>

    SERVER_URL  : ngrok HTTPS URL of the macOS server (no trailing slash)
                  Example: https://abcd-1234.ngrok-free.app

One-time Termux setup:
    pkg install python
    termux-setup-storage     # tap Allow when Android prompts
    cp media_backup.py ~/media_backup.py

Security:
    Auth token is read from  ~/.backup_token  (one token per line, first line
    used).  Create it with:
        echo "YOUR_SECRET_TOKEN" > ~/.backup_token
        chmod 600 ~/.backup_token
    The same token must be set as UPLOAD_TOKEN in server.py.
    If the file is missing the script still runs but uploads will be rejected
    by an auth-enabled server.
"""

import os
import sys
import time
import json
import hashlib
import urllib.request
import urllib.error
from datetime import datetime

# ── Configuration ─────────────────────────────────────────────────────────────

SERVER_URL   = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8019"
UPLOAD_URL   = f"{SERVER_URL}/upload"
HEALTH_URL   = f"{SERVER_URL}/health"
TOKEN_FILE   = os.path.expanduser("~/.backup_token")
STATE_FILE   = os.path.expanduser("~/.media_backup_state.json")

# Media extensions to include
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif", ".bmp", ".tiff", ".tif", ".dng", ".raw"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".3gp", ".m4v", ".webm", ".ts"}
ALL_EXTS   = IMAGE_EXTS | VIDEO_EXTS

# Directories to scan on /sdcard (Android shared storage)
# Only directories that exist and are readable are scanned.
SEARCH_DIRS = [
    "/sdcard/DCIM/Camera",
    "/sdcard/DCIM",
    "/sdcard/Pictures",
    "/sdcard/Pictures/Screenshots",
    "/sdcard/Pictures/Instagram",
    "/sdcard/Pictures/WhatsApp Images",
    "/sdcard/WhatsApp/Media/WhatsApp Images",
    "/sdcard/WhatsApp/Media/WhatsApp Video",
    "/sdcard/Telegram",
    "/sdcard/Download",
    "/sdcard/Movies",
    "/sdcard/Snapchat",
    "/sdcard/Signal/Signal/Signal Images",
]

# Transfer settings
MAX_SIZE_MB    = 500     # skip individual files larger than this
RETRY_MAX      = 3       # retry attempts per file
RETRY_DELAY_S  = 3       # seconds between retries
INTER_FILE_S   = 0.2     # seconds between successful uploads
CONNECT_TIMEOUT= 10      # seconds for server health check
UPLOAD_TIMEOUT = 120     # seconds per file upload

# ── Auth token ────────────────────────────────────────────────────────────────

def load_token():
    """Read auth token from ~/.backup_token. Returns empty string if missing."""
    try:
        with open(TOKEN_FILE) as fh:
            token = fh.readline().strip()
        return token
    except FileNotFoundError:
        return ""

AUTH_TOKEN = load_token()

# ── State (incremental / duplicate detection) ─────────────────────────────────

def load_state():
    """Return dict of {sha256: filename} of previously uploaded files."""
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_state(state):
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)

# ── SHA-256 ───────────────────────────────────────────────────────────────────

def sha256_of(path):
    """Return hex SHA-256 of a file. Returns None on read error."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None

# ── File discovery ────────────────────────────────────────────────────────────

def collect_media():
    """
    Walk SEARCH_DIRS, deduplicate by real path, return sorted list of paths.
    Symlinks are resolved so the same physical file is never returned twice.
    """
    seen_real = set()
    found     = []

    for base in SEARCH_DIRS:
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            # Skip hidden directories (e.g. .thumbnails, .cache)
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in ALL_EXTS:
                    continue
                full = os.path.join(root, fname)
                try:
                    real = os.path.realpath(full)
                except OSError:
                    continue
                if real in seen_real:
                    continue
                seen_real.add(real)
                found.append(full)

    return sorted(found)

# ── Server health check ───────────────────────────────────────────────────────

def check_server():
    """Return True if server responds to GET /health."""
    try:
        req = urllib.request.Request(
            HEALTH_URL,
            headers={"ngrok-skip-browser-warning": "true"},
        )
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT) as r:
            data = json.loads(r.read())
        return data.get("status") == "ok"
    except Exception as exc:
        print(f"  [!] Server health check failed: {exc}")
        return False

# ── File upload ───────────────────────────────────────────────────────────────

def upload_file(path, sha256, category):
    """
    Upload a single file via multipart/form-data POST.

    Form fields sent:
        file      : the binary file
        sha256    : hex digest (server uses this for dedup + integrity check)
        category  : DCIM | Pictures | Screenshots | Movies | Downloads | Other
        mtime     : file modification time as ISO-8601 string

    Returns the parsed JSON response dict on success.
    Raises urllib.error.HTTPError or OSError on failure.
    """
    fname    = os.path.basename(path)
    boundary = "MediaBackupBoundary7f3a9c"

    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(path)).isoformat()
    except OSError:
        mtime = datetime.now().isoformat()

    def field(name, value):
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()

    with open(path, "rb") as fh:
        file_data = fh.read()

    file_part = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + file_data + b"\r\n"

    body = (
        field("sha256",   sha256)
        + field("category", category)
        + field("mtime",    mtime)
        + file_part
        + f"--{boundary}--\r\n".encode()
    )

    headers = {
        "Content-Type":               f"multipart/form-data; boundary={boundary}",
        "Content-Length":             str(len(body)),
        "ngrok-skip-browser-warning": "true",
    }
    if AUTH_TOKEN:
        headers["X-Backup-Token"] = AUTH_TOKEN

    req = urllib.request.Request(UPLOAD_URL, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT) as resp:
        return json.loads(resp.read())

# ── Category mapping ──────────────────────────────────────────────────────────

def category_for(path):
    p = path.lower()
    if "/dcim"        in p: return "DCIM"
    if "/screenshots" in p: return "Screenshots"
    if "/pictures"    in p: return "Pictures"
    if "/movies"      in p: return "Movies"
    if "/download"    in p: return "Downloads"
    return "Other"

# ── User selection UI ─────────────────────────────────────────────────────────

def select_files(all_files):
    """
    Show discovered media summary and ask the user what to upload.
    Returns the approved list of paths.
    """
    # Categorise
    cats = {}
    for f in all_files:
        c = category_for(f)
        cats.setdefault(c, []).append(f)

    total_size = sum(os.path.getsize(f) for f in all_files if os.path.exists(f))
    total_mb   = total_size / 1_048_576

    print("\n" + "─" * 56)
    print("  MEDIA DISCOVERED")
    print("─" * 56)
    for cat, files in sorted(cats.items()):
        cat_mb = sum(os.path.getsize(f) for f in files if os.path.exists(f)) / 1_048_576
        print(f"  [{cat}]  {len(files)} files  ({cat_mb:.1f} MB)")
    print(f"\n  Total : {len(all_files)} files  ({total_mb:.1f} MB)")
    print("─" * 56)

    print("\nOptions:")
    print("  all        — upload everything")
    print("  <category> — upload one category (e.g. DCIM, Screenshots)")
    print("  none       — cancel")
    print()

    try:
        choice = input("Your choice: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return []

    if choice == "none" or not choice:
        print("Backup cancelled by user.")
        return []
    if choice == "all":
        approved = all_files
    elif choice in {c.lower() for c in cats}:
        approved = []
        for cat, files in cats.items():
            if cat.lower() == choice:
                approved = files
                break
    else:
        print(f"Unknown choice '{choice}'. Backup cancelled.")
        return []

    # Final confirmation
    conf_size = sum(os.path.getsize(f) for f in approved if os.path.exists(f)) / 1_048_576
    print(f"\n  Selected: {len(approved)} files  ({conf_size:.1f} MB)")
    try:
        ok = input("Proceed? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return []
    if ok != "y":
        print("Backup cancelled.")
        return []

    return approved

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "═" * 56)
    print("  Android Media Backup Client")
    print(f"  Server  : {SERVER_URL}")
    print(f"  Auth    : {'token loaded' if AUTH_TOKEN else 'NO TOKEN (set ~/.backup_token)'}")
    print("═" * 56)

    # 1. Server reachability
    print("\n[1/6] Checking server …")
    if not check_server():
        print("  Server unreachable. Check ngrok and server.py are running.")
        sys.exit(1)
    print("  Server OK.")

    # 2. Discover media
    print("\n[2/6] Scanning media directories …")
    all_files = collect_media()
    if not all_files:
        print("  No media found. Did you run  termux-setup-storage ?")
        sys.exit(0)

    # 3. Load incremental state
    print("\n[3/6] Loading incremental state …")
    state        = load_state()          # {sha256: saved_filename}
    already_seen = set(state.keys())
    print(f"  {len(already_seen)} files previously uploaded (will skip duplicates).")

    # 4. User selection
    print("\n[4/6] User selection …")
    approved = select_files(all_files)
    if not approved:
        sys.exit(0)

    # 5. Upload loop
    print("\n[5/6] Uploading …\n")
    ok_count   = 0
    skip_dup   = 0
    skip_size  = 0
    fail_count = 0
    fail_list  = []

    total = len(approved)

    for idx, path in enumerate(approved, 1):
        fname   = os.path.basename(path)
        try:
            size_mb = os.path.getsize(path) / 1_048_576
        except OSError:
            print(f"  [{idx:>4}/{total}] SKIP  {fname}  (unreadable)")
            fail_count += 1
            continue

        # Size gate
        if size_mb > MAX_SIZE_MB:
            print(f"  [{idx:>4}/{total}] SKIP  {fname}  ({size_mb:.1f} MB > {MAX_SIZE_MB} MB limit)")
            skip_size += 1
            continue

        # SHA-256 (also used for duplicate detection)
        sha = sha256_of(path)
        if sha is None:
            print(f"  [{idx:>4}/{total}] SKIP  {fname}  (hash error)")
            fail_count += 1
            continue

        # Incremental: skip if already uploaded
        if sha in already_seen:
            print(f"  [{idx:>4}/{total}] DUP   {fname}  (already uploaded)")
            skip_dup += 1
            continue

        cat = category_for(path)

        # Upload with retry
        success  = False
        response = None
        for attempt in range(1, RETRY_MAX + 1):
            try:
                response = upload_file(path, sha, cat)
                success  = True
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode(errors="replace")
                if exc.code == 409:
                    # Server-side duplicate
                    print(f"  [{idx:>4}/{total}] DUP   {fname}  (server duplicate)")
                    state[sha] = fname
                    skip_dup += 1
                    success = True   # treat as success so we don't retry
                    break
                if exc.code == 401:
                    print(f"  [{idx:>4}/{total}] FAIL  {fname}  (auth error — check token)")
                    fail_count += 1
                    fail_list.append(fname)
                    break
                print(f"  [{idx:>4}/{total}] retry {attempt}/{RETRY_MAX}  HTTP {exc.code}: {body[:80]}")
            except Exception as exc:
                if attempt < RETRY_MAX:
                    print(f"  [{idx:>4}/{total}] retry {attempt}/{RETRY_MAX}  {fname}  ({exc})")
                    time.sleep(RETRY_DELAY_S)
                else:
                    print(f"  [{idx:>4}/{total}] FAIL  {fname}  ({exc})")

        if success and response and response.get("ok"):
            saved = response.get("saved", fname)
            print(f"  [{idx:>4}/{total}] OK    {fname}  ({size_mb:.1f} MB)  → {saved}")
            state[sha] = saved
            ok_count  += 1
            time.sleep(INTER_FILE_S)
        elif not success:
            fail_count += 1
            fail_list.append(fname)

        # Persist state after every file so a crash doesn't lose progress
        save_state(state)

    # 6. Summary
    print("\n[6/6] Summary")
    print("─" * 40)
    print(f"  Uploaded   : {ok_count}")
    print(f"  Duplicates : {skip_dup}")
    print(f"  Too large  : {skip_size}  (> {MAX_SIZE_MB} MB)")
    print(f"  Failed     : {fail_count}")
    if fail_list:
        print("  Failed files:")
        for f in fail_list:
            print(f"    • {f}")
    print(f"\n  Gallery : {SERVER_URL}/files")
    print(f"  Report  : {SERVER_URL}/report")
    print("─" * 40 + "\n")


if __name__ == "__main__":
    main()
