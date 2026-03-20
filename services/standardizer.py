"""
services/standardizer.py — Wrap cbz_standardize.process_cbz for use in FastAPI.

Yields log lines as strings (for SSE streaming).
After success, re-scans the affected book in the DB.
"""

import sys
import io
from pathlib import Path
from typing import Generator

import cbz_standardize as cbz
from db.database import get_conn
from services.covers import extract_cover


def standardize_book(
    book_id: int,
    webp: bool = False,
    webp_quality: int = 85,
) -> Generator[str, None, None]:
    """
    Generator: yields log lines while processing, then a final 'DONE:<path>'
    or 'ERROR:<message>' sentinel line.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT path FROM books WHERE id = ?", (book_id,)
        ).fetchone()

    if not row:
        yield f"ERROR:Book {book_id} not found in database."
        return

    cbz_path = Path(row["path"])
    if not cbz_path.exists():
        yield f"ERROR:File not found on disk: {cbz_path}"
        return

    if cbz_path.suffix.lower() not in (".cbz", ".cbr"):
        yield f"ERROR:Standardizer only supports CBZ/CBR files, got {cbz_path.suffix}"
        return

    log_lines: list[str] = []

    class _Capture(io.StringIO):
        def write(self, s: str):
            if s.strip():
                log_lines.append(s.rstrip())

    real_stdout = sys.stdout
    sys.stdout = _Capture()

    # Non-interactive mode: resolve name from file / metadata only
    # (no stdin prompt possible in a web context)
    def _no_prompt(prompt: str) -> str:
        log_lines.append(f"  [warn]    Interactive prompt skipped in web mode: {prompt}")
        return ""

    cbz._ask = _no_prompt

    try:
        output_path = cbz.process_cbz(
            cbz_path=cbz_path,
            output_dir=cbz_path.parent,
            csv_mapping=None,
            rename_pages_meta=False,
            webp=webp,
            webp_quality=webp_quality,
            dry_run=False,
            verbose=True,
        )

        sys.stdout = real_stdout

        # Flush captured lines
        for line in log_lines:
            yield line

        # Update DB: new path, cover, file size
        with get_conn() as conn:
            new_size = output_path.stat().st_size if output_path.exists() else None

            # Infer new title / series / volume from standardized filename
            from services.scanner import _guess_metadata
            title, series, volume = _guess_metadata(output_path)

            conn.execute(
                """
                UPDATE books
                SET path = ?, title = ?, series = ?, volume = ?,
                    file_size = ?, date_updated = datetime('now')
                WHERE id = ?
                """,
                (str(output_path), title, series, volume, new_size, book_id),
            )

            # Refresh cover
            cover = extract_cover(output_path, book_id)
            if cover:
                conn.execute(
                    "UPDATE books SET cover_path = ? WHERE id = ?",
                    (str(cover), book_id),
                )

        yield f"DONE:{output_path}"

    except Exception as e:
        sys.stdout = real_stdout
        for line in log_lines:
            yield line
        yield f"ERROR:{e}"
