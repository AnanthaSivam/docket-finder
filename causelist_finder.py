#!/usr/bin/env python3
"""
Cause List Finder
------------------
A local command-line tool that scans a court cause-list PDF and pulls out
every case involving a set of tracked advocates, then writes a clean,
printable PDF report.

Usage:
    # Manage your firm's advocate list (saved locally, reused every run)
    python cause_list_finder.py add "P. Martin Jose"
    python cause_list_finder.py remove "P. Martin Jose"
    python cause_list_finder.py list

    # Run against a cause list PDF
    python cause_list_finder.py run causelist.pdf
    python cause_list_finder.py run causelist.pdf --out results.pdf --csv results.csv
    python cause_list_finder.py run causelist.pdf --advocates "Tom Pious,Sunil V Mohammed"
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pdfplumber
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, BaseDocTemplate, PageTemplate, Frame,
    Paragraph, Spacer, Table, TableStyle, KeepTogether, HRFlowable
)

CONFIG_DIR = Path.home() / ".docket_finder"
CONFIG_FILE = CONFIG_DIR / "advocates.json"

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

SR_NO_RE = re.compile(r'^\d+(?:\.\d+)?$')
CASE_NUMBER_YEAR_RE = re.compile(r'/\s*\d{4}\s*$')
COURT_RE = re.compile(r"COURT NO\.?\s*[0-9A-Za-z]+\s*-\s*\d+", re.IGNORECASE)
CJ_COURT_RE = re.compile(r"CJ'S COURT(?:\s+\S+){0,8}", re.IGNORECASE)
PAGE_FOOTER_RE = re.compile(r'^\d+/\d+$')
NAME_STRIP_RE = re.compile(r'\b(SHRI|SRI|SMT|ADV|MR|MS|DR)\.?\b')
NON_ALPHA_RE = re.compile(r'[^A-Z\s]')
MULTI_SPACE_RE = re.compile(r'\s+')
CASE_NO_YEAR_RE = re.compile(r'([\d,\s]+)/\s*(\d{4})')
# Column boundaries (x0 in points) for: Sr.No | Case Number | Main Parties | Advocates
# Derived from the header row layout of the source document; holds across all pages
# since the whole cause list is generated from one template.
COLUMNS = [(0, 85), (85, 225), (225, 345), (345, 999)]

# Section-marker rows (e.g. "FOR ORDERS", "FRESH ADMISSION", "ADMISSION",
# "FOR HEARING", "PREVENTIVE DETENTION MATTERS FOR ADMISSION") appear as
# standalone, digit-free, all-caps lines with no serial number, dividing a
# court's list into stages. We detect them by keyword rather than an exact
# phrase list, since courts vary the wording.
SECTION_FULLMATCH_RE = re.compile(
    r'^('
    r'FOR (ORDERS?|HEARING|CLARIFICATION|MENTIONING|DIRECTIONS?)'
    r'|(FRESH )?ADMISSION(\s*\(\s*FRESH\s*\))?'
    r'|(REGULAR|ANTICIPATORY|ANICIPATORY) BAIL FOR ADMISSION(\s*\(\s*FRESH\s*\))?'
    r'|PREVENTIVE DETENTION MATTERS FOR ADMISSION'
    r'|CONTEMPT OF COURT CASES\s*\(\s*(FOR|FRESH) ADMISSION\s*\)'
    r'|(FRESH )?PETITIONS?'
    r'|URGENT PETITION'
    r'|PETITION IN DISPOSED CASE'
    r')$', re.IGNORECASE)


def _is_section_marker(c1, c2, c3, c4):
    if c1 or c4:
        return False
    # Genuine section headers (FOR ORDERS / ADMISSION / FRESH ADMISSION /
    # FOR HEARING / PREVENTIVE DETENTION MATTERS FOR ADMISSION / ...) only
    # ever appear in the case-number + main-parties columns, never in the
    # advocate column — and their full text matches a known, curated set of
    # phrases. Matching a whole phrase (not just a loose keyword) avoids
    # false positives from remark text that merely mentions "ORDER" etc.
    combined = MULTI_SPACE_RE.sub(' ', " ".join(x for x in (c2, c3) if x)).strip()
    if not combined:
        return False
    return bool(SECTION_FULLMATCH_RE.match(combined))



def normalize(s: str) -> str:
    s = s.upper()
    s = NAME_STRIP_RE.sub(' ', s)
    s = NON_ALPHA_RE.sub(' ', s)
    s = MULTI_SPACE_RE.sub(' ', s).strip()
    return s


def is_match(block_text: str, name: str) -> bool:
    """Exact-phrase match: the tracked name (normalized) must appear as a
    contiguous phrase inside the block's advocate text — not just have its
    words scattered anywhere in it. This is intentionally strict; use the
    `suggest` command to pick the precise spelling(s) that actually appear
    in the document before tracking a name."""
    nb = normalize(block_text)
    nn = normalize(name)
    if not nn:
        return False
    return nn in nb


def _group_rows(words, tol=2.0):
    """Group words on a page into visual rows by their y (top) position."""
    rows = []
    for w in words:
        y = w["top"]
        match_row = None
        for r in rows:
            if abs(r["y"] - y) < tol:
                match_row = r
                break
        if match_row is None:
            match_row = {"y": y, "words": []}
            rows.append(match_row)
        match_row["words"].append(w)
    rows.sort(key=lambda r: r["y"])
    return rows


def _bucket_row(row):
    """Split a row's words into the 4 layout columns based on x position.
    Uses each word's horizontal center (not its left edge) so a word that
    starts just before a column boundary but mostly sits past it — which
    happens on narrower 'tagged/WITH' sub-case rows — lands in the right
    column instead of being split from its neighbor."""
    cols = ["", "", "", ""]
    for w in sorted(row["words"], key=lambda w: w["x0"]):
        center = (w["x0"] + w["x1"]) / 2
        for i, (lo, hi) in enumerate(COLUMNS):
            if lo <= center < hi:
                cols[i] = (cols[i] + " " + w["text"]).strip()
                break
    return cols


FOOTER_SEP_RE = re.compile(r'^[X=\s]{6,}$')


def _is_footer_separator(c1, c2, c3, c4):
    """Detect the 'X==========X========X=========X' rule that marks the end
    of a court's list, right before a paper-size/formatting notice that
    isn't part of any case and must never be attached to the last case."""
    if c1:
        return False
    combined = "".join(x for x in (c2, c3, c4) if x)
    return bool(combined) and 'X' in combined and bool(FOOTER_SEP_RE.match(combined.replace(' ', '')))


