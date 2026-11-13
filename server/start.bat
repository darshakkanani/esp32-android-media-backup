@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM start.bat — Windows launcher for the Android Media Backup server
REM
REM Usage:  Double-click start.bat  OR  run from cmd / PowerShell:
REM             cd fetch_photos\server
REM             start.bat
REM
REM What it does:
REM   1. Checks Python is installed (looks for 'python' on PATH).
REM   2. Creates a virtual-env (.venv) if one does not already exist.
REM   3. Installs / upgrades dependencies from requirements.txt.
REM   4. Loads UPLOAD_TOKEN from .env if the file exists.
REM   5. Warns if UPLOAD_TOKEN is still unset.
REM   6. Starts server.py on port 8019.
REM ─────────────────────────────────────────────────────────────────────────────
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM ── 1. Python check ───────────────────────────────────────────────────────────
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found on PATH.
    echo.
    echo   Install Python 3.8+ from https://www.python.org/downloads/
    echo   Make sure to tick "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo [start.bat] %%v detected.

REM ── 2. Virtual-env ────────────────────────────────────────────────────────────
if not exist ".venv\" (
    echo [start.bat] Creating virtual environment ^(.venv^)...
    python -m venv .venv
)
call .venv\Scripts\activate.bat

REM ── 3. Dependencies ───────────────────────────────────────────────────────────
echo [start.bat] Installing / checking dependencies...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

REM ── 4. Load .env ──────────────────────────────────────────────────────────────
if exist ".env" (
    echo [start.bat] Loading .env...
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
        set "line=%%a"
        if not "!line:~0,1!" == "#" (
            if not "%%a" == "" (
                set "%%a=%%b"
            )
        )
    )
)

REM ── 5. Token warning ──────────────────────────────────────────────────────────
if not defined UPLOAD_TOKEN (
    echo.
    echo   [WARNING] UPLOAD_TOKEN is not set - server will accept uploads without auth.
    echo   To fix:
    echo     1. Generate a token:
    echo        python -c "import secrets; print(secrets.token_hex(32))"
    echo     2. Add to .env:
    echo        echo UPLOAD_TOKEN=your_token >> .env
    echo.
)

REM ── 6. Start ──────────────────────────────────────────────────────────────────
echo [start.bat] Starting server on port 8019...
echo   Open in browser: http://localhost:8019/files
echo.
python server.py
pause
