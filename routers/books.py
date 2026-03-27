"""
routers/books.py — CRUD for the book collection.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

import db.config as cfg
from db.database import get_conn
from db.models import (
    BookOut, BookUpdate,
    MoveRequest, SeriesOut, StatusUpdate, TagOut,
)
from services.covers import extract_cover
from services.metadata import get_cached, _parse_db_row

log = logging.getLogger("manga.books")

router = APIRouter(prefix="/books", tags=["books"])

# Whitelist of book-table columns that may appear in a SET clause
_ALLOWED_BOOK_UPDATE_FIELDS = frozenset({"title", "series", "volume", "type", "category"})

# Characters forbidden in filenames
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---------------------------------------------------------------------------
# List / search
# ---------------------------------------------------------------------------

@router.get("", response_model=list[BookOut])
def list_books(
    q:        Optional[str] = Query(None),
    book_type: Optional[str] = Query(None, alias="type"),
    category:  Optional[str] = Query(None),
    status:   Optional[str] = Query(None),
    series:   Optional[str] = Query(None),
    tag:      Optional[str] = Query(None),
    sort:     str = Query("title"),
    order:    str = Query("asc"),
    limit:    int = Query(50, le=500),
    offset:   int = Query(0),
):
    """List and filter books with optional search, sort and pagination."""
    allowed_sorts = {"title", "series", "date_added", "volume", "category"}
    sort  = sort if sort in allowed_sorts else "title"
    order = "ASC" if order.lower() != "desc" else "DESC"

    conditions, params = [], []

    if q:
        conditions.append("(b.title LIKE ? OR b.series LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if book_type:
        conditions.append("b.type = ?")
        params.append(book_type)
    if category:
        conditions.append("b.category = ?")
        params.append(category)
    if series:
        conditions.append("b.series LIKE ?")
        params.append(f"%{series}%")
    if status:
        conditions.append("rs.status = ?")
        params.append(status)
    if tag:
        conditions.append(
            "EXISTS (SELECT 1 FROM book_tags bt JOIN tags t ON bt.tag_id=t.id "
            "WHERE bt.book_id=b.id AND t.name=?)"
        )
        params.append(tag)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    # Join best metadata row: pinned first, then manual, then highest score, then most recent
    # When sorting by series, always use volume ASC as secondary key so tomes
    # within a series appear in the correct reading order regardless of the
    # direction of the primary sort.
    secondary_sort = ", b.volume ASC NULLS LAST" if sort == "series" else ""

    sql = f"""
        SELECT b.*,
               rs.status, rs.progress, rs.last_read,
               GROUP_CONCAT(DISTINCT t.name) AS tag_list,
               COALESCE(mc.authors, '[]')    AS meta_authors,
               mc.synopsis                   AS meta_synopsis
        FROM books b
        LEFT JOIN reading_status rs ON rs.book_id = b.id
        LEFT JOIN book_tags bt ON bt.book_id = b.id
        LEFT JOIN tags t ON t.id = bt.tag_id
        LEFT JOIN (
            SELECT book_id,
                   authors,
                   synopsis,
                   ROW_NUMBER() OVER (
                       PARTITION BY book_id
                       ORDER BY is_pinned   DESC,
                                is_manual   DESC,
                                COALESCE(score, 0) DESC,
                                fetched_at  DESC
                   ) AS rn
            FROM metadata_cache
        ) mc ON mc.book_id = b.id AND mc.rn = 1
        {where}
        GROUP BY b.id
        ORDER BY b.{sort} {order}{secondary_sort}
        LIMIT ? OFFSET ?
    """
    params += [limit, offset]

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_book(r) for r in rows]


# ---------------------------------------------------------------------------
# Series grouping
# ---------------------------------------------------------------------------

@router.get("/series", response_model=list[SeriesOut])
def list_series(category: Optional[str] = Query(None)):
    """List all series grouped by name with volume counts and status."""
    conditions, params = ["b.series IS NOT NULL AND b.series != ''"], []
    if category:
        conditions.append("b.category = ?")
        params.append(category)
    where = "WHERE " + " AND ".join(conditions)

    sql = f"""
        SELECT b.series,
               b.category,
               COUNT(b.id)            AS cnt,
               MIN(b.id)              AS cover_id,
               GROUP_CONCAT(rs.status) AS status_list
        FROM books b
        LEFT JOIN reading_status rs ON rs.book_id = b.id
        {where}
        GROUP BY b.series
        ORDER BY b.series ASC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    result = []
    for r in rows:
        statuses: dict = {"unread": 0, "reading": 0, "read": 0}
        for s in (r["status_list"] or "").split(","):
            s = s.strip()
            if s in statuses:
                statuses[s] += 1
        result.append(SeriesOut(
            series=r["series"],
            category=r["category"] or "unknown",
            count=r["cnt"],
            cover_id=r["cover_id"],
            statuses=statuses,
        ))
    return result


# ---------------------------------------------------------------------------
# Single book
# ---------------------------------------------------------------------------

