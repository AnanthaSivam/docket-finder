#!/bin/bash
# Double-click this file (or run: ./start-mac.command) to launch Docket Finder.
cd "$(dirname "$0")"

if ! command -v python3 &> /dev/null; then
    echo "Python 3 isn't installed. Install it from https://www.python.org/downloads/ and try again."
    read -p "Press Enter to close..."
    exit 1
fi

echo "Checking dependencies (first run only, may take a minute)..."
python3 -m pip install --quiet -r requirements.txt

echo "Starting Docket Finder..."
python3 app.py
