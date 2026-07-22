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

**For Mac — build it yourself, right now:**
```bash
chmod +x build-mac.sh
./build-mac.sh
```
This needs Python once, to *build* the app (not to run it afterward). A
couple of minutes later, you'll have `dist/Docket Finder.app` — copy that
anywhere, share it with anyone on a Mac, and it just double-clicks and
runs. No Python, no terminal, no pip install for whoever uses it.

**For Windows — no Windows machine required, via a free cloud build:**
PyInstaller has to build on the same OS it targets, so I can't hand you a
`.exe` directly and you can't build one on a Mac. The practical workaround
is GitHub Actions, which lets you build on a real (free, temporary)
Windows machine in the cloud:

1. Create a free GitHub account if you don't have one, and create a new
   repository.
2. Push this folder's contents to it (the `.github/workflows/build-windows.yml`
   file is already included).
3. Open the repo's **Actions** tab — a build starts automatically (or
   click "Run workflow" to trigger it manually).
4. When it finishes (~2–3 minutes), download the **DocketFinder-Windows**
   artifact from that run — it's the ready-to-run `Docket Finder.exe`
   folder. Share that with anyone on Windows; they just double-click
   `Docket Finder.exe`, no installation required.

You only need to do this once; after that, the built app can be reused and
shared freely.

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
