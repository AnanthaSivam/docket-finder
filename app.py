#!/usr/bin/env python3
"""
Docket Finder — local web app
------------------------------
A small Flask server providing a drag-and-drop interface around
cause_list_finder.py. Run it with:

    python app.py

then open http://localhost:5000 in a browser. Everything runs locally —
no data leaves your machine.
"""

import json
import os
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, render_template

import cause_list_finder as clf

# When bundled into a standalone app (PyInstaller), the executable's own
# folder is a temporary, read-only extraction directory — so uploads and
# results need a real, writable, stable location instead. Running normally
# (`python app.py`), everything just lives next to this file as before.
FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    BASE_DIR = Path.home() / "DocketFinder"
    TEMPLATE_DIR = Path(sys._MEIPASS) / "templates"
else:
    BASE_DIR = Path(__file__).resolve().parent
    TEMPLATE_DIR = BASE_DIR / "templates"

BASE_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = BASE_DIR / "uploads"
RESULTS_DIR = BASE_DIR / "results"
UPLOAD_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)

app = Flask(__name__, template_folder=str(TEMPLATE_DIR))
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200MB, generous for a scanned cause list

# In-memory state — this app is meant for one person on their own machine,
# so a simple process-wide dict is enough (no database needed).
UPLOADS = {}   # upload_id -> {"path": ..., "filename": ..., "pages": int}
PARSED = {}    # upload_id -> {"blocks": [...], "num_pages": int}
JOBS = {}      # job_id -> {"status": "running"|"done"|"error", "page": int, "total": int, "result": {...}, "error": str}
EXTRAS = {}    # extra_id -> {"path": ..., "filename": ...} — supplementary case-list files (pdf table / csv)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Advocate list
# ---------------------------------------------------------------------------

@app.route("/api/advocates", methods=["GET"])
def get_advocates():
    return jsonify(clf.load_advocates())


@app.route("/api/advocates", methods=["POST"])
def add_advocate():
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    advocates = clf.load_advocates()
    if name not in advocates:
        advocates.append(name)
        clf.save_advocates(advocates)
    return jsonify(advocates)


@app.route("/api/advocates/remove", methods=["POST"])
def remove_advocate():
    name = (request.json or {}).get("name", "")
    advocates = clf.load_advocates()
    if name in advocates:
        advocates.remove(name)
        clf.save_advocates(advocates)
    return jsonify(advocates)


# ---------------------------------------------------------------------------
# Upload + parsing
# ---------------------------------------------------------------------------

def _get_parsed(upload_id):
    """Parse (once) and cache the blocks for an uploaded PDF."""
    if upload_id not in PARSED:
        path = UPLOADS[upload_id]["path"]
        blocks = clf.parse_pdf(path, progress=False)
        with clf.pdfplumber.open(path) as pdf:
            num_pages = len(pdf.pages)
        PARSED[upload_id] = {"blocks": blocks, "num_pages": num_pages}
    return PARSED[upload_id]


@app.route("/api/upload", methods=["POST"])
def upload():
    file = request.files.get("file")
    if not file or not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a PDF file"}), 400

    upload_id = uuid.uuid4().hex
    dest = UPLOAD_DIR / f"{upload_id}.pdf"
    file.save(dest)

    try:
        with clf.pdfplumber.open(dest) as pdf:
            pages = len(pdf.pages)
    except Exception as e:
        dest.unlink(missing_ok=True)
        return jsonify({"error": f"Could not read this PDF: {e}"}), 400

    UPLOADS[upload_id] = {"path": str(dest), "filename": file.filename, "pages": pages}
    return jsonify({"upload_id": upload_id, "filename": file.filename, "pages": pages})


@app.route("/api/suggest", methods=["POST"])
def suggest():
    data = request.json or {}
    upload_id = data.get("upload_id")
    query = (data.get("query") or "").strip()
    if upload_id not in UPLOADS:
        return jsonify({"error": "Upload not found — please re-upload the PDF"}), 404
    if not query:
        return jsonify({"error": "Enter a name to search for"}), 400

    parsed = _get_parsed(upload_id)
    candidates = clf.collect_advocate_candidates(parsed["blocks"], query)
    return jsonify([{"name": name, "count": count} for name, count in candidates])


# ---------------------------------------------------------------------------
# Supplementary "cases to add" files (simple table PDF or CSV) — merged
# into the report alongside the main cause list, skipping any case number
# already found there.
# ---------------------------------------------------------------------------

@app.route("/api/extras", methods=["GET"])
def list_extras():
    return jsonify([{"id": eid, "filename": e["filename"]} for eid, e in EXTRAS.items()])


