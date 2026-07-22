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

ICON_ARGS=()
if [ -f "assets/logo.png" ]; then
    echo "Building app icon from assets/logo.png..."
    ICONSET="assets/icon.iconset"
    rm -rf "$ICONSET" && mkdir -p "$ICONSET"
    for size in 16 32 64 128 256 512; do
        sips -z $size $size assets/logo.png --out "$ICONSET/icon_${size}x${size}.png" > /dev/null
        double=$((size*2))
        sips -z $double $double assets/logo.png --out "$ICONSET/icon_${size}x${size}@2x.png" > /dev/null
    done
    iconutil -c icns "$ICONSET" -o assets/icon.icns
    rm -rf "$ICONSET"
    ICON_ARGS=(--icon "assets/icon.icns")
fi

echo "Building Docket Finder.app (this takes a couple of minutes)..."
python3 -m PyInstaller \
    --name "Docket Finder" \
    --windowed \
    --onedir \
    --add-data "templates:templates" \
    --add-data "static:static" \
    "${ICON_ARGS[@]}" \
    --collect-all pdfplumber \
    --collect-all reportlab \
    --noconfirm \
    app.py

echo ""
echo "Done. Find it at: dist/Docket Finder.app"
echo "Double-click it to run — the first time, right-click and choose"
echo "\"Open\" instead, since it isn't from a registered Apple developer."