def _match_court_header(c1, c2, c3, c4):
    """If this row IS the court header line itself (e.g. 'COURT NO. 3B - 5083'
    or the one-line 'CJ'S COURT Court No. 1- 5051 For Tuesday...' repeated at
    the top of continuation pages), return its normalized label. Checking
    per-row (not per-page) lets us fully exclude the header row from case
    content, which matters when a case's advocate list continues onto a new
    page right below that repeated header."""
    if c1:
        return None
    combined = MULTI_SPACE_RE.sub(' ', " ".join(x for x in (c2, c3, c4) if x)).strip()
    if not combined:
        return None
    m = COURT_RE.search(combined)
    if not m:
        m = CJ_COURT_RE.search(combined)
        if m and not re.search(r'\d', m.group(0)):
            # A bare "CJ'S COURT CAUSELIST" banner carries no room number —
            # it's not a useful court identifier, and must not overwrite the
            # specific "COURT NO. X - NNNN" label already captured for this page.
            return None
    if not m:
        return None
    label = m.group(0).strip().upper()
    label = label.replace("COURT NO ", "COURT NO. ")
    label = re.sub(r'\s*-\s*', ' - ', label)
    return label


def parse_pdf(pdf_path: str, progress=True, on_page=None):
    """Returns a list of case blocks:
    {page, sr_no, case_number, main_parties, court, section, advocate_text}
    on_page(pnum, total), if given, is called after every page — used by
    the web UI to drive a progress bar without depending on stderr output.
    """
    blocks = []
    current_court = "Unspecified Court"
    current_section = "UNSPECIFIED"
    cur = None  # accumulator for the case block currently being built; persists
                # across page boundaries since a case's advocate list can
                # continue onto the next page.

    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        for pnum, page in enumerate(pdf.pages, start=1):
            words = page.extract_words()
            if not words:
                continue

            rows = [_bucket_row(r) for r in _group_rows(words)]

            for c1, c2, c3, c4 in rows:
                court_label = _match_court_header(c1, c2, c3, c4)
                if court_label:
                    if court_label != current_court:
                        if cur:
                            blocks.append(cur)
                            cur = None
                        current_court = court_label
                        current_section = "UNSPECIFIED"
                    continue

                if _is_footer_separator(c1, c2, c3, c4):
                    if cur:
                        blocks.append(cur)
                        cur = None
                    continue

                if _is_section_marker(c1, c2, c3, c4):
                    if cur:
                        blocks.append(cur)
                        cur = None
                    current_section = MULTI_SPACE_RE.sub(' ', " ".join(x for x in (c2, c3) if x)).strip().upper()
                    continue

                if SR_NO_RE.match(c1):
                    if cur:
                        blocks.append(cur)
                    cur = {
                        "page": pnum,
                        "sr_no": c1,
                        "case_number": c2,
                        "court": current_court,
                        "section": current_section,
                        "source": "main",
                        "_petitioner": [],
                        "_respondent": [],
                        "_seen_vs": False,
                        "_advocate_parts": [],
                    }
                    if c3 and not PAGE_FOOTER_RE.match(c3):
                        cur["_petitioner"].append(c3.rstrip(","))
                    if c4:
                        cur["_advocate_parts"].append(c4)
                    continue

                if cur is None:
                    continue

                if c2 and not CASE_NUMBER_YEAR_RE.search(cur["case_number"]):
                    m = re.match(r'^(\d{4})\b', c2.strip())
                    if m:
                        cur["case_number"] = f"{cur['case_number']} {m.group(1)}".strip()

                if c3:
                    if c3.strip() == "Vs":
                        cur["_seen_vs"] = True
                    elif not PAGE_FOOTER_RE.match(c3.strip()):
                        target = cur["_respondent"] if cur["_seen_vs"] else cur["_petitioner"]
                        target.append(c3.strip().rstrip(","))
                if c4:
                    cur["_advocate_parts"].append(c4)

            if progress and (pnum % 100 == 0 or pnum == total):
                print(f"  parsed page {pnum}/{total}", file=sys.stderr)
            if on_page:
                on_page(pnum, total)

        if cur:
            blocks.append(cur)

    for b in blocks:
        petitioner = " ".join(b.pop("_petitioner")).strip()
        respondent = " ".join(b.pop("_respondent")).strip()
        b.pop("_seen_vs", None)
        b["main_parties"] = f"{petitioner} Vs {respondent}" if respondent else petitioner
        b["advocate_lines"] = list(b["_advocate_parts"])
        b["advocate_text"] = " ".join(b.pop("_advocate_parts"))

    _tag_section_ranges(blocks)
    return blocks


