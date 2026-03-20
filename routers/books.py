"""
routers/books.py — CRUD for the book collection.
"""

import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from db.database import get_conn
from db.models import BookFilters, BookOut, BookUpdate, StatusUpdate, TagOut

router = APIRouter(prefix="/books", tags=["books"])


# ---------------------------------------------------------------------------
# List / search
# ---------------------------------------------------------------------------

@router.get("", response_model=list[BookOut])
def list_books(
    q: Optional[str] = Query(None, description="Search title or series"),
    type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    series: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    sort: str = Query("title"),
    order: str = Query("asc"),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
):
    allowed_sorts = {"title", "series", "date_added", "volume"}
    sort = sort if sort in allowed_sorts else "title"
    order = "ASC" if order.lower() != "desc" else "DESC"

    conditions = []
    params: list = []

    if q:
        conditions.append("(b.title LIKE ? OR b.series LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if type:
        conditions.append("b.type = ?")
        params.append(type)
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

    sql = f"""
        SELECT b.*,
               rs.status, rs.progress, rs.last_read,
               GROUP_CONCAT(DISTINCT t.name) AS tag_list
        FROM books b
        LEFT JOIN reading_status rs ON rs.book_id = b.id
        LEFT JOIN book_tags bt ON bt.book_id = b.id
        LEFT JOIN tags t ON t.id = bt.tag_id
        {where}
        GROUP BY b.id
        ORDER BY b.{sort} {order}
        LIMIT ? OFFSET ?
    """
    params += [limit, offset]

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    return [_row_to_book(row) for row in rows]


# ---------------------------------------------------------------------------
# Single book
# ---------------------------------------------------------------------------

@router.get("/{book_id}", response_model=BookOut)
def get_book(book_id: int):
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT b.*, rs.status, rs.progress, rs.last_read,
                   GROUP_CONCAT(DISTINCT t.name) AS tag_list
            FROM books b
            LEFT JOIN reading_status rs ON rs.book_id = b.id
            LEFT JOIN book_tags bt ON bt.book_id = b.id
            LEFT JOIN tags t ON t.id = bt.tag_id
            WHERE b.id = ?
            GROUP BY b.id
            """,
            (book_id,),
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
    values = list(fields.values()) + [book_id]

    with get_conn() as conn:
        conn.execute(
            f"UPDATE books SET {set_clause}, date_updated = datetime('now') WHERE id = ?",
            values,
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

    cover_path = Path(row["cover_path"])
    if not cover_path.exists():
        raise HTTPException(status_code=404, detail="Cover file missing on disk")

    return FileResponse(cover_path, media_type="image/jpeg")


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

@router.get("/tags/all", response_model=list[TagOut])
def list_all_tags():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name FROM tags ORDER BY name"
        ).fetchall()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


@router.post("/{book_id}/tags/{tag_name}", status_code=201)
def add_tag(book_id: int, tag_name: str):
    tag_name = tag_name.strip().lower()
    if not tag_name:
        raise HTTPException(status_code=400, detail="Tag name cannot be empty")
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,)
        )
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
# Metadata cache
# ---------------------------------------------------------------------------

@router.get("/{book_id}/metadata")
def get_metadata(book_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM metadata_cache WHERE book_id = ?", (book_id,)
        ).fetchall()
    return [
        {**dict(r),
         "authors": json.loads(r["authors"] or "[]"),
         "genres": json.loads(r["genres"] or "[]")}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@router.get("/stats/summary")
def get_stats():
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        by_type = {
            r["type"]: r["cnt"]
            for r in conn.execute(
                "SELECT type, COUNT(*) AS cnt FROM books GROUP BY type"
            ).fetchall()
        }
        by_status = {
            r["status"]: r["cnt"]
            for r in conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM reading_status GROUP BY status"
            ).fetchall()
        }
    return {"total": total, "by_type": by_type, "by_status": by_status}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _row_to_book(row) -> BookOut:
    d = dict(row)
    d["tags"] = [t for t in (d.pop("tag_list", "") or "").split(",") if t]
    return BookOut(**d)
