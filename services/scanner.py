"""
services/scanner.py — Scan a library folder and sync it with the database.

Public API
----------
scan_library(library_path, on_progress=None, cancel_event=None) -> ScanResult
    Synchronous full scan. on_progress(done, total, filename) called after each file.
    cancel_event: threading.Event — set it to abort mid-scan.

scan_library_stream(library_path, cancel_event) -> Generator[dict, None, None]
    Generator variant used by the SSE endpoint. Yields dicts:
      {"type": "count",    "total": int}          — file discovery done
      {"type": "progress", "done": int, "total": int, "file": str, "action": str}
      {"type": "removed",  "count": int}
      {"type": "done",     "added": int, "removed": int, "errors": list[str]}
      {"type": "cancelled"}
      {"type": "error",    "msg": str}
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Callable, Generator, Optional

from db.database import get_conn
from db.models import ScanResult
from services.covers import extract_cover

SUPPORTED = {".cbz", ".cbr", ".epub", ".pdf", ".mobi", ".azw3"}

_EXT_CATEGORY = {
    ".cbz": "manga",
    ".cbr": "comics",
    ".epub": "book",
    ".mobi": "book",
    ".azw3": "book",
    ".pdf": "book",
}


def _ext_to_type(ext: str) -> str:
    return ext.lstrip(".").lower()


def _guess_category(book_type: str) -> str:
    return _EXT_CATEGORY.get("." + book_type, "unknown")


# ---------------------------------------------------------------------------
# File discovery (shared)
# ---------------------------------------------------------------------------

def _discover_files(library_path: Path) -> dict[str, Path]:
    """Walk library_path and return {path_str: Path} for all supported files."""
    disk_files: dict[str, Path] = {}
    for root, dirs, files in os.walk(library_path):
        dirs[:] = [d for d in dirs if d != "__MACOSX"]
        for fname in files:
            if fname.startswith("._") or fname.startswith("."):
                continue
            p = Path(root) / fname
            if p.suffix.lower() in SUPPORTED:
                disk_files[str(p)] = p
    return disk_files


# ---------------------------------------------------------------------------
# Insert / update one book (shared between both scan variants)
# ---------------------------------------------------------------------------

def _insert_book(conn, path_str: str, path: Path) -> str:
    """
    Insert a new book. Returns one of: 'added' | 'skip' | raises Exception.
    Caller is responsible for checking whether path_str is already in DB.
    """
    title, series, volume = _guess_metadata(path)
    book_type = _ext_to_type(path.suffix)
    file_size = path.stat().st_size
    category  = _guess_category(book_type)

    cur = conn.execute(
        """
        INSERT INTO books (path, title, series, volume, type, file_size, category)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (path_str, title, series, volume, book_type, file_size, category),
    )
    book_id = cur.lastrowid
    conn.execute(
        "INSERT OR IGNORE INTO reading_status (book_id) VALUES (?)", (book_id,)
    )
    try:
        cover = extract_cover(path, book_id)
        if cover:
            conn.execute(
                "UPDATE books SET cover_path = ? WHERE id = ?",
                (str(cover), book_id),
            )
    except Exception:
        pass  # cover extraction is non-fatal

    return "added"


# ---------------------------------------------------------------------------
# SSE streaming scan
# ---------------------------------------------------------------------------

def scan_library_stream(
    library_path: Path,
    cancel_event: Optional[threading.Event] = None,
) -> Generator[dict, None, None]:
    """
    Generator that yields progress dicts for SSE streaming.
    Supports cancellation via cancel_event (threading.Event).
    """
    import logging
    log = logging.getLogger("manga.scan")

    try:
        # Phase 1 — discover all files on disk
        log.info("Scan: discovering files in %s", library_path)
        disk_files = _discover_files(library_path)
        total = len(disk_files)
        yield {"type": "count", "total": total}
        log.info("Scan: found %d files", total)

        if cancel_event and cancel_event.is_set():
            yield {"type": "cancelled"}
            return

        # Phase 2 — compare with DB and insert new books
        with get_conn() as conn:
            db_paths = {
                row["path"]
                for row in conn.execute("SELECT path FROM books").fetchall()
            }

            added   = 0
            errors: list[str] = []
            done    = 0

            new_files = {k: v for k, v in disk_files.items() if k not in db_paths}
            existing  = len(disk_files) - len(new_files)

            if existing:
                yield {
                    "type": "progress", "done": existing, "total": total,
                    "file": f"({existing} already in library)", "action": "skip",
                }
                done = existing

            for path_str, path in new_files.items():
                if cancel_event and cancel_event.is_set():
                    yield {"type": "cancelled"}
                    return

                done += 1
                try:
                    _insert_book(conn, path_str, path)
                    added += 1
                    action = "added"
                    log.debug("Scan: added %s", path.name)
                except Exception as e:
                    errors.append(f"{path.name}: {e}")
                    action = "error"
                    log.warning("Scan: error on %s: %s", path.name, e)

                yield {
                    "type":   "progress",
                    "done":   done,
                    "total":  total,
                    "file":   path.name,
                    "action": action,
                }

            # Phase 3 — remove orphaned DB entries
            orphans = db_paths - set(disk_files.keys())
            removed = len(orphans)
            for path_str in orphans:
                conn.execute("DELETE FROM books WHERE path = ?", (path_str,))
                log.debug("Scan: removed orphan %s", path_str)

            if removed:
                yield {"type": "removed", "count": removed}

        log.info("Scan done: +%d added, -%d removed, %d errors", added, removed, len(errors))
        yield {"type": "done", "added": added, "removed": removed, "errors": errors}

    except Exception as e:
        log.exception("Scan failed: %s", e)
        yield {"type": "error", "msg": str(e)}


# ---------------------------------------------------------------------------
# Blocking scan (used by startup auto-scan)
# ---------------------------------------------------------------------------

def scan_library(
    library_path: Path,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> ScanResult:
    """
    Blocking scan. Consumes scan_library_stream internally.
    on_progress(done, total, filename) is called after each file if provided.
    """
    added = removed = 0
    errors: list[str] = []

    for event in scan_library_stream(library_path, cancel_event):
        t = event["type"]
        if t == "progress" and on_progress:
            on_progress(event["done"], event["total"], event.get("file", ""))
        elif t == "done":
            added   = event["added"]
            removed = event["removed"]
            errors  = event["errors"]
        elif t == "cancelled":
            break
        elif t == "error":
            errors.append(event["msg"])

    return ScanResult(added=added, updated=0, removed=removed, errors=errors)


# ---------------------------------------------------------------------------
# Filename heuristics
# ---------------------------------------------------------------------------

def _guess_metadata(path: Path) -> tuple[str, str | None, int | None]:
    """Extract title, series, volume from filename."""
    stem = path.stem

    m = re.match(r"^(.+?)\s*-\s*[Tt](\d+)$", stem)
    if m:
        series = m.group(1).strip()
        volume = int(m.group(2))
        return f"{series} T{volume:02d}", series, volume

    m = re.search(
        r"^(.*?)[\s_\-\.]*(?:v|t|vol|tome|volume)[\s_\-\.]*(\d+)\s*$",
        stem, re.IGNORECASE,
    )
    if m:
        series = re.sub(r"[\s_\-\.]+", " ", m.group(1)).strip() or None
        volume = int(m.group(2))
        return (f"{series} {volume}" if series else stem), series, volume

    return stem, None, None