def section_bucket(section: str) -> str:
    """Collapse every raw stage label the document prints down to exactly
    three stages a court's list moves through, in order: admission matters,
    then petitions, then hearing. There is no fourth bucket — anything that
    isn't clearly a petitions or hearing stage (FOR ORDERS, FOR DIRECTIONS,
    FOR CLARIFICATION, FOR MENTIONING, an unlabeled run at the very top of
    the list, ...) defaults to ADM, since in practice these are small early
    stages that come before or alongside the admission list."""
    s = section.upper()

    if "HEARING" in s:
        return "HG"

    if "PETITIONS" in s or "PETN" in s or "PETITION" in s:
        return "PET"

    # Everything else — ADMISSION, FRESH ADMISSION, BAIL, HABEAS CORPUS,
    # PREVENTIVE DETENTION, CONTEMPT OF COURT CASES, FOR ORDERS, FOR
    # DIRECTIONS, FOR CLARIFICATION, FOR MENTIONING, UNSPECIFIED, etc.
    return "ADM"


BUCKET_DISPLAY = {"ADM": "Adm", "PET": "Pet", "HG": "Hg"}


def _tag_section_ranges(blocks):
    JUMP_THRESHOLD = 150
    runs = []
    current_key = None
    for idx, b in enumerate(blocks):
        bucket = section_bucket(b["section"])
        effective_label = BUCKET_DISPLAY.get(bucket, b["section"])
        key = (b["court"], effective_label)
        try:
            sr_int = int(str(b["sr_no"]).split(".")[0])
        except ValueError:
            sr_int = None

        big_jump = (
            key == current_key and sr_int is not None and runs and
            runs[-1]["end"] is not None and abs(sr_int - runs[-1]["end"]) > JUMP_THRESHOLD
        )

        if key != current_key or big_jump:
            runs.append({"court": b["court"], "label": effective_label,
                         "start": sr_int, "end": sr_int, "block_indices": [idx]})
            current_key = key
        else:
            run = runs[-1]
            run["block_indices"].append(idx)
            if sr_int is not None:
                if run["start"] is None:
                    run["start"] = sr_int
                run["end"] = sr_int if run["end"] is None else max(run["end"], sr_int)

    # Collapse noise: 4+ consecutive tiny (<=1-span) runs under the same
    # (court, label) key are pagination/serial-extraction artifacts, not
    # real distinct stages -- merge into one range covering their full span.
    merged = []
    i = 0
    while i < len(runs):
        j = i
        key = (runs[i]["court"], runs[i]["label"])
        while (j < len(runs) and (runs[j]["court"], runs[j]["label"]) == key and
               (runs[j]["start"] is None or runs[j]["end"] is None or
                runs[j]["end"] - runs[j]["start"] <= 1)):
            j += 1
        if j - i >= 4:
            starts = [r["start"] for r in runs[i:j] if r["start"] is not None]
            ends = [r["end"] for r in runs[i:j] if r["end"] is not None]
            merged.append({
                "court": key[0], "label": key[1],
                "start": min(starts) if starts else None,
                "end": max(ends) if ends else None,
                "block_indices": [idx for r in runs[i:j] for idx in r["block_indices"]],
            })
            i = j
        else:
            merged.append(runs[i])
            i += 1
    runs = merged

    for run in runs:
        start = run["start"] if run["start"] is not None else "?"
        end = run["end"] if run["end"] is not None else start
        display = f"{run['label']} ({start}/{end})"
        for idx in run["block_indices"]:
            blocks[idx]["section_range"] = display

_TRAILING_ROLE_RE = re.compile(
    r'\s*[-/]\s*(SERVED ON.*|R\d+[\d,\s]*|R\d+\s*\([^)]*\).*)$', re.IGNORECASE
)


def _clean_advocate_line(line: str) -> str:
    """Trim a raw advocate-column line down to just the name, dropping
    trailing role/status text like '-SERVED ON' or '-R1,R2,R3'."""
    cleaned = _TRAILING_ROLE_RE.sub('', line).strip()
    cleaned = cleaned.rstrip('-/, ').strip()
    return cleaned or line.strip()


