# Docket Finder — Web App

A local, drag-and-drop web app for scanning court cause-list PDFs and
generating a compact case report for your firm's tracked advocates.
Everything runs on your own machine — no data is uploaded anywhere else.

## Two ways to run this

**Option A — quick start (needs Python installed once).** Good for
yourself or anyone comfortable installing one thing.

**Option B — a real double-click app, no Python needed at all.** Best for
handing this to someone non-technical. Takes one extra one-time step
(below) to produce, then works exactly like any other app.

---

### Option A: Quick start

Requires Python 3.9+ ([download here](https://www.python.org/downloads/) — on Windows, check "Add python.exe to PATH" during install).

**Mac:** double-click `start-mac.command`. (First time only: right-click it
and choose "Open" instead of double-clicking — Mac blocks scripts from
unidentified developers on the first launch. After that, double-click
works normally.)

**Windows:** double-click `start-windows.bat`.

Either way, it installs the couple of required packages automatically the
first time, then opens `http://localhost:5050` (or the next free port) in
your browser. Leave the black terminal window open while you use it; close
it when you're done.

Or from a terminal:
```bash
pip install -r requirements.txt
python app.py
```

### Option B: A real standalone app (recommended for non-technical users)

#### Building the Mac `.app`, step by step

1. Open **Terminal** (Applications → Utilities → Terminal).
2. Navigate into this folder, e.g.:
   ```bash
   cd ~/Downloads/webapp
   ```
3. Make the build script runnable (one-time):
   ```bash
   chmod +x build-mac.sh
   ```
4. Run it:
   ```bash
   ./build-mac.sh
   ```
   This installs Python's build tool (PyInstaller) if you don't have it,
   builds the icon from `assets/logo.png` automatically, then bundles
   everything. Takes a couple of minutes.
5. When it finishes, look inside the new `dist` folder — you'll find
   **`Docket Finder.app`**. Drag that anywhere you like (Applications
   folder, Desktop, a USB stick to give to someone else).
6. Double-click to run it. The very first time, macOS will refuse and say
   it's from an "unidentified developer" — right-click the app instead and
   choose **Open**, then confirm. After that first time, plain
   double-click works normally.

That's it — whoever you give `Docket Finder.app` to needs nothing else
installed; Python and all dependencies are bundled inside it.

#### Building the Windows `.exe`, step by step

PyInstaller has to build on the same OS it targets, so a `.exe` can't be
produced on a Mac. The practical workaround — and a genuinely standard
one — is to let GitHub build it for you on a free, temporary Windows
machine in the cloud:

1. Go to [github.com](https://github.com) and sign up for a free account
   if you don't already have one.
2. Click **New repository** (the `+` icon, top right). Give it any name
   (e.g. `docket-finder`), leave it **Private** if you'd rather, and
   create it.
3. Upload this whole `webapp` folder's contents to that repository. The
   easiest way if you're not familiar with git: on the repo's page, click
   **"uploading an existing file"** and drag in everything (make sure
   `.github/workflows/build-windows.yml` comes along — some drag-and-drop
   uploaders hide dot-folders, so if it doesn't show up, use the git
   command line instead: `git init`, `git add .`, `git commit -m "first"`,
   then follow GitHub's on-screen push instructions).
4. Open the **Actions** tab at the top of the repository page. A build
   should already be running (triggered automatically by the upload); if
   not, click **"Build Windows executable"** on the left, then **"Run
   workflow"**.
5. Wait for the green checkmark (roughly 2–3 minutes).
6. Click into that finished run, scroll to **Artifacts**, and download
   **DocketFinder-Windows** — it's a zip containing the ready-to-run
   `Docket Finder.exe` and its supporting files.
7. Unzip it and share the whole unzipped folder with anyone on Windows —
   they double-click `Docket Finder.exe` inside it. No Python, no
   installation.

You only need to do steps 1–6 once. After that, the built app can be
reused and shared freely; re-run the workflow only if you change the code.

---

## Adding your own logo

The app ships with a placeholder navy-and-brass "DF" monogram
(`assets/logo.png` and `static/logo.png` — these are the same image; one
copy feeds the built app's icon, the other feeds the web page). To use
your own:

1. Get a square image, ideally **at least 1024×1024px**, PNG with a
   transparent or solid background (a firm crest, initials, a simple
   icon — anything square works).
2. Replace **both** `assets/logo.png` and `static/logo.png` with it (same
   filename, so nothing else needs to change).
3. Re-run the relevant build:
   - **Web page logo**: just restart the app (`start-mac.command` /
     `start-windows.bat` / `python app.py`) — it picks up the new
     `static/logo.png` immediately.
   - **Mac app icon**: re-run `./build-mac.sh` — it regenerates
     `assets/icon.icns` from your new `assets/logo.png` automatically.
   - **Windows exe icon**: the workflow already converts `assets/logo.png`
     the same way, via `assets/icon.ico` — regenerate that file locally
     first if you're not using Pillow in the workflow (see note below),
     or simply replace `assets/icon.ico` with your own `.ico` export from
     any online PNG-to-ICO converter.

If you don't have Pillow handy to regenerate `assets/icon.ico` yourself,
the quickest path is any free online "PNG to ICO" converter — export
your logo at 256×256 and save it as `assets/icon.ico`.

---

## Using it

1. **Track your advocates** (left panel) — type an exact name and click
   Add, or upload a PDF first and use **"Not sure of the exact spelling?
   Search the uploaded PDF"** to find the precise spelling(s) that appear
   in that document and add them with one click. The list is saved on
   your machine and reused every time you open the app.
2. **Drop a cause list PDF** onto the upload area (or click to browse).
3. Click **Generate report** — a progress bar tracks parsing page by page
   (large 800+ page cause lists take roughly a minute).
4. When it finishes, download the **PDF** (a compact, two-column,
   worksheet-style report — court code, `Adm`/`Pet`/`Hg` fractions, then
   each matching case) or the **CSV** (full underlying data: page numbers,
   matched advocate names, full stage names).

## What's under the hood

- `app.py` — the Flask server: handles uploads, runs parsing in a
  background thread (so the progress bar works), and serves results.
  Automatically picks a free port (avoiding 5000, which macOS's AirPlay
  Receiver commonly occupies) and adapts its file paths when bundled by
  PyInstaller.
- `cause_list_finder.py` — the actual parsing/report logic. This is the
  same engine as the command-line version; see its own comments for how
  column detection, section-stage bucketing (Adm/Pet/Hg), sub-case
  collapsing, and exact-phrase advocate matching work.
- `templates/index.html` — the single-page UI.
- `build-mac.sh` / `.github/workflows/build-windows.yml` — one-time build
  scripts for producing standalone apps (Option B above).
- `uploads/` and `results/` — working directories created automatically
  (in `~/DocketFinder/` when running as a built app, or right here when
  running from source); safe to delete their contents between sessions.

## Notes on running this longer-term

This uses Flask's built-in development server, which is fine for personal,
single-user local use (which is what this is built for) but explicitly
not meant to be exposed to the internet or used with multiple simultaneous
users. If you ever want to share this with a few colleagues on the same
office network, that's a different setup (a production server like
gunicorn, plus some access control) — ask if you'd like help with that.
