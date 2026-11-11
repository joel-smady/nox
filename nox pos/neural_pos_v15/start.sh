#!/bin/bash
# Neural POS — Linux startup script (fixed)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "╔══════════════════════════════════════╗"
echo "║         NEURAL POS  STARTUP          ║"
echo "╚══════════════════════════════════════╝"

# ── Create required directories ──────────────────────────────────────────────
mkdir -p data/backups static/css static/js static/img/products logs
echo "✓ Directories ready"

# ── Remove corrupt phantom directories from bad ZIP extraction ────────────────
rm -rf "{static" 2>/dev/null || true
echo "✓ Cleaned phantom directories"

# ── Check Python version ──────────────────────────────────────────────────────
PYTHON=$(command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
    echo "✗ Python not found. Install with: sudo apt install python3"
    exit 1
fi
PY_VER=$($PYTHON --version 2>&1)
echo "✓ $PY_VER"

# ── System dependencies for hardware/USB printer support ─────────────────────
echo "→ Checking system dependencies..."
if command -v apt-get &>/dev/null; then
    dpkg -l libusb-1.0-0-dev &>/dev/null 2>&1 || \
        sudo apt-get install -y -q libusb-1.0-0-dev libusb-1.0-0 2>/dev/null || \
        echo "  ⚠ Could not install libusb-dev (USB printer support may be limited)"
fi
echo "✓ System dependencies checked"

# ── Virtual env: create if missing ───────────────────────────────────────────
if [ ! -d "venv" ]; then
    echo "→ Creating virtual environment..."
    $PYTHON -m venv venv
    echo "✓ venv created"
fi

# ── Activate venv ─────────────────────────────────────────────────────────────
# Use '.' instead of 'source' — compatible with bash AND dash/sh
. venv/bin/activate
echo "✓ venv activated"

# ── Install ALL dependencies ──────────────────────────────────────────────────
echo "→ Installing dependencies..."
pip install --quiet --upgrade pip

# Core (always required)
pip install --quiet \
    fastapi \
    "uvicorn[standard]" \
    pyjwt \
    bcrypt \
    httpx \
    python-multipart \
    jinja2 \
    aiofiles \
    python-dotenv

# Optional — isolated so one failure doesn't abort startup
echo "  → Receipt printer support..."
pip install --quiet "python-escpos>=3.0" pyserial pyusb 2>/dev/null || \
    echo "  ⚠ python-escpos skipped — thermal printer features unavailable"

echo "  → Image / barcode / PDF support..."
pip install --quiet Pillow python-barcode "qrcode[pil]" reportlab 2>/dev/null || \
    echo "  ⚠ Barcode/image packages skipped"

echo "  → Price intelligence (web scraping)..."
pip install --quiet beautifulsoup4 lxml 2>/dev/null || \
    echo "  ⚠ beautifulsoup4/lxml skipped — price intelligence unavailable"

echo "✓ Dependencies ready"

# ── Copy .env if missing ─────────────────────────────────────────────────────
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo "✓ .env created from .env.example"
fi

# ── Warn if JWT_SECRET is still the default shipped value ────────────────────
if grep -q "d8ff32b7d4b4344b53ea42c09775b9d299866f0e72b6e3ccc99e83ca976d5abd" .env 2>/dev/null; then
    echo ""
    echo "  ⚠ WARNING: JWT_SECRET is the default shipped value — change it!"
    echo "    Run: python3 -c \"import secrets; print(secrets.token_hex(32))\""
    echo "    Then paste the result into JWT_SECRET= in your .env file."
    echo ""
fi

# ── Start the server ──────────────────────────────────────────────────────────
HOST=${HOST:-0.0.0.0}
PORT=${PORT:-8000}
echo ""
echo "✓ Starting Neural POS on http://$HOST:$PORT"
echo "  Default login → admin / admin123"
echo "  ⚠ Change default passwords immediately after first login!"
echo "  Press Ctrl+C to stop"
echo ""

python3 main.py