SUMMARY_ROW_RE = re.compile(
    r'^(\S+)\s+(\d+)\s+([A-Za-z][A-Za-z0-9.()/ ]*?\d+/\s*\d{4})\s+(.+)$'
)
PARTIES_SPLIT_RE = re.compile(r'\s+v\.?s?\.?\s+', re.IGNORECASE)


def _format_summary_parties(parties: str) -> str:
    """'E. Mohammed Faisal v. CBI, Kochi Unit' -> 'E. Mohammed Faisal Vs CBI, Kochi Unit'"""
    parts = PARTIES_SPLIT_RE.split(parties, maxsplit=1)
    if len(parts) == 2:
        return f"{parts[0].strip()} Vs {parts[1].strip()}"
    return parties.strip()


def parse_summary_pdf(path: str):
    """Parse a simple tabular 'Court Hall | Item | Case No. | Parties'
    summary PDF (a manually curated case list, not a full cause list) into
    the same entry shape used for the main report. Tries real table
    extraction first (reliable when the PDF has actual table structure);
    falls back to line-by-line regex parsing otherwise."""
    entries = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table:
                continue
            for row in table:
                cells = [(c or "").strip() for c in row]
                if len(cells) < 4 or cells[0].lower() == "court hall":
                    continue
                court_code, item, case_number, parties = cells[0], cells[1], cells[2], cells[3]
                if not case_number:
                    continue
                entries.append({
                    "court_code": court_code,
                    "sr_no": item,
                    "case_number": MULTI_SPACE_RE.sub(' ', case_number).strip(),
                    "main_parties": _format_summary_parties(parties),
                })

    if entries:
        return entries

    # Fallback: no real table structure detected — parse as plain text lines.
    text = clf_pdftotext(path)
    for line in text.split("\n"):
        line = line.replace('\x0c', '').rstrip()
        if not line.strip() or line.strip().lower().startswith("court hall"):
            continue
        m = SUMMARY_ROW_RE.match(line)
        if not m:
            continue
        court_code, item, case_number, parties = m.groups()
        entries.append({
            "court_code": court_code.strip(),
            "sr_no": item.strip(),
            "case_number": MULTI_SPACE_RE.sub(' ', case_number).strip(),
            "main_parties": _format_summary_parties(parties),
        })
    return entries


def parse_summary_csv(path: str):
    """Parse a CSV with some combination of court/hall, item/sr, case
    number, and parties columns (flexible header names) into the same
    entry shape used for the main report."""
    entries = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = {c.lower().strip(): c for c in (reader.fieldnames or [])}

        def pick(*names):
            for n in names:
                if n in cols:
                    return cols[n]
            return None

        court_col = pick("court hall", "court", "hall", "court_hall")
        item_col = pick("item", "sr.no", "sr no", "sr_no", "sl.no", "s.no")
        case_col = pick("case no.", "case no", "case number", "case_number")
        parties_col = pick("parties", "main parties", "main_parties")

        for row in reader:
            case_number = (row.get(case_col) or "").strip() if case_col else ""
            if not case_number:
                continue
            parties_raw = (row.get(parties_col) or "").strip() if parties_col else ""
            main_parties = (
                _format_summary_parties(parties_raw)
                if PARTIES_SPLIT_RE.search(parties_raw)
                else parties_raw
            )
            entries.append({
                "court_code": (row.get(court_col) or "?").strip() if court_col else "?",
                "sr_no": (row.get(item_col) or "").strip() if item_col else "",
                "case_number": MULTI_SPACE_RE.sub(' ', case_number).strip(),
                "main_parties": main_parties,
            })
    return entries
def parse_summary_file(path: str):
    ext = Path(path).suffix.lower()
    if ext == ".csv":
        return parse_summary_csv(path)
    if ext == ".pdf":
        if _looks_like_advocate_wise(path):
            return parse_advocate_wise_pdf(path)
        return parse_summary_pdf(path)
    raise ValueError(f"Unsupported file type for a case list to add: {path} "
                      f"(expected .pdf or .csv)")

def _looks_like_advocate_wise(path: str) -> bool:
    """The Kerala HC website's 'Causelist - Advocate Wise' export has a
    distinct 6-column layout — different from the generic 4-column
    'Court Hall | Item | Case No. | Parties' summary format. Peek at the
    first page's header row to tell them apart before picking a parser."""
    with pdfplumber.open(path) as pdf:
        if not pdf.pages:
            return False
        table = pdf.pages[0].extract_table()
        if not table or not table[0]:
            return False
        header = [(c or "").strip().lower() for c in table[0]]
        return header[:1] == ["item no"] and "bench" in header


def parse_advocate_wise_pdf(path: str):
    entries = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table or not table[0]:
                continue
            header = [(c or "").strip().lower() for c in table[0]]
            try:
                offset = header.index("item no")  # handles stray blank columns
            except ValueError:
                offset = 0
            for row in table:
                cells = [(c or "").strip() for c in row[offset:offset + 6]]
                if len(cells) < 6 or cells[0].lower() == "item no":
                    continue
                item_no, court_hall, _bench, _list, case_no, parties = cells
                if not case_no:
                    continue
                item_no = item_no.split("\n")[0].strip()
                case_no = MULTI_SPACE_RE.sub(' ', case_no.replace('\n', ' ')).strip()
                parties = MULTI_SPACE_RE.sub(' ', parties.replace('\n', ' ')).strip()
                entries.append({
                    "court_code": court_hall.split("\n")[0].strip(),
                    "sr_no": item_no,
                    "case_number": case_no,
                    "main_parties": parties,
                })
    return entries
