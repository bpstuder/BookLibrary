"""
services/standardizer.py — Wrap cbz_standardize.process_cbz for FastAPI.

Yields log lines as strings (for SSE streaming).
After success: updates the DB, refreshes cover, optionally deletes original.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path
from typing import Generator

import cbz_standardize as cbz
from db.database import get_conn
from services.covers import extract_cover


def standardize_book(
    book_id:     int,
    webp:        bool = False,
    webp_quality: int = 85,
    delete_old:  bool = False,
) -> Generator[str, None, None]:
    """
    Generator: yields log lines, then 'DONE:<path>' or 'ERROR:<message>'.
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
        yield f"ERROR:Standardizer only supports CBZ/CBR (got {cbz_path.suffix})"
        return

    log_lines: list[str] = []

    class _Capture(io.StringIO):
        def write(self, s: str):
            if s.strip():
                log_lines.append(s.rstrip())
        def flush(self): pass

    def _no_prompt(prompt: str) -> str:
        log_lines.append("  [warn]    Skipping interactive prompt in web mode")
        return ""

    cbz._ask = _no_prompt

    try:
        # redirect_stdout is a context manager: stdout is always restored on exit,
        # even if an exception is raised — safe under concurrent requests.
        with redirect_stdout(_Capture()):
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

        for line in log_lines:
            yield line

        # Update DB
        new_size = output_path.stat().st_size if output_path.exists() else None
        from services.scanner import _guess_metadata
        title, series, volume = _guess_metadata(output_path)

        with get_conn() as conn:
            conn.execute(
                """
                UPDATE books
                SET path = ?, title = ?, series = ?, volume = ?,
                    file_size = ?, date_updated = datetime('now')
                WHERE id = ?
                """,
                (str(output_path), title, series, volume, new_size, book_id),
            )
            cover = extract_cover(output_path, book_id)
            if cover:
                conn.execute(
                    "UPDATE books SET cover_path = ? WHERE id = ?",
                    (str(cover), book_id),
                )

        # Delete original if requested and different from output
        if delete_old and output_path != cbz_path and cbz_path.exists():
            try:
                cbz_path.unlink()
                yield f"  [delete]  Original file removed: {cbz_path.name}"
            except Exception as e:
                yield f"  [warn]    Could not delete original: {e}"

        yield f"DONE:{output_path}"

    except Exception as e:
        for line in log_lines:
            yield line
        yield f"ERROR:{e}"
