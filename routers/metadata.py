"""
routers/metadata.py — Full metadata CRUD.

GET  /metadata/{book_id}                — list all rows
POST /metadata/fetch                    — scrape from provider
POST /metadata/{book_id}/pin/{id}       — pin a result
POST /metadata/{book_id}/apply/{id}     — apply fields to book
PUT  /metadata/{book_id}/manual         — save manual metadata
DELETE /metadata/{book_id}/{id}         — delete a row
DELETE /metadata/{book_id}              — delete all rows for book
GET  /metadata/sources                  — list providers + enabled status
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

import db.config as cfg
from db.database import get_conn
from db.models import MetadataApply, MetadataWrite, MetadataSaveRequest
from services.metadata import (
    ALL_SOURCES, enabled_sources, fetch_and_store,
    get_cached, pin_metadata, delete_metadata,
    save_manual, apply_to_book,
)

router = APIRouter(prefix="/metadata", tags=["metadata"])


class FetchRequest(BaseModel):
    book_id: int
    source:  str
    query:   str


# ---------------------------------------------------------------------------
# Sources info
# ---------------------------------------------------------------------------

@router.get("/sources")
def get_sources():
    """Return all providers with enabled/disabled status and key status."""
    enabled = enabled_sources()
    source_info = {
        "anilist":     {"label": "AniList",     "requires_key": False,  "category": "manga"},
        "comicvine":   {"label": "ComicVine",   "requires_key": True,   "category": "comics"},
        "googlebooks": {"label": "Google Books","requires_key": False,  "category": "books"},
        "hardcover":   {"label": "Hardcover",   "requires_key": True,   "category": "books"},
        "openlib":     {"label": "Open Library","requires_key": False,  "category": "books"},
    }
    return [
        {
            **source_info.get(s, {"label": s, "requires_key": False, "category": "all"}),
            "id":       s,
            "enabled":  s in enabled,
            "key_set":  bool(cfg.get(f"{s}_api_key", "")) if source_info.get(s, {}).get("requires_key") else True,
        }
        for s in ALL_SOURCES
    ]


# ---------------------------------------------------------------------------
# List metadata for a book
# ---------------------------------------------------------------------------

@router.get("/{book_id}")
def list_metadata(book_id: int):
    return get_cached(book_id)


# ---------------------------------------------------------------------------
# Fetch from provider
# ---------------------------------------------------------------------------

@router.post("/fetch")
async def fetch_meta(body: FetchRequest):
    if body.source not in ALL_SOURCES:
        raise HTTPException(400, f"Unknown source '{body.source}'")
    if body.source not in enabled_sources():
        raise HTTPException(403, f"Provider '{body.source}' is disabled in Settings")

    with get_conn() as conn:
        if not conn.execute("SELECT id FROM books WHERE id=?", (body.book_id,)).fetchone():
            raise HTTPException(404, "Book not found")

    try:
        results = await fetch_and_store(body.book_id, body.source, body.query)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"API error: {e}")

    return {"count": len(results), "results": get_cached(body.book_id)}


# ---------------------------------------------------------------------------
# Pin a result
# ---------------------------------------------------------------------------

@router.post("/{book_id}/pin/{metadata_id}")
def pin_meta(book_id: int, metadata_id: int):
    pin_metadata(book_id, metadata_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Apply fields to the book record
# ---------------------------------------------------------------------------

@router.post("/{book_id}/apply/{metadata_id}")
def apply_meta(book_id: int, metadata_id: int, body: MetadataApply):
    apply_to_book(book_id, metadata_id, body.fields, body.pin)
    return get_cached(book_id)


# ---------------------------------------------------------------------------
# Save manual metadata
# ---------------------------------------------------------------------------

@router.put("/{book_id}/manual")
def save_manual_meta(book_id: int, body: MetadataWrite):
    data = body.model_dump(exclude_none=True)
    row  = save_manual(book_id, data)
    return row


# ---------------------------------------------------------------------------
# Delete one row
# ---------------------------------------------------------------------------

@router.delete("/{book_id}/{metadata_id}", status_code=204)
def delete_meta_row(book_id: int, metadata_id: int):
    delete_metadata(book_id, metadata_id)


# ---------------------------------------------------------------------------
# Delete ALL rows for a book (reset)
# ---------------------------------------------------------------------------

@router.delete("/{book_id}", status_code=204)
def delete_all_meta(book_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM metadata_cache WHERE book_id = ?", (book_id,))
    from services.metadata import _sidecar_path
    sp = _sidecar_path(book_id)
    if sp.exists():
        sp.unlink()
