"""
routers/books.py — CRUD for the book collection.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from db.database import get_conn
from db.models import (
    BookOut, BookUpdate,
    MoveRequest, SeriesOut, StatusUpdate, TagOut,
)

router = APIRouter(prefix="/books", tags=["books"])

# Characters forbidden in filenames
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---------------------------------------------------------------------------
# List / search
# ---------------------------------------------------------------------------

@router.get("", response_model=list[BookOut])
def list_books(
    q:        Optional[str] = Query(None),
    type:     Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    status:   Optional[str] = Query(None),
    series:   Optional[str] = Query(None),
    tag:      Optional[str] = Query(None),
    sort:     str = Query("title"),
    order:    str = Query("asc"),
    limit:    int = Query(50, le=500),
    offset:   int = Query(0),
):
    allowed_sorts = {"title", "series", "date_added", "volume", "category"}
    sort  = sort if sort in allowed_sorts else "title"
    order = "ASC" if order.lower() != "desc" else "DESC"

    conditions, params = [], []

    if q:
        conditions.append("(b.title LIKE ? OR b.series LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if type:
        conditions.append("b.type = ?")
        params.append(type)
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
    # Join best metadata row (manual first, then highest score, then most recent)
    sql = f"""
        SELECT b.*,
               rs.status, rs.progress, rs.last_read,
               GROUP_CONCAT(DISTINCT t.name) AS tag_list,
               COALESCE(mc.authors, '[]')    AS meta_authors,
               mc.synopsis                  AS meta_synopsis
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
                       ORDER BY CASE WHEN source='manual' THEN 0 ELSE 1 END,
                                COALESCE(score, 0) DESC,
                                fetched_at DESC
                   ) AS rn
            FROM metadata_cache
            WHERE authors IS NOT NULL AND authors != '[]'
        ) mc ON mc.book_id = b.id AND mc.rn = 1
        {where}
        GROUP BY b.id
        ORDER BY b.{sort} {order}
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
    with get_conn() as conn:
        rows = conn.execute("SELECT id, name FROM tags ORDER BY name").fetchall()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


@router.get("/{book_id}", response_model=BookOut)
def get_book(book_id: int):
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
                           ORDER BY CASE WHEN source='manual' THEN 0 ELSE 1 END,
                                    COALESCE(score, 0) DESC,
                                    fetched_at DESC
                       ) AS rn
                FROM metadata_cache
                WHERE authors IS NOT NULL AND authors != '[]'
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
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to update")
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE books SET {set_clause}, date_updated = datetime('now') WHERE id = ?",
            [*fields.values(), book_id],
        )
    return get_book(book_id)


@router.delete("/{book_id}", status_code=204)
def delete_book(book_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM books WHERE id = ?", (book_id,))


# ---------------------------------------------------------------------------
# Cover
# ---------------------------------------------------------------------------

@router.get("/{book_id}/cover")
def get_cover(book_id: int):
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
    from services.metadata import get_cached
    return get_cached(book_id)


# ---------------------------------------------------------------------------
# Move / rename file
# ---------------------------------------------------------------------------

class MovePreviewRequest(BaseModel):
    pattern: str


@router.post("/{book_id}/move/preview")
def preview_move(book_id: int, body: MovePreviewRequest):
    """
    Return what the destination path would be without actually moving the file.
    """
    import db.config as cfg

    with get_conn() as conn:
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Book not found")

    book    = dict(row)
    src     = Path(book["path"])
    library = Path(cfg.get("library_path", "./library")).expanduser().resolve()

    variables = {
        "series":   _sanitize(book.get("series") or "Unknown Series"),
        "title":    _sanitize(book.get("title")  or src.stem),
        "volume":   f"{book.get('volume') or 0:02d}",
        "category": _sanitize(book.get("category") or "unknown"),
        "type":     book.get("type") or src.suffix.lstrip("."),
    }
    try:
        rel_path = body.pattern.format(**variables)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Unknown pattern variable: {e}")

    dest = (library / rel_path).with_suffix(src.suffix)
    return {
        "source":      str(src),
        "destination": str(dest),
        "pattern":     body.pattern,
        "variables":   variables,
    }


@router.post("/{book_id}/move", response_model=BookOut)
def move_book(book_id: int, body: MoveRequest):
    """
    Rename and/or move the book file according to a pattern.

    Pattern variables: {series} {title} {volume} {category} {type}
    Example: "{series}/{title}"  → Manga/One Piece - T01.cbz

    The path is relative to the library root (from config).
    """
    import db.config as cfg

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
    variables = {
        "series":   _sanitize(book.get("series") or "Unknown Series"),
        "title":    _sanitize(book.get("title")  or src.stem),
        "volume":   f"{book.get('volume') or 0:02d}",
        "category": _sanitize(book.get("category") or "unknown"),
        "type":     book.get("type") or src.suffix.lstrip("."),
    }
    try:
        rel_path = body.pattern.format(**variables)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Unknown pattern variable: {e}")

    dest = (library / rel_path).with_suffix(src.suffix)

    if dest == src:
        return get_book(book_id)

    if dest.exists():
        raise HTTPException(
            status_code=409, detail=f"Destination already exists: {dest}"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)

    # Update DB
    from services.covers import extract_cover
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
        except Exception:
            pass  # non-fatal

    return get_book(book_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_book(row) -> BookOut:
    import json as _json
    d = dict(row)
    d["tags"]     = [t for t in (d.pop("tag_list", "") or "").split(",") if t]
    d["authors"]  = _json.loads(d.pop("meta_authors",  None) or "[]")
    d["synopsis"] = d.pop("meta_synopsis", None)
    if "category" not in d:
        d["category"] = "unknown"
    return BookOut(**d)


def _parse_meta_row(row) -> dict:
    from services.metadata import _parse_db_row
    return _parse_db_row(row)


def _sanitize(name: str) -> str:
    return _FORBIDDEN.sub("_", name).strip().strip(".")
