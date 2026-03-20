"""
routers/library.py — Library scan and CBZ standardizer (SSE streaming).
"""

import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from db.models import ScanResult
from services.scanner import scan_library
from services.standardizer import standardize_book

router = APIRouter(tags=["library"])

LIBRARY_PATH = Path(os.getenv("LIBRARY_PATH", "./library"))


# ---------------------------------------------------------------------------
# Library scan
# ---------------------------------------------------------------------------

@router.post("/scan", response_model=ScanResult)
def trigger_scan():
    """Scan LIBRARY_PATH and sync the database."""
    if not LIBRARY_PATH.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Library path does not exist: {LIBRARY_PATH}",
        )
    return scan_library(LIBRARY_PATH)


# ---------------------------------------------------------------------------
# CBZ Standardizer — SSE stream
# ---------------------------------------------------------------------------

class StandardizeRequest(BaseModel):
    webp: bool = False
    webp_quality: int = 85


@router.post("/books/{book_id}/standardize")
def standardize(book_id: int, body: StandardizeRequest):
    """
    Stream CBZ standardization logs via Server-Sent Events.
    Final event is either 'done' or 'error'.
    """

    async def event_stream():
        loop = asyncio.get_event_loop()

        def _run():
            return list(
                standardize_book(
                    book_id=book_id,
                    webp=body.webp,
                    webp_quality=body.webp_quality,
                )
            )

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
