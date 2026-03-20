"""
routers/library.py — Library scan and CBZ standardizer (SSE streaming).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

import db.config as cfg
from db.models import ScanResult, StandardizeRequest
from services.scanner import scan_library
from services.standardizer import standardize_book

router = APIRouter(tags=["library"])


def _library_path() -> Path:
    """Always read from live config (can be changed via Settings page)."""
    return Path(cfg.get("library_path", "./library")).expanduser().resolve()


# ---------------------------------------------------------------------------
# Library scan
# ---------------------------------------------------------------------------

@router.post("/scan", response_model=ScanResult)
def trigger_scan():
    library = _library_path()
    if not library.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Library path does not exist: {library}",
        )
    return scan_library(library)


# ---------------------------------------------------------------------------
# CBZ Standardizer — SSE stream
# ---------------------------------------------------------------------------

@router.post("/books/{book_id}/standardize")
def standardize(book_id: int, body: StandardizeRequest):
    """
    Stream CBZ standardization logs via Server-Sent Events.
    Supports delete_old to remove the original file after processing.
    Final event: 'done' or 'error'.
    """

    async def event_stream():
        loop = asyncio.get_event_loop()

        def _run():
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