@app.route("/api/extras", methods=["POST"])
def add_extra():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file provided"}), 400
    ext = Path(file.filename).suffix.lower()
    if ext not in (".pdf", ".csv"):
        return jsonify({"error": "Please upload a .pdf or .csv file"}), 400

    extra_id = uuid.uuid4().hex
    dest = UPLOAD_DIR / f"extra_{extra_id}{ext}"
    file.save(dest)

    try:
        entries = clf.parse_summary_file(str(dest))
    except Exception as e:
        dest.unlink(missing_ok=True)
        return jsonify({"error": f"Could not read this file: {e}"}), 400

    if not entries:
        dest.unlink(missing_ok=True)
        return jsonify({"error": "No case rows recognized in this file. Expected columns "
                                  "like Court Hall / Item / Case No. / Parties."}), 400

    EXTRAS[extra_id] = {"path": str(dest), "filename": file.filename}
    return jsonify({"id": extra_id, "filename": file.filename, "case_count": len(entries)})


@app.route("/api/extras/remove", methods=["POST"])
def remove_extra():
    extra_id = (request.json or {}).get("id")
    extra = EXTRAS.pop(extra_id, None)
    if extra:
        Path(extra["path"]).unlink(missing_ok=True)
    return jsonify([{"id": eid, "filename": e["filename"]} for eid, e in EXTRAS.items()])


# ---------------------------------------------------------------------------
# Report generation — runs in a background thread so the UI can show a
# live progress bar instead of one long blocking request.
# ---------------------------------------------------------------------------

def _run_job(job_id, upload_id):
    job = JOBS[job_id]
    try:
        path = UPLOADS[upload_id]["path"]
        filename = UPLOADS[upload_id]["filename"]

        def on_page(pnum, total):
            job["page"] = pnum
            job["total"] = total

        if upload_id in PARSED:
            blocks = PARSED[upload_id]["blocks"]
            num_pages = PARSED[upload_id]["num_pages"]
            job["page"] = num_pages
            job["total"] = num_pages
        else:
            blocks = clf.parse_pdf(path, progress=False, on_page=on_page)
            with clf.pdfplumber.open(path) as pdf:
                num_pages = len(pdf.pages)
            PARSED[upload_id] = {"blocks": blocks, "num_pages": num_pages}

        advocates = clf.load_advocates()
        tagged = clf.find_matches(blocks, advocates)

        extras_summary = []
        for extra_id, extra in EXTRAS.items():
            entries = clf.parse_summary_file(extra["path"])
            tagged, added, skipped = clf.merge_extra_entries(tagged, entries, blocks)
            extras_summary.append({"filename": extra["filename"], "read": len(entries),
                                    "added": added, "skipped": skipped})

        grouped = clf.group_by_court(tagged)

        stem = Path(filename).stem
        pdf_name = f"{job_id}_{stem}_results.pdf"
        csv_name = f"{job_id}_{stem}_results.csv"
        clf.build_pdf_report(str(RESULTS_DIR / pdf_name), filename, num_pages, blocks, grouped)
        clf.write_csv(str(RESULTS_DIR / csv_name), blocks, grouped)

        total_matches = sum(len(cases) for sections in grouped.values() for cases in sections.values())

        job["status"] = "done"
        job["result"] = {
            "pdf_file": pdf_name,
            "csv_file": csv_name,
            "pages": num_pages,
            "total_cases": len(blocks),
            "courts": len(grouped),
            "matches": total_matches,
            "extras_summary": extras_summary,
        }
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/api/run", methods=["POST"])
def run():
    data = request.json or {}
    upload_id = data.get("upload_id")
    if upload_id not in UPLOADS:
        return jsonify({"error": "Upload not found — please re-upload the PDF"}), 404

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "running", "page": 0, "total": UPLOADS[upload_id]["pages"]}
    thread = threading.Thread(target=_run_job, args=(job_id, upload_id), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify(job)


@app.route("/download/<path:filename>")
def download(filename):
    return send_from_directory(RESULTS_DIR, filename, as_attachment=True)


def _find_free_port(preferred=5050):
    """Try the preferred port first, then fall back to whatever the OS
    hands out. Port 5000 is deliberately avoided — macOS's AirPlay Receiver
    uses it by default, which is the most common reason this kind of app
    fails to start on a Mac."""
    import socket
    for candidate in (preferred, 8000, 8765, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", candidate))
                return s.getsockname()[1]
            except OSError:
                continue
    return preferred


if __name__ == "__main__":
    port = _find_free_port()
    url = f"http://localhost:{port}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"\n  Docket Finder is running at {url}\n  Press Ctrl+C to stop.\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
