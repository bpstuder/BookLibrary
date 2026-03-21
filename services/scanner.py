"""
services/scanner.py — Scan a library folder and sync it with the database.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from db.database import get_conn
from db.models import ScanResult
from services.covers import extract_cover

SUPPORTED = {".cbz", ".cbr", ".epub", ".pdf", ".mobi", ".azw3"}

# Category heuristics based on file extension
_EXT_CATEGORY = {
    ".cbz": "manga",    # default; user can change per-book
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


def scan_library(library_path: Path) -> ScanResult:
    """
    Walk library_path recursively, insert/update books in the DB.
    Books whose files have disappeared are removed.
    """
    added = updated = removed = 0
    errors: list[str] = []

    disk_files: dict[str, Path] = {}
    for root, dirs, files in os.walk(library_path):
        # Skip macOS __MACOSX metadata directories
        dirs[:] = [d for d in dirs if d != "__MACOSX"]
        for fname in files:
            # Skip macOS AppleDouble sidecar files (._filename)
            if fname.startswith("._") or fname.startswith("."):
                continue
            p = Path(root) / fname
            if p.suffix.lower() in SUPPORTED:
                disk_files[str(p)] = p

    with get_conn() as conn:
        db_paths = {
            row["path"] for row in conn.execute("SELECT path FROM books").fetchall()
        }

        for path_str, path in disk_files.items():
            if path_str in db_paths:
                continue
            try:
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
                    "INSERT OR IGNORE INTO reading_status (book_id) VALUES (?)",
                    (book_id,),
                )

                try:
                    cover = extract_cover(path, book_id)
                    if cover:
                        conn.execute(
                            "UPDATE books SET cover_path = ? WHERE id = ?",
                            (str(cover), book_id),
                        )
                except Exception:
                    pass

                added += 1
            except Exception as e:
                errors.append(f"{path.name}: {e}")

        for path_str in db_paths - set(disk_files.keys()):
            conn.execute("DELETE FROM books WHERE path = ?", (path_str,))
            removed += 1

    return ScanResult(added=added, updated=updated, removed=removed, errors=errors)


def _guess_metadata(path: Path) -> tuple[str, str | None, int | None]:
    """Extract title, series, volume from filename."""
    stem = path.stem

    # <series> - T<volume>
    m = re.match(r"^(.+?)\s*-\s*[Tt](\d+)$", stem)
    if m:
        series = m.group(1).strip()
        volume = int(m.group(2))
        return f"{series} T{volume:02d}", series, volume

    # <series> [v|vol|t|tome|volume] <digits>
    m = re.search(
        r"^(.*?)[\s_\-\.]*(?:v|t|vol|tome|volume)[\s_\-\.]*(\d+)\s*$",
        stem, re.IGNORECASE,
    )
    if m:
        series = re.sub(r"[\s_\-\.]+", " ", m.group(1)).strip() or None
        volume = int(m.group(2))
        return (f"{series} {volume}" if series else stem), series, volume

    return stem, None, None
