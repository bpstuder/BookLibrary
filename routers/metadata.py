"""
routers/metadata.py — Metadata CRUD for BookLibrary.

Endpoints
---------
GET  /metadata/sources              List all providers with key/enabled status
GET  /metadata/{book_id}            List all cached metadata rows for a book
POST /metadata/fetch                Scrape from a provider and store results
POST /metadata/{book_id}/pin/{id}   Pin one result as the canonical metadata
POST /metadata/{book_id}/apply/{id} Copy selected fields to the books table
PUT  /metadata/{book_id}/manual     Create/update the manual metadata row
DELETE /metadata/{book_id}/{id}     Delete one cached row
DELETE /metadata/{book_id}          Delete all cached rows for a book
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import db.config as cfg
from db.database import get_conn
from db.models import MetadataApply, MetadataWrite
from services.metadata import (
    _sidecar_path,
    ALL_SOURCES, enabled_sources, fetch_and_store,
    get_cached, pin_metadata, delete_metadata,
    save_manual, apply_to_book,
)

router = APIRouter(prefix="/metadata", tags=["metadata"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class FetchRequest(BaseModel):
    """Trigger a metadata scrape for one book."""
    book_id: int
    source:  str   # one of ALL_SOURCES
    query:   str   # search string sent to the external API


# ---------------------------------------------------------------------------
# Sources info
# ---------------------------------------------------------------------------

@router.get("/sources", summary="List metadata providers")
def get_sources() -> list[dict]:
    """
    Return all known providers with their status:
    - `enabled`   — whether the provider is enabled in Settings
    - `key_set`   — whether an API key is configured (for providers that need one)
    - `requires_key` — whether the provider needs an API key at all
    """
    enabled = enabled_sources()
    source_info: dict[str, dict] = {
        "anilist":     {"label": "AniList",      "requires_key": False, "category": "manga"},
        "comicvine":   {"label": "ComicVine",    "requires_key": True,  "category": "comics"},
        "googlebooks": {"label": "Google Books", "requires_key": False, "category": "books"},
        "hardcover":   {"label": "Hardcover",    "requires_key": True,  "category": "books"},
        "openlib":     {"label": "Open Library", "requires_key": False, "category": "books"},
    }
    return [
        {
            **source_info.get(s, {"label": s, "requires_key": False, "category": "all"}),
            "id":      s,
            "enabled": s in enabled,
            # key_set is True for free providers; for key-required ones check config
            "key_set": (
                bool(cfg.get(f"{s}_api_key", ""))
                if source_info.get(s, {}).get("requires_key")
                else True
            ),
        }
        for s in ALL_SOURCES
    ]


# ---------------------------------------------------------------------------
# List cached rows
# ---------------------------------------------------------------------------

@router.get("/{book_id}", summary="List metadata for a book")
def list_metadata(book_id: int) -> list[dict]:
    """
    Return all cached metadata rows for a book, ordered by:
    pinned first → manual → highest score → most recent fetch.
    """
    return get_cached(book_id)


# ---------------------------------------------------------------------------
# Fetch from external provider
# ---------------------------------------------------------------------------

@router.post("/fetch", summary="Scrape metadata from a provider")
async def fetch_meta(body: FetchRequest) -> dict:
    """
    Fetch up to 10 results from the requested provider, store each as a
    separate `metadata_cache` row (e.g. `anilist_0` … `anilist_9`),
    and return the full updated list for the book.

    Errors:
    - 400 if `source` is not a known provider
    - 403 if the provider is disabled in Settings
    - 404 if `book_id` does not exist
    - 503 if an API key is missing/invalid
    - 502 on any external API error
    """
    if body.source not in ALL_SOURCES:
        raise HTTPException(400, f"Unknown source '{body.source}'. Valid: {ALL_SOURCES}")
    if body.source not in enabled_sources():
        raise HTTPException(403, f"Provider '{body.source}' is disabled in Settings")

    with get_conn() as conn:
        if not conn.execute("SELECT id FROM books WHERE id=?", (body.book_id,)).fetchone():
            raise HTTPException(404, "Book not found")

    try:
        await fetch_and_store(body.book_id, body.source, body.query)
    except RuntimeError as e:
        # RuntimeError is raised when an API key is missing
        raise HTTPException(503, str(e)) from e
    except Exception as e:
        raise HTTPException(502, f"External API error: {e}") from e

    rows = get_cached(body.book_id)
    return {"count": len(rows), "results": rows}


# ---------------------------------------------------------------------------
# Pin
# ---------------------------------------------------------------------------

@router.post("/{book_id}/pin/{metadata_id}", summary="Pin a metadata result")
def pin_meta(book_id: int, metadata_id: int) -> dict:
    """
    Mark one metadata row as pinned (is_pinned=1) and unpin all others.
    The pinned row is used as the canonical source for authors/synopsis
    in the book list and Info tab.
    """
    pin_metadata(book_id, metadata_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Apply to book record
# ---------------------------------------------------------------------------

@router.post("/{book_id}/apply/{metadata_id}", summary="Apply metadata fields to book")
def apply_meta(book_id: int, metadata_id: int, body: MetadataApply) -> list[dict]:
    """
    Copy the selected fields from a cached metadata row to the `books` table.

    - `title`, `series`, `volume` are written directly to `books`
    - All other fields (synopsis, authors, genres…) go to the manual metadata row
    - If `pin=True` (default), also pins this row as the canonical result
    """
    apply_to_book(book_id, metadata_id, body.fields, body.pin)
    return get_cached(book_id)


# ---------------------------------------------------------------------------
# Manual metadata
# ---------------------------------------------------------------------------

@router.put("/{book_id}/manual", summary="Save manual metadata")
def save_manual_meta(book_id: int, body: MetadataWrite) -> dict:
    """
    Create or update the `manual` metadata row for a book.
    Fields already set are preserved unless explicitly overwritten.
    Also syncs `title`/`series`/`volume` to the `books` table if provided.
    """
    data = body.model_dump(exclude_none=True)
    return save_manual(book_id, data)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

@router.delete("/{book_id}/{metadata_id}", status_code=204, summary="Delete one metadata row")
def delete_meta_row(book_id: int, metadata_id: int) -> None:
    """Delete a single cached metadata row by its ID."""
    delete_metadata(book_id, metadata_id)


@router.delete("/{book_id}", status_code=204, summary="Delete all metadata for a book")
def delete_all_meta(book_id: int) -> None:
    """
    Delete all cached metadata rows for a book (including manual entries).
    Also removes the sidecar JSON file if it exists.
    """
    with get_conn() as conn:
        conn.execute("DELETE FROM metadata_cache WHERE book_id = ?", (book_id,))
    _sidecar_path(book_id).unlink(missing_ok=True)