def clf_pdftotext(path: str) -> str:
    """Plain 'reading order' text extraction (not the column-aware layout
    used for the main cause list) — good enough for a simple table PDF.
    Tries layout mode first, then falls back to plain extraction if that
    comes back empty (some PDFs render oddly under forced layout)."""
    with pdfplumber.open(path) as pdf:
        layout_text = "\n".join(page.extract_text(layout=True) or "" for page in pdf.pages)
        if layout_text.strip():
            return layout_text
        return "\n".join(page.extract_text() or "" for page in pdf.pages)

'''
def _normalize_case_number_for_dedup(case_number: str) -> str:
    """'CRL.A 1084/ 2017' and 'Crl.A. 1084/2017' should count as the same
    case — but the digits must be kept (unlike advocate-name normalize(),
    which deliberately strips them)."""
    return re.sub(r'[^A-Z0-9]', '', case_number.upper())
'''
def _case_number_keys(case_number: str) -> set:
    """Extract the core (number, year) identifier(s) from a case-number
    string, ignoring the case-type prefix and any whitespace/punctuation
    noise around the numbers themselves. Handles both:
      'Crl.Rev.Pet 541/2022'                -> {('541', '2022')}
      'Crl.Rev.Pet 541,546,547,...,517/2022' -> {('541','2022'), ('546','2022'), ...}
    so a squashed multi-case listing and a single-case listing referring to
    one of the same numbers are recognized as overlapping."""
    keys = set()
    for nums, year in CASE_NO_YEAR_RE.findall(case_number):
        for n in re.split(r'[,\s]+', nums.strip()):
            if n.isdigit():
                keys.add((n, year))
    return keys


def _case_numbers_overlap(a: str, b: str) -> bool:
    ka, kb = _case_number_keys(a), _case_number_keys(b)
    if ka and kb:
        return bool(ka & kb)
    # Neither string had a recognizable "number/year" pattern — fall back
    # to the old strict compare so we don't silently treat everything as
    # a match just because the format was unusual.
    return re.sub(r'[^A-Z0-9]', '', a.upper()) == re.sub(r'[^A-Z0-9]', '', b.upper())

def merge_extra_entries(tagged_blocks, extra_entries, main_blocks):
    code_to_court = {}
    for b in main_blocks:
        code_to_court.setdefault(_short_court_code(b["court"]), b["court"])

    # Group existing case numbers by court, not globally — two different
    # courts can legitimately have a case numbered "541/2022" each.
    existing_by_court = {}
    for b in tagged_blocks:
        existing_by_court.setdefault(b["court"], []).append(b["case_number"])

    merged = list(tagged_blocks)
    added, skipped_dupe = 0, 0
    for e in extra_entries:
        court = code_to_court.get(e["court_code"], e["court_code"])
        bucket = existing_by_court.setdefault(court, [])
        if any(_case_numbers_overlap(e["case_number"], existing) for existing in bucket):
            skipped_dupe += 1
            continue
        bucket.append(e["case_number"])
        merged.append({
            "court": court,
            "section": "ADDED",
            "section_range": "Added from list",
            "sr_no": e["sr_no"] or "-",
            "case_number": e["case_number"],
            "main_parties": e["main_parties"],
            "page": None,
            "source": "added",
            "matched_advocates": ["(added from list)"],
        })
        added += 1
    return merged, added, skipped_dupe


def collect_advocate_candidates(blocks, query, limit=25):
    """Scan every advocate-column line in the document and return the
    distinct exact spellings that fuzzy-contain the query's words, most
    frequent first — for the user to pick precise name(s) to track instead
    of relying on fuzzy matching at report time."""
    query_tokens = [t for t in normalize(query).split(' ') if len(t) > 1]
    if not query_tokens:
        return []
    counts = Counter()
    for b in blocks:
        for line in b.get("advocate_lines", []):
            if all(t in normalize(line) for t in query_tokens):
                counts[_clean_advocate_line(line)] += 1
    return counts.most_common(limit)


def find_matches(blocks, advocates):
    """For each block, find which tracked advocates appear in it. Each block is
    kept ONCE and tagged with every matching advocate, so a case shared between
    lawyers is never listed twice (redundancy removed)."""
    tagged = []
    for b in blocks:
        matched = [name for name in advocates if is_match(b["advocate_text"], name)]
        if matched:
            b2 = dict(b)
            b2["matched_advocates"] = matched
            tagged.append(b2)
    return tagged
def _flatten_sort_key(b):
    try:
        return int(str(b["sr_no"]).split(".")[0])
    except (ValueError, TypeError):
        return float("inf")