@router.get("/stats/summary")
def get_stats():
    """Return aggregate statistics by type, category and reading status."""
    with get_conn() as conn:
        total    = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        by_type  = {r["type"]: r["cnt"] for r in conn.execute(
            "SELECT type, COUNT(*) AS cnt FROM books GROUP BY type").fetchall()}
        by_cat   = {r["category"]: r["cnt"] for r in conn.execute(
            "SELECT category, COUNT(*) AS cnt FROM books GROUP BY category").fetchall()}
        by_status = {r["status"]: r["cnt"] for r in conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM reading_status GROUP BY status").fetchall()}
    return {"total": total, "by_type": by_type, "by_category": by_cat, "by_status": by_status}


@router.get("/tags/all", response_model=list[TagOut])
def list_all_tags():
    """Return all tags in alphabetical order."""
    with get_conn() as conn:
        rows = conn.execute("SELECT id, name FROM tags ORDER BY name").fetchall()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


@router.get("/{book_id}", response_model=BookOut)
def get_book(book_id: int):
    """Return a single book by id, 404 if not found."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT b.*, rs.status, rs.progress, rs.last_read,
                   GROUP_CONCAT(DISTINCT t.name) AS tag_list,
                   COALESCE(mc.authors, '[]')    AS meta_authors,
                   mc.synopsis                  AS meta_synopsis
            FROM books b
            LEFT JOIN reading_status rs ON rs.book_id = b.id
            LEFT JOIN book_tags bt ON bt.book_id = b.id
            LEFT JOIN tags t ON t.id = bt.tag_id
            LEFT JOIN (
                SELECT book_id, authors, synopsis,
                       ROW_NUMBER() OVER (
                           PARTITION BY book_id
                           ORDER BY is_pinned   DESC,
                                    is_manual   DESC,
                                    COALESCE(score, 0) DESC,
                                    fetched_at  DESC
                       ) AS rn
                FROM metadata_cache
            ) mc ON mc.book_id = b.id AND mc.rn = 1
            WHERE b.id = ?
            GROUP BY b.id
            """, (book_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Book not found")
    return _row_to_book(row)


@router.patch("/{book_id}", response_model=BookOut)
def update_book(book_id: int, body: BookUpdate):
    """Update book fields (title, series, volume, type, category)."""
    fields = {
        k: v for k, v in body.model_dump(exclude_none=True).items()
        if k in _ALLOWED_BOOK_UPDATE_FIELDS
    }
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to update")
    log.debug("books: PATCH id=%d — %s", book_id, fields)
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE books SET {set_clause}, date_updated = datetime('now') WHERE id = ?",
            [*fields.values(), book_id],
        )
    return get_book(book_id)


@router.delete("/{book_id}", status_code=204)
def delete_book(book_id: int):
    """Delete a book record (does not remove the file from disk)."""
    log.debug("books: DELETE id=%d", book_id)
    with get_conn() as conn:
        conn.execute("DELETE FROM books WHERE id = ?", (book_id,))


# ---------------------------------------------------------------------------
# Cover
# ---------------------------------------------------------------------------

@router.get("/{book_id}/cover")
def get_cover(book_id: int):
    """Serve the cover thumbnail for a book."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT cover_path FROM books WHERE id = ?", (book_id,)
        ).fetchone()
    if not row or not row["cover_path"]:
        raise HTTPException(status_code=404, detail="No cover available")
    p = Path(row["cover_path"])
    if not p.exists():
        raise HTTPException(status_code=404, detail="Cover file missing on disk")
    return FileResponse(p, media_type="image/jpeg")


# ---------------------------------------------------------------------------
# Reading status
# ---------------------------------------------------------------------------

@router.put("/{book_id}/status", response_model=BookOut)
def set_status(book_id: int, body: StatusUpdate):
    """Set reading status and optional page progress."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO reading_status (book_id, status, progress, last_read)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(book_id) DO UPDATE SET
                status    = excluded.status,
                progress  = excluded.progress,
                last_read = excluded.last_read
            """,
            (book_id, body.status, body.progress),
        )
    return get_book(book_id)


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

@router.post("/{book_id}/tags/{tag_name}", status_code=201)
def add_tag(book_id: int, tag_name: str):
    """Add a tag to a book, creating it if needed."""
    tag_name = tag_name.strip().lower()
    if not tag_name:
        raise HTTPException(status_code=400, detail="Tag name cannot be empty")
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,))
        tag_id = conn.execute(
            "SELECT id FROM tags WHERE name = ?", (tag_name,)
        ).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO book_tags (book_id, tag_id) VALUES (?, ?)",
            (book_id, tag_id),
        )
    return {"ok": True, "tag": tag_name}


@router.delete("/{book_id}/tags/{tag_name}", status_code=204)
def remove_tag(book_id: int, tag_name: str):
    """Remove a tag from a book."""
    with get_conn() as conn:
        tag = conn.execute(
            "SELECT id FROM tags WHERE name = ?", (tag_name.strip().lower(),)
        ).fetchone()
        if tag:
            conn.execute(
                "DELETE FROM book_tags WHERE book_id = ? AND tag_id = ?",
                (book_id, tag["id"]),
            )


# ---------------------------------------------------------------------------
# Metadata — thin wrappers that redirect to /metadata/* routes
# (kept for backward compatibility with the frontend calling /books/{id}/metadata)
# ---------------------------------------------------------------------------

@router.get("/{book_id}/metadata")
def get_book_metadata(book_id: int):
    """Return all cached metadata rows for a book (backward compat alias)."""
    return get_cached(book_id)


# ---------------------------------------------------------------------------
# Move / rename file
# ---------------------------------------------------------------------------

class MovePreviewRequest(BaseModel):
    """Request body for move preview (dry-run)."""
    pattern: str


@router.post("/{book_id}/move/preview")
def preview_move(book_id: int, body: MovePreviewRequest):
    """
    Return what the destination path would be without actually moving the file.
    """
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Book not found")

    book    = dict(row)
    src     = Path(book["path"])
    library = Path(cfg.get("library_path", "./library")).expanduser().resolve()

    variables = _build_move_vars(book, src)
    try:
        rel_path = body.pattern.format(**variables)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid pattern: {e}") from e

    dest = (library / rel_path).with_suffix(src.suffix)
    return {
        "source":      str(src),
        "destination": str(dest),
        "pattern":     body.pattern,
        "variables":   {k: str(v) for k, v in variables.items()},
    }


@router.post("/{book_id}/move", response_model=BookOut)
def move_book(book_id: int, body: MoveRequest):
    """
    Rename and/or move the book file according to a pattern.

    Pattern variables: {series} {title} {volume} {category} {type}
    Example: "{series}/{title}"  → Manga/One Piece - T01.cbz

    The path is relative to the library root (from config).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM books WHERE id = ?", (book_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Book not found")

    book    = dict(row)
    src     = Path(book["path"])
    library = Path(cfg.get("library_path", "./library")).expanduser().resolve()

    if not src.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {src}")

    # Build destination path from pattern
    variables = _build_move_vars(book, src)
    try:
        rel_path = body.pattern.format(**variables)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid pattern: {e}") from e

    dest = (library / rel_path).with_suffix(src.suffix)

    if dest == src:
        return get_book(book_id)

    if dest.exists():
        raise HTTPException(
            status_code=409, detail=f"Destination already exists: {dest}"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    log.debug("books: move id=%d  %s → %s", book_id, src.name, dest)
    shutil.copy2(src, dest)

    # Update DB
    new_size = dest.stat().st_size
    cover    = extract_cover(dest, book_id)

    with get_conn() as conn:
        conn.execute(
            """
            UPDATE books
            SET path = ?, file_size = ?, cover_path = ?,
                date_updated = datetime('now')
            WHERE id = ?
            """,
            (str(dest), new_size, str(cover) if cover else book.get("cover_path"), book_id),
        )

    if body.delete_old:
        try:
            src.unlink()
        except Exception:  # pylint: disable=broad-exception-caught
            pass  # non-fatal

    return get_book(book_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_move_vars(book: dict, src: "Path") -> dict:
    """Build pattern variables for move/rename — volume kept as int for {volume:02d}."""
    title = _strip_volume_suffix(book.get("title") or src.stem)
    return {
        "series":   _sanitize(book.get("series") or "Unknown Series"),
        "title":    _sanitize(title),
        "volume":   book.get("volume") or 0,   # int: supports {volume:02d} in pattern
        "category": _sanitize(book.get("category") or "unknown"),
        "type":     book.get("type") or src.suffix.lstrip("."),
    }


def _strip_volume_suffix(title: str) -> str:
    """
    Remove trailing volume indicators from a title.
    Examples:
      "Dragon Ball Super T10"  → "Dragon Ball Super"
      "One Piece - T01"        → "One Piece"
      "Naruto Vol. 5"          → "Naruto"
    """
    t = re.sub(r'[\s\-_]*[Tt](?:ome|om)?\s*\d+\s*$', '', title)
    t = re.sub(r'[\s\-_]*(?:vol|volume|v)\.?\s*\d+\s*$', '', t, flags=re.IGNORECASE)
    return t.strip(" -_") or title


def _row_to_book(row) -> BookOut:
    """Convert a raw SQLite row (with joined metadata columns) to a BookOut model."""
    d = dict(row)
    d["tags"]     = [t for t in (d.pop("tag_list", "") or "").split(",") if t]
    d["authors"]  = json.loads(d.pop("meta_authors",  None) or "[]")
    d["synopsis"] = d.pop("meta_synopsis", None)
    if "category" not in d:
        d["category"] = "unknown"
    return BookOut(**d)


def _parse_meta_row(row) -> dict:
    """Parse a raw DB row into a metadata dict."""
    return _parse_db_row(row)


def _sanitize(name: str) -> str:
    return _FORBIDDEN.sub("_", name).strip().strip(".")
