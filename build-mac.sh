#!/bin/bash
# Run this ONCE on a Mac to build a standalone "Docket Finder.app" that
# anyone can double-click — no Python, no pip, no terminal needed after this.
#
# Usage:
#   chmod +x build-mac.sh
#   ./build-mac.sh
#
# The finished app appears in dist/Docket Finder.app — copy that file
# anywhere (another Mac, a USB drive, etc.) and it just runs.

set -e
cd "$(dirname "$0")"

if ! command -v python3 &> /dev/null; then
    echo "Python 3 is required to build the app (not to run it afterward)."
    echo "Install it from https://www.python.org/downloads/ and try again."
    exit 1
fi

echo "Installing build tools and dependencies..."
python3 -m pip install --quiet -r requirements.txt pyinstaller

echo "Building Docket Finder.app (this takes a couple of minutes)..."
python3 -m PyInstaller \
    --name "Docket Finder" \
    --windowed \
    --onedir \
    --add-data "templates:templates" \
    --collect-all pdfplumber \
    --collect-all reportlab \
    --noconfirm \
    app.py

echo ""
echo "Done. Find it at: dist/Docket Finder.app"
echo "Double-click it to run — the first time, right-click and choose"
echo "\"Open\" instead, since it isn't from a registered Apple developer."