def _family_key(b):
    """'124', '124.1', '124.19' all belong to family 124 (a main case with
    'WITH'-tagged sub-cases) — but only among cases from the actual cause
    list's own sequential numbering. A manually-added entry (from --extra)
    uses a completely unrelated numbering system, so it must never be
    merged with a main-list case (or another added entry) just because
    they happen to share the same serial/item number — each gets a unique
    key of its own."""
    if b.get("source") == "added":
        return f"added:{b['case_number']}"
    try:
        return int(str(b["sr_no"]).split('.')[0])
    except ValueError:
        return str(b["sr_no"])

def _sr_sort_key(sr_no):
        try:
            return float(sr_no)
        except (ValueError, TypeError):
            return float('inf')
def collapse_subcases(cases):
    """Collapse a 'WITH'-tagged family of sub-cases (124, 124.1, 124.2, ...)
    into a single display unit: the lowest-numbered member is shown in full
    (case number, parties, advocates); the rest are listed as a compact
    'also covers' reference (serial + case number only), not full cards."""
    families = {}
    order = []
    for b in cases:
        key = _family_key(b)
        if key not in families:
            families[key] = []
            order.append(key)
        families[key].append(b)
    
    def _sort_key(b):
        s = str(b["sr_no"])
        if '.' in s:
            try:
                frac = float(s.split('.', 1)[1])
            except ValueError:
                frac = 0.0
            return (1, frac)
        return (0, 0.0)

    units = []
    for key in order:
        members = families[key]
        members.sort(key=_sort_key)
        primary, rest = members[0], members[1:]
        units.append({"primary": primary, "subcases": rest})
    return units


def full_court_stage_ranges(all_blocks):
    """The Adm/Pet/Hg fractions for a court describe that court's WHOLE
    list — they shouldn't depend on which specific cases happened to match
    a tracked advocate. Walk every parsed case (matched or not) and return,
    per court, the ordered list of distinct stage ranges actually printed
    in that court's list, e.g. {'COURT NO. 1B - 5010': ['Adm (1/6)',
    'Pet (7/69)', 'Hg (823)']}."""
    ranges = {}
    for b in all_blocks:
        seen = ranges.setdefault(b["court"], [])
        r = b.get("section_range")
        if r and (not seen or seen[-1] != r):
            seen.append(r)
    return ranges

def write_stage_json(ranges: dict, out_dir):
    """Split {court: [range_str,...]} into 3 files by stage prefix
    (Adm/Pet/Hg), each shaped {court: [range_str,...]}, preserving order."""
    buckets = {"Adm": {}, "Pet": {}, "Hg": {}}
    for court, range_list in ranges.items():
        for r in range_list:
            label = r.split(" ", 1)[0]
            if label in buckets:
                buckets[label].setdefault(court, []).append(r)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for label, fname in (("Adm", "adm.json"), ("Pet", "pet.json"), ("Hg", "hg.json")):
        (out_dir / fname).write_text(json.dumps(buckets[label], indent=2, ensure_ascii=False))

def group_by_court(tagged_blocks):
    """Group matched, deduplicated blocks by court, then by section stage
    (FOR ORDERS / FRESH ADMISSION / ADMISSION / FOR HEARING / etc.), each
    labeled with its case-number range within that court."""
    courts = {}
    for b in tagged_blocks:
        court_group = courts.setdefault(b["court"], {})
        section_group = court_group.setdefault(b.get("section_range", b.get("section", "")), [])
        section_group.append(b)

    def sr_key(b):
        s = str(b["sr_no"])

        try:
            if "." in s:
                main, sub = s.split(".", 1)
                return (int(main), int(sub))
            return (int(s), -1)
        except Exception:
            return (float("inf"), float("inf"))

    for court_group in courts.values():
        for section_range in court_group:
            court_group[section_range].sort(key=sr_key)

    def court_sort_key(label):
        m = re.search(r'COURT NO\.\s*(\d+)', label)
        return (0, int(m.group(1)), label) if m else (1, 0, label)

    return dict(sorted(courts.items(), key=lambda kv: court_sort_key(kv[0])))


# ---------------------------------------------------------------------------
# Advocate list (local config, persists between runs)
# ---------------------------------------------------------------------------

