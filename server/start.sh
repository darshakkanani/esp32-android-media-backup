#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# start.sh — macOS / Linux launcher for the Android Media Backup server
#
# Usage:
#   chmod +x start.sh      # one-time
#   ./start.sh
#
# What it does:
#   1. Checks Python 3 is installed.
#   2. Creates a virtual-env (.venv) if one does not already exist.
#   3. Installs / upgrades dependencies from requirements.txt.
#   4. Loads UPLOAD_TOKEN from .env if the file exists.
#   5. Warns if UPLOAD_TOKEN is still unset.
#   6. Starts server.py on port 8019.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")"

# ── 1. Python check ───────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] python3 not found."
    echo "  macOS  : brew install python  OR  install from https://python.org"
    echo "  Ubuntu : sudo apt install python3 python3-venv python3-pip"
    echo "  Fedora : sudo dnf install python3"
    exit 1
fi

PY=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "[start.sh] Python $PY detected."

# ── 2. Virtual-env ────────────────────────────────────────────────────────────
if [ ! -d ".venv" ]; then
    echo "[start.sh] Creating virtual environment (.venv)..."
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# ── 3. Dependencies ───────────────────────────────────────────────────────────
echo "[start.sh] Installing / checking dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# ── 4. Load .env ──────────────────────────────────────────────────────────────
if [ -f ".env" ]; then
    echo "[start.sh] Loading .env..."
    # Export only lines of the form KEY=VALUE (skip comments and blanks)
    set -o allexport
    # shellcheck disable=SC2046
    source .env
    set +o allexport
fi

# ── 5. Token warning ──────────────────────────────────────────────────────────
if [ -z "${UPLOAD_TOKEN:-}" ]; then
    echo ""
    echo "  [WARNING] UPLOAD_TOKEN is not set — server will accept uploads without auth."
    echo "  To fix:"
    echo "    1. Generate a token:  python3 -c \"import secrets; print(secrets.token_hex(32))\""
    echo "    2. Add to .env:       echo 'UPLOAD_TOKEN=your_token' >> .env"
    echo ""
fi

# ── 6. Start ──────────────────────────────────────────────────────────────────
echo "[start.sh] Starting server on port 8019..."
echo "  Open in browser: http://localhost:8019/files"
echo ""
python3 server.py
