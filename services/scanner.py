"""
services/scanner.py — Scan a library folder and sync it with the database.

Supported formats: CBZ, CBR, EPUB, PDF, MOBI, AZW3.
"""

import os
from pathlib import Path

from db.database import get_conn
from db.models import ScanResult
from services.covers import extract_cover

SUPPORTED = {".cbz", ".cbr", ".epub", ".pdf", ".mobi", ".azw3"}


def _ext_to_type(ext: str) -> str:
    return ext.lstrip(".").lower()


def scan_library(library_path: Path) -> ScanResult:
    """
    Walk library_path recursively, insert / update books in the DB.
    Books whose files have disappeared are removed.
    Returns a ScanResult summary.
    """
    added = updated = removed = 0
    errors: list[str] = []

    # Collect all files on disk
    disk_files: dict[str, Path] = {}
    for root, _, files in os.walk(library_path):
        for fname in files:
            p = Path(root) / fname
            if p.suffix.lower() in SUPPORTED:
                disk_files[str(p)] = p

    with get_conn() as conn:
        # Existing DB entries
        db_paths = {
            row["path"] for row in conn.execute("SELECT path FROM books").fetchall()
        }

        # --- Insert new files ---
        for path_str, path in disk_files.items():
            if path_str in db_paths:
                continue
            try:
                title, series, volume = _guess_metadata(path)
                book_type = _ext_to_type(path.suffix)
                file_size = path.stat().st_size

                cur = conn.execute(
                    """
                    INSERT INTO books (path, title, series, volume, type, file_size)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (path_str, title, series, volume, book_type, file_size),
                )
                book_id = cur.lastrowid

                # Insert default reading status
                conn.execute(
                    "INSERT OR IGNORE INTO reading_status (book_id) VALUES (?)",
                    (book_id,),
                )

                # Extract cover (best-effort)
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

        # --- Remove deleted files ---
        for path_str in db_paths - set(disk_files.keys()):
            conn.execute("DELETE FROM books WHERE path = ?", (path_str,))
            removed += 1

    return ScanResult(added=added, updated=updated, removed=removed, errors=errors)


def _guess_metadata(path: Path) -> tuple[str, str | None, int | None]:
    """
    Try to extract title, series, and volume from the filename.
    Falls back to the stem as the title.
    Mirrors the heuristics in cbz_standardize.py.
    """
    import re

    stem = path.stem

    # Pattern: <series> - T<volume>  (standard output of our standardizer)
    m = re.match(r"^(.+?)\s*-\s*[Tt](\d+)$", stem)
    if m:
        series = m.group(1).strip()
        volume = int(m.group(2))
        title = f"{series} T{volume:02d}"
        return title, series, volume

    # Pattern: <series> [v|vol|t|tome] <digits>
    m = re.search(
        r"^(.*?)[\s_\-\.]*(?:v|t|vol|tome|volume)[\s_\-\.]*(\d+)\s*$",
        stem,
        re.IGNORECASE,
    )
    if m:
        series = re.sub(r"[\s_\-\.]+", " ", m.group(1)).strip() or None
        volume = int(m.group(2))
        title = f"{series} {volume}" if series else stem
        return title, series, volume

    # No pattern matched
    return stem, None, None