def load_advocates():
    if not CONFIG_FILE.exists():
        return []
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def save_advocates(advocates):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(advocates, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

_SECTION_RANGE_TAIL_RE = re.compile(r'\((>?\d+(?:/\d+)?|>?\?)\)$')
_COURT_CODE_RE = re.compile(r'COURT NO\.\s*([0-9A-Za-z]+)', re.IGNORECASE)


def _abbreviate_section_range(section_range: str) -> str:
    """'Adm (1/34)' -> 'Adm 1/34' — always a fraction, using the actual
    highest serial number seen even for a court's last/pending stage."""
    m = _SECTION_RANGE_TAIL_RE.search(section_range)
    if not m:
        return section_range
    range_part = m.group(1)
    label = section_range[:m.start()].strip()
    if range_part.startswith('>'):
        return f"{label}&nbsp;{range_part[1:]}"
    return f"{label}&nbsp;{range_part}"


def _short_court_code(court_label: str) -> str:
    """'COURT NO. 1B - 5010' -> '1B'; falls back to the full label if the
    court wasn't identified by the usual 'COURT NO. X' pattern."""
    m = _COURT_CODE_RE.search(court_label)
    return m.group(1) if m else court_label


def build_pdf_report(out_path, source_name, num_pages, all_blocks, grouped):
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=15, spaceAfter=1,
    )
    sub_style = ParagraphStyle(
        "Sub", parent=styles["Normal"], fontName="Helvetica", fontSize=7.5,
        textColor=colors.HexColor("#5B6472"), spaceAfter=8,
    )
    court_style = ParagraphStyle(
        "Court", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=11, spaceBefore=9, spaceAfter=1,
        textColor=colors.HexColor("#1C2536"),
    )
    stages_style = ParagraphStyle(
        "Stages", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=8, spaceAfter=4,
        textColor=colors.HexColor("#A6802E"),
    )
    case_line_style = ParagraphStyle(
        "CaseLine", parent=styles["Normal"], fontName="Helvetica", fontSize=8,
        leading=11, spaceBefore=4, spaceAfter=1.5,
        leftIndent=13, firstLineIndent=-13,  # hanging indent: wrapped lines
        # align under the case text, not under the "#" number
    )
    sub_line_style = ParagraphStyle(
        "SubLine", parent=styles["Normal"], fontName="Helvetica", fontSize=7,
        textColor=colors.HexColor("#5B6472"), leading=9,
        leftIndent=24, firstLineIndent=-9, spaceAfter=0.5,
    )

    # Two-column layout: content flows down the left column, then the right
    # column, then wraps to a fresh page — same as filling a folded sheet.
    margin = 12 * mm
    gutter = 6 * mm
    page_w, page_h = A4
    col_w = (page_w - 2 * margin - gutter) / 2
    col_h = page_h - 2 * margin
    left = Frame(margin, margin, col_w, col_h, id="left",
                 leftPadding=0, rightPadding=6, topPadding=0, bottomPadding=0)
    right = Frame(margin + col_w + gutter, margin, col_w, col_h, id="right",
                  leftPadding=6, rightPadding=0, topPadding=0, bottomPadding=0)
    doc = BaseDocTemplate(out_path, pagesize=A4)
    doc.addPageTemplates([PageTemplate(id="TwoCol", frames=[left, right])])
    story = []

    story.append(Paragraph("Docket Finder", title_style))
    story.append(Paragraph(
        f"{source_name} &nbsp;&bull;&nbsp; {datetime.now().strftime('%d %b %Y, %I:%M %p')}"
        f" &nbsp;&bull;&nbsp; {num_pages}pg / {len(all_blocks)} cases scanned",
        sub_style,
    ))

    full_ranges = full_court_stage_ranges(all_blocks)

    for court, sections in grouped.items():
        stages_line = "   ".join(
            _abbreviate_section_range(s) for s in full_ranges.get(court, sections.keys())
        )
        story.append(KeepTogether([
            Paragraph(f"{_short_court_code(court)} &nbsp; {stages_line}", court_style),
            HRFlowable(width="100%", thickness=0.8, color=colors.HexColor("#1C2536")),
        ]))

        flattened = [b for cases in sections.values() for b in cases]
        flattened.sort(key=lambda b: _sr_sort_key(b["sr_no"]))
        for unit in collapse_subcases(flattened):
            b = unit["primary"]
            lines = [
                Paragraph(f"<b>#{b['sr_no']}</b> {b['case_number']} — "
                          f"{b['main_parties'] or '(parties not detected)'}", case_line_style),
            ]
            for s in unit["subcases"]:
                lines.append(Paragraph(f"&ndash; #{s['sr_no']} {s['case_number']}", sub_line_style))
            story.append(KeepTogether(lines))

    doc.build(story)


def write_csv(out_path, all_blocks, grouped):
    full_ranges = full_court_stage_ranges(all_blocks)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["court", "court_full_stages", "sr_no", "case_number", "main_parties",
                          "page", "matched_advocates", "also_covers"])
        for court, sections in grouped.items():
            stages_line = " | ".join(full_ranges.get(court, sections.keys()))
            flattened = [b for cases in sections.values() for b in cases]
            flattened.sort(key=lambda b: _sr_sort_key(b["sr_no"]))
            for unit in collapse_subcases(flattened):
                b = unit["primary"]
                also_covers = "; ".join(f"#{s['sr_no']} {s['case_number']}" for s in unit["subcases"])
                writer.writerow([
                    court, stages_line, b["sr_no"], b["case_number"], b["main_parties"],
                    b["page"], "; ".join(b["matched_advocates"]), also_covers,
                ])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_add(args):
    advocates = load_advocates()
    if args.name in advocates:
        print(f"'{args.name}' is already tracked.")
        return
    advocates.append(args.name)
    save_advocates(advocates)
    print(f"Added '{args.name}'. Now tracking {len(advocates)} advocate(s).")


def cmd_remove(args):
    advocates = load_advocates()
    if args.name not in advocates:
        print(f"'{args.name}' was not in the list.")
        return
    advocates.remove(args.name)
    save_advocates(advocates)
    print(f"Removed '{args.name}'. Now tracking {len(advocates)} advocate(s).")


