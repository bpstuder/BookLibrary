"""
routers/library.py — Library scan and CBZ/CBR conversion (SSE streaming).

Endpoints
---------
POST /scan                          Start a scan (SSE stream)
DELETE /scan                        Cancel the running scan
POST /books/{book_id}/standardize   Convert a CBZ/CBR file (SSE stream)
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

import db.config as cfg
from db.models import StandardizeRequest
from services.scanner import scan_library_stream
from services.standardizer import standardize_book

router = APIRouter(tags=["library"])


# ---------------------------------------------------------------------------
# Scan state — one scan at a time (per process)
# Using a plain object avoids module-level mutable globals and the
# 'global' statement warnings they require.
# ---------------------------------------------------------------------------

class _ScanState:
    """Mutable scan lifecycle state, shared across requests in the same process."""
    lock        = threading.Lock()
    cancel_flag = threading.Event()
    active      = False


def _library_path() -> Path:
    """Resolve the library path from live config."""
    return Path(cfg.get("library_path", "./library")).expanduser().resolve()


# ---------------------------------------------------------------------------
# Scan — SSE stream
# ---------------------------------------------------------------------------

@router.post("/scan", summary="Scan library folder (SSE)")
def trigger_scan():
    """
    Walk the library directory and sync it with the database.
    Streams progress via SSE:

    - `event: count`    — `{"total": N}` — files discovered
    - `event: progress` — `{"done": N, "total": N, "file": "...", "action": "added"|"skip"|"error"}`
    - `event: removed`  — `{"count": N}` — orphaned DB entries deleted
    - `event: done`     — `{"added": N, "removed": N, "errors": [...]}` — scan complete
    - `event: cancelled`— scan was aborted via DELETE /scan
    - `event: error`    — fatal error
    """
    with _ScanState.lock:
        if _ScanState.active:
            raise HTTPException(status_code=409, detail="A scan is already running")
        _ScanState.active = True
        _ScanState.cancel_flag.clear()

    library = _library_path()
    if not library.exists():
        _ScanState.active = False
        raise HTTPException(404, f"Library path does not exist: {library}")

    async def event_stream():
        loop = asyncio.get_event_loop()

        queue: asyncio.Queue = asyncio.Queue()

        def _run():
            """Run in thread pool — puts each event into the queue as it arrives."""
            try:
                for ev in scan_library_stream(library, cancel_event=_ScanState.cancel_flag):
                    loop.call_soon_threadsafe(queue.put_nowait, ev)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                loop.call_soon_threadsafe(
                    queue.put_nowait, {"type": "error", "msg": str(exc)}
                )
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)  # sentinel

        future = loop.run_in_executor(None, _run)

        try:
            while True:
                ev = await queue.get()
                if ev is None:   # sentinel — scan thread finished
                    break
                etype = ev.get("type", "log")
                yield f"event: {etype}\ndata: {json.dumps(ev)}\n\n"
        finally:
            _ScanState.active = False
            _ScanState.cancel_flag.clear()
            await asyncio.shield(future)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.delete("/scan", status_code=204, summary="Cancel running scan")
def cancel_scan():
    """Signal the running scan to stop after the current file."""
    if not _ScanState.active:
        raise HTTPException(status_code=404, detail="No scan is currently running")
    _ScanState.cancel_flag.set()


# ---------------------------------------------------------------------------
# CBZ conversion — SSE stream
# ---------------------------------------------------------------------------

@router.post("/books/{book_id}/standardize", summary="Convert CBZ/CBR file")
def standardize(book_id: int, body: StandardizeRequest):
    """
    Convert a CBZ/CBR file in-place (flatten, cleanup, optional WebP, repack).
    Streams progress via SSE: log | done | error.
    """
    async def event_stream():
        loop = asyncio.get_event_loop()

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
