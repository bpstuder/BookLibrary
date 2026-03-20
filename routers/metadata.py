"""
routers/metadata.py — Trigger metadata scraping from external APIs.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.metadata import fetch_and_store, get_cached
from db.database import get_conn

router = APIRouter(prefix="/metadata", tags=["metadata"])

SOURCES = ("comicvine", "googlebooks", "anilist")


class FetchRequest(BaseModel):
    book_id: int
    source: str
    query: str  # search string sent to the API


@router.post("/fetch")
async def fetch_metadata(body: FetchRequest):
    if body.source not in SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown source '{body.source}'. Valid: {SOURCES}",
        )

    # Verify book exists
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM books WHERE id = ?", (body.book_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Book not found")

    try:
        result = await fetch_and_store(body.book_id, body.source, body.query)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"API error: {e}")

    return result


@router.get("/{book_id}")
def get_metadata(book_id: int):
    return get_cached(book_id)
