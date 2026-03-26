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

Folder heuristics
-----------------
Category: each segment of the path between library_root and the file is tested
against keyword lists. The first match from the top (closest to root) wins.
  e.g.  library/Mangas/One Piece/T01.cbz  →  category = "manga"
        library/BD/Lucky Luke/T01.cbz      →  category = "comics"

Series: if the file sits inside a subdirectory of a category folder, that
subdirectory name is used as the series (unless the filename already contains
the same series string, in which case it just confirms the guess).
  e.g.  library/Mangas/One Piece/T01.cbz  →  series = "One Piece"
        library/Mangas/One Piece T01.cbz   →  series stays from filename
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
    ".pdf":  "book",
}

# ---------------------------------------------------------------------------
# Folder-based category keywords
# Each entry is (category_value, {keyword_set}).
# Matching is case-insensitive on the normalised folder name
# (lowercased, accents stripped, spaces/underscores/hyphens collapsed).
# First match wins, scanning from the library root downward.
# ---------------------------------------------------------------------------

_FOLDER_CATEGORY_RULES: list[tuple[str, set[str]]] = [
    ("manga",  {"manga", "mangas", "manhwa", "manhua", "webtoon", "webtoons"}),
    ("comics", {"comics", "comic", "bd", "bandes dessinees", "bande dessinee",
                "bds", "western comics", "us comics", "marvel", "dc", "superhero"}),
    ("book",   {"books", "book", "novels", "novel", "romans", "roman", "ebooks",
                "ebook", "livres", "livre", "literature", "non-fiction", "fiction"}),
]


def _normalise_folder_name(name: str) -> str:
    """Lowercase, strip accents, collapse separators."""
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = nfkd.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[\s_\-]+", " ", ascii_name).strip().lower()


def _category_from_path(path: Path, library_root: Path) -> Optional[str]:
    """
    Walk path segments between library_root and the file.
    Return the first category matched, or None if no keyword matches.
    """
    try:
        rel = path.relative_to(library_root)
    except ValueError:
        return None

    # Test every intermediate folder (not the filename itself)
    for part in rel.parts[:-1]:
        norm = _normalise_folder_name(part)
        for category, keywords in _FOLDER_CATEGORY_RULES:
            if norm in keywords:
                return category
    return None


def _series_from_path(
    path: Path,
    library_root: Path,
    filename_series: Optional[str],
    category_folder: Optional[str],
) -> Optional[str]:
    """
    Infer series from folder structure.

    Rules (in priority order):
    1. If the file is directly inside a *category* folder  →  no series from path
       (the category folder is not a series name)
    2. If the file is inside a subfolder of a category folder  →  that subfolder = series
    3. If the file is inside any non-root subfolder and no series was found from
       the filename  →  use the immediate parent folder as series

    The folder-derived series is used as-is if filename_series is None.
    If filename_series is set and resembles the folder name, keep filename_series
    (it's more precisely parsed — has vol number stripped, etc.).
    """
    try:
        rel = path.relative_to(library_root)
    except ValueError:
        return filename_series

    parts = rel.parts[:-1]   # intermediate folders only, no filename
    if not parts:
        return filename_series  # file is at library root — no folder hint

    # Find the category folder depth (if any)
    cat_depth: Optional[int] = None
    if category_folder is not None:
        for i, part in enumerate(parts):
            norm = _normalise_folder_name(part)
            for _, keywords in _FOLDER_CATEGORY_RULES:
                if norm in keywords:
                    cat_depth = i
                    break
            if cat_depth is not None:
                break

    # The series candidate is the folder immediately after the category folder,
    # or the immediate parent folder if no category was detected.
    if cat_depth is not None:
        series_depth = cat_depth + 1
    else:
        series_depth = 0   # use the top-most subfolder

    if series_depth >= len(parts):
        # File is directly inside the category folder — no series hint
        return filename_series

    folder_series = parts[series_depth]

    # If filename already has a series, only use folder series if they look alike
    # (avoids overriding a good filename-based guess with an unrelated folder name)
    if filename_series:
        fn_norm  = _normalise_folder_name(filename_series)
        fld_norm = _normalise_folder_name(folder_series)
        # Accept if folder name starts with the filename series or vice-versa
        if fld_norm.startswith(fn_norm) or fn_norm.startswith(fld_norm):
            return filename_series   # filename parse is more precise
        # They differ — trust the folder name (explicit organisation beats filename heuristic)
        return folder_series

    return folder_series


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


def _ext_to_type(ext: str) -> str:
    return ext.lstrip(".").lower()


def _guess_category(book_type: str) -> str:
    return _EXT_CATEGORY.get("." + book_type, "unknown")


# ---------------------------------------------------------------------------
# Insert / update one book
# ---------------------------------------------------------------------------

def _insert_book(conn, path_str: str, path: Path, library_root: Path) -> str:
    """
    Insert a new book. Returns 'added' on success, raises on error.

    Metadata resolution order:
      1. Filename heuristics (series + volume from name pattern)
      2. Folder-based series override (parent folder = series name)
      3. Folder-based category override (keyword match on path segments)
      4. Extension-based category fallback
    """
    title, filename_series, volume = _guess_metadata(path)
    book_type = _ext_to_type(path.suffix)

    # ── Category ──────────────────────────────────────────────────────────
    folder_category = _category_from_path(path, library_root)
    category = folder_category or _guess_category(book_type)

    # ── Series ────────────────────────────────────────────────────────────
    series = _series_from_path(path, library_root, filename_series, folder_category)

    # Rebuild title if series changed (folder gave us a better series name)
    if series and series != filename_series:
        if volume is not None:
            title = f"{series} T{volume:02d}"
        else:
            title = series

    file_size = path.stat().st_size

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
                    _insert_book(conn, path_str, path, library_root=library_path)
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
    """Extract title, series, volume from filename alone."""
    stem = path.stem

    # Pattern: <series> - T<volume>  e.g. "One Piece - T01"
    m = re.match(r"^(.+?)\s*-\s*[Tt](\d+)$", stem)
    if m:
        series = m.group(1).strip()
        volume = int(m.group(2))
        return f"{series} T{volume:02d}", series, volume

    # Pattern: <series> [v|vol|t|tome|volume] <digits>
    m = re.search(
        r"^(.*?)[\s_\-\.]*(?:v|t|vol|tome|volume)[\s_\-\.]*(\d+)\s*$",
        stem, re.IGNORECASE,
    )
    if m:
        series = re.sub(r"[\s_\-\.]+", " ", m.group(1)).strip() or None
        volume = int(m.group(2))
        return (f"{series} {volume}" if series else stem), series, volume

    return stem, None, None