def cmd_list(args):
    advocates = load_advocates()
    if not advocates:
        print("No advocates tracked yet. Add one with: cause_list_finder.py add \"Name\"")
        return
    print(f"Tracking {len(advocates)} advocate(s):")
    for a in advocates:
        print(f"  - {a}")


def cmd_suggest(args):
    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"File not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Parsing {pdf_path.name} ...", file=sys.stderr)
    blocks = parse_pdf(str(pdf_path))

    candidates = collect_advocate_candidates(blocks, args.query)
    if not candidates:
        print(f"No advocate-column text matching '{args.query}' was found in this document.")
        return

    print(f"\nFound {len(candidates)} distinct spelling(s) matching '{args.query}':\n")
    for i, (name, count) in enumerate(candidates, start=1):
        print(f"  {i:2d}. {name}  ({count} case{'s' if count != 1 else ''})")

    if not sys.stdin.isatty():
        print("\n(Run interactively to select which of these to add to your tracked list, "
              "or add one directly with: cause_list_finder.py add \"Exact Name\")")
        return

    choice = input("\nEnter number(s) to track (comma-separated), or press Enter to skip: ").strip()
    if not choice:
        return
    try:
        indices = [int(x.strip()) for x in choice.split(",") if x.strip()]
    except ValueError:
        print("Could not parse that input — nothing added.", file=sys.stderr)
        return

    advocates = load_advocates()
    added = []
    for i in indices:
        if 1 <= i <= len(candidates):
            name = candidates[i - 1][0]
            if name not in advocates:
                advocates.append(name)
                added.append(name)
    if added:
        save_advocates(advocates)
        print(f"Added: {', '.join(added)}. Now tracking {len(advocates)} advocate(s).")
    else:
        print("Nothing new added.")


def cmd_run(args):
    if args.advocates:
        advocates = [n.strip() for n in args.advocates.split(",") if n.strip()]
    else:
        advocates = load_advocates()

    if not advocates:
        print("No advocates specified. Use --advocates \"Name1,Name2\" or add some first with:\n"
              "  cause_list_finder.py add \"Name\"", file=sys.stderr)
        sys.exit(1)

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"File not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Parsing {pdf_path.name} ...", file=sys.stderr)
    blocks = parse_pdf(str(pdf_path))
    with pdfplumber.open(str(pdf_path)) as pdf:
        num_pages = len(pdf.pages)

    print(f"Found {len(blocks)} case entries across {num_pages} pages.", file=sys.stderr)
    tagged = find_matches(blocks, advocates)

    if args.extra:
        for extra_path in args.extra:
            p = Path(extra_path)
            if not p.exists():
                print(f"Warning: extra file not found, skipping: {p}", file=sys.stderr)
                continue
            entries = parse_summary_file(str(p))
            tagged, added, skipped = merge_extra_entries(tagged, entries, blocks)
            print(f"  {p.name}: {len(entries)} case(s) read, {added} added, "
                  f"{skipped} skipped as already present.", file=sys.stderr)

    grouped = group_by_court(tagged)
    print(f"Matched {len(tagged)} unique case(s) (deduplicated) across "
          f"{len(grouped)} court(s).", file=sys.stderr)

    out_path = args.out or str(pdf_path.with_suffix("")) + "_results.pdf"
    build_pdf_report(out_path, pdf_path.name, num_pages, blocks, grouped)
    print(f"Report written to {out_path}")

    if args.csv:
        write_csv(args.csv, blocks, grouped)
        print(f"CSV written to {args.csv}")


def main():
    parser = argparse.ArgumentParser(description="Extract advocate cases from a court cause list PDF.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Add an advocate to the tracked list")
    p_add.add_argument("name")
    p_add.set_defaults(func=cmd_add)

    p_remove = sub.add_parser("remove", help="Remove an advocate from the tracked list")
    p_remove.add_argument("name")
    p_remove.set_defaults(func=cmd_remove)

    p_list = sub.add_parser("list", help="Show the tracked advocate list")
    p_list.set_defaults(func=cmd_list)

    p_suggest = sub.add_parser(
        "suggest",
        help="Find exact name spellings in a cause list PDF matching a fuzzy query, "
             "and pick which to track (recommended way to add advocates)",
    )
    p_suggest.add_argument("query", help="Approximate name, e.g. \"Martin Jose\"")
    p_suggest.add_argument("pdf", help="Path to a cause list PDF to search")
    p_suggest.set_defaults(func=cmd_suggest)

    p_run = sub.add_parser("run", help="Parse a cause list PDF and generate a report")
    p_run.add_argument("pdf", help="Path to the cause list PDF")
    p_run.add_argument("--out", help="Output PDF path (default: <input>_results.pdf)")
    p_run.add_argument("--csv", help="Also write a CSV of matches to this path")
    p_run.add_argument("--advocates", help="Comma-separated names, overrides the saved list for this run")
    p_run.add_argument(
        "--extra", nargs="+", metavar="FILE",
        help="One or more supplementary case-list files (.pdf table or .csv) with "
             "'Court Hall / Item / Case No. / Parties' columns to merge in, skipping "
             "any case number already found in the main cause list.",
    )
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()