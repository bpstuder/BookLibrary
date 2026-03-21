"""
routers/library.py — Library scan and CBZ/CBR conversion (SSE streaming).

Endpoints
---------
POST /scan                          Scan the library folder and sync the DB
POST /books/{book_id}/standardize   Convert a CBZ/CBR file (WebP, cleanup, etc.)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

import db.config as cfg
from db.models import ScanResult, StandardizeRequest
from services.scanner import scan_library
from services.standardizer import standardize_book

router = APIRouter(tags=["library"])


def _library_path() -> Path:
    """Resolve the library path from live config — always current, never cached at import."""
    return Path(cfg.get("library_path", "./library")).expanduser().resolve()


# ---------------------------------------------------------------------------
# Library scan
# ---------------------------------------------------------------------------

@router.post("/scan", response_model=ScanResult, summary="Scan library folder")
def trigger_scan() -> ScanResult:
    """
    Walk the library directory recursively, then:
    - Insert new files into the `books` table
    - Remove DB entries whose files have been deleted
    - Extract cover thumbnails for new books

    Returns a summary with counts of added/removed/errored files.
    """
    library = _library_path()
    if not library.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Library path does not exist: {library}",
        )
    return scan_library(library)


# ---------------------------------------------------------------------------
# CBZ conversion — SSE stream
# ---------------------------------------------------------------------------

@router.post("/books/{book_id}/standardize", summary="Convert CBZ/CBR file")
def standardize(book_id: int, body: StandardizeRequest):
    """
    Convert a CBZ/CBR file in-place:
    1. Extract archive to a temp folder
    2. Flatten nested image directories
    3. Remove non-image files (macOS `._*` sidecars, XML, etc.)
    4. Optionally convert images to WebP
    5. Repack as CBZ

    Progress is streamed via **Server-Sent Events** (SSE):
    - `event: log`   — a log line (string)
    - `event: done`  — final success message (new file path)
    - `event: error` — error message if the conversion failed

    Only CBZ and CBR files are supported.
    """
    async def event_stream():
        loop = asyncio.get_event_loop()

        # Run the blocking standardize_book generator in a thread pool
        def _run() -> list[str]:
            return list(standardize_book(
                book_id=book_id,
                webp=body.webp,
                webp_quality=body.webp_quality,
                delete_old=body.delete_old,
            ))

        lines = await loop.run_in_executor(None, _run)

        for line in lines:
            if line.startswith("DONE:"):
                yield f"event: done\ndata: {line[5:]}\n\n"
            elif line.startswith("ERROR:"):
                yield f"event: error\ndata: {line[6:]}\n\n"
            else:
                yield f"event: log\ndata: {line}\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
