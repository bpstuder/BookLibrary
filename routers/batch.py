"""
routers/batch.py — Bulk metadata operations streamed via SSE.

Endpoints:
  POST /batch/metadata/fetch      — scrape N books, stream progress
  POST /batch/metadata/apply      — auto-apply best result for each book, stream
  POST /batch/metadata/edit       — set field values on a list of books
  POST /batch/metadata/delete     — delete all metadata for a list of books
  GET  /batch/preview             — dry-run: show what would be changed
"""

from __future__ import annotations

import asyncio
import json

from typing import Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from db.database import get_conn
from services.metadata import (
    fetch_and_store, get_cached, pin_metadata,
    save_manual, apply_to_book, enabled_sources,
)

router = APIRouter(prefix="/batch", tags=["batch"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class BatchFetchRequest(BaseModel):
    book_ids:      list[int]
    source:        str
    auto_pin:      bool  = True    # pin the highest-score result automatically
    min_score:     float = 0.0     # only pin if score >= min_score (0 = always)
    skip_existing: bool  = True    # skip books that already have a pinned result
    query_field:   str   = "series"  # "series" | "title" — which book field to use as query


class BatchApplyRequest(BaseModel):
    book_ids: list[int]
    fields:   list[str] = [
        "title", "series", "synopsis", "authors", "genres", "year", "publisher"
    ]
    # Only apply if a pinned result exists; skip books without one
    pinned_only: bool = True


class BatchEditRequest(BaseModel):
    """Set explicit field values on every book in the list."""
    book_ids: list[int]
    edits:    dict   # field → value, e.g. {"language": "fr", "publisher": "Shueisha"}
    # Which edits go to the books table vs metadata manual row
    # book_fields: title, series, volume, category, type
    # meta_fields: everything else


class BatchDeleteRequest(BaseModel):
    book_ids:      list[int]
    keep_manual:   bool = True   # keep user-entered manual entries, only delete scraped


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def _progress(done: int, total: int, msg: str) -> str:
    return _sse("progress", json.dumps({"done": done, "total": total, "msg": msg}))


def _log(msg: str, level: str = "info") -> str:
    return _sse("log", json.dumps({"msg": msg, "level": level}))


def _done(summary: dict) -> str:
    return _sse("done", json.dumps(summary))


def _error(msg: str) -> str:
    return _sse("error", json.dumps({"msg": msg}))


# ---------------------------------------------------------------------------
# Batch scrape — SSE
# ---------------------------------------------------------------------------

@router.post("/metadata/fetch")
def batch_fetch(body: BatchFetchRequest):
    """
    Scrape metadata for each book_id, stream progress via SSE.
    """
    if body.source not in enabled_sources():
        async def _blocked():
            yield _error(f"Provider '{body.source}' is disabled or not enabled.")
        return StreamingResponse(_blocked(), media_type="text/event-stream")

    async def stream():
        total   = len(body.book_ids)
        ok = skipped = failed = 0

        yield _log(f"Starting batch scrape — {total} books via {body.source}")
        yield _progress(0, total, "Starting…")

        for i, book_id in enumerate(body.book_ids):
            # Fetch book info
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT id, title, series FROM books WHERE id = ?", (book_id,)
                ).fetchone()
            if not row:
                yield _log(f"  [{i+1}/{total}] Book #{book_id} not found — skip", "warn")
                skipped += 1
                yield _progress(i + 1, total, f"Skipped #{book_id}")
                continue

            book = dict(row)
            query_val = book.get(body.query_field) or book.get("title") or ""
            display   = book.get("series") or book.get("title") or f"#{book_id}"

            # Skip if already pinned and skip_existing is set
            if body.skip_existing:
                existing = get_cached(book_id)
                if any(r.get("is_pinned") for r in existing):
                    yield _log(f"  [{i+1}/{total}] {display} — already pinned, skip")
                    skipped += 1
                    yield _progress(i + 1, total, f"Skipped: {display}")
                    continue

            try:
                results = await fetch_and_store(book_id, body.source, query_val)
                if not results:
                    yield _log(f"  [{i+1}/{total}] {display} — no results", "warn")
                    failed += 1
                else:
                    # Auto-pin best result
                    if body.auto_pin and results:
                        best = _pick_best(results, body.min_score)
                        if best and best.get("id"):
                            pin_metadata(book_id, best["id"])
                            score_str = f" (score {best.get('score', '?')}/10)" if best.get("score") else ""
                            title_str = best.get("title") or best.get("_title") or ""
                            yield _log(
                                f"  [{i+1}/{total}] {display} → pinned: {title_str}{score_str}"
                            )
                        else:
                            yield _log(
                                f"  [{i+1}/{total}] {display} — {len(results)} results, "
                                f"score below threshold, not pinned", "warn"
                            )
                    else:
                        yield _log(
                            f"  [{i+1}/{total}] {display} — {len(results)} results fetched"
                        )
                    ok += 1
            except Exception as e:
                yield _log(f"  [{i+1}/{total}] {display} — error: {e}", "error")
                failed += 1

            yield _progress(i + 1, total, display)
            await asyncio.sleep(0)   # yield to event loop

        summary = {"ok": ok, "skipped": skipped, "failed": failed, "total": total}
        yield _log(
            f"\nDone — {ok} scraped, {skipped} skipped, {failed} failed"
        )
        yield _done(summary)

    return StreamingResponse(stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Batch apply pinned metadata → books table — SSE
# ---------------------------------------------------------------------------

@router.post("/metadata/apply")
def batch_apply(body: BatchApplyRequest):
    """
    For each book, apply the pinned (or best) metadata row's fields
    to the books table. Streams progress.
    """
    async def stream():
        total   = len(body.book_ids)
        ok = skipped = failed = 0

        yield _log(
            f"Applying metadata to {total} books — fields: {', '.join(body.fields)}"
        )
        yield _progress(0, total, "Starting…")

        for i, book_id in enumerate(body.book_ids):
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT title, series FROM books WHERE id = ?", (book_id,)
                ).fetchone()
            if not row:
                skipped += 1
                yield _progress(i + 1, total, f"#{book_id} not found")
                continue

            display = dict(row).get("series") or dict(row).get("title") or f"#{book_id}"
            rows    = get_cached(book_id)

            # Find best row: pinned first, then highest score
            best = None
            if body.pinned_only:
                best = next((r for r in rows if r.get("is_pinned")), None)
            else:
                best = next((r for r in rows if r.get("is_pinned")), None)
                if not best:
                    rows_scored = [r for r in rows if r.get("score")]
                    best = max(rows_scored, key=lambda r: r["score"], default=None)
                if not best and rows:
                    best = rows[0]

            if not best:
                yield _log(f"  [{i+1}/{total}] {display} — no metadata, skip", "warn")
                skipped += 1
                yield _progress(i + 1, total, f"No metadata: {display}")
                continue

            try:
                apply_to_book(book_id, best["id"], body.fields, pin=False)
                applied = [f for f in body.fields if best.get(f)]
                yield _log(f"  [{i+1}/{total}] {display} — applied: {', '.join(applied)}")
                ok += 1
            except Exception as e:
                yield _log(f"  [{i+1}/{total}] {display} — error: {e}", "error")
                failed += 1

            yield _progress(i + 1, total, display)
            await asyncio.sleep(0)

        summary = {"ok": ok, "skipped": skipped, "failed": failed, "total": total}
        yield _log(f"\nDone — {ok} applied, {skipped} skipped, {failed} failed")
        yield _done(summary)

    return StreamingResponse(stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Batch field edit — SSE
# ---------------------------------------------------------------------------

_BOOK_TABLE_FIELDS = {"title", "series", "volume", "category", "type"}

@router.post("/metadata/edit")
def batch_edit(body: BatchEditRequest):
    """
    Set one or more field values directly on a list of books.
    Book-table fields (title, series, volume, category, type) → UPDATE books.
    Other fields → upsert into manual metadata row.
    """
    async def stream():
        total = len(body.book_ids)
        ok = failed = 0

        # Whitelist: split edits into known book-table columns vs metadata fields.
        # Unknown keys in body.edits are silently routed to save_manual, which
        # has its own validation — they never reach a raw SQL SET clause.
        book_fields = {k: v for k, v in body.edits.items() if k in _BOOK_TABLE_FIELDS}
        meta_fields = {k: v for k, v in body.edits.items() if k not in _BOOK_TABLE_FIELDS}

        edits_desc = ", ".join(f"{k}={repr(v)}" for k, v in body.edits.items())
        yield _log(f"Editing {total} books — {edits_desc}")
        yield _progress(0, total, "Starting…")

        for i, book_id in enumerate(body.book_ids):
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT title, series FROM books WHERE id = ?", (book_id,)
                ).fetchone()
            if not row:
                yield _progress(i + 1, total, f"#{book_id} not found")
                continue

            display = dict(row).get("series") or dict(row).get("title") or f"#{book_id}"
            try:
                if book_fields:
                    set_clause = ", ".join(f"{k} = ?" for k in book_fields)
                    with get_conn() as conn:
                        conn.execute(
                            f"UPDATE books SET {set_clause}, date_updated = datetime('now') "
                            f"WHERE id = ?",
                            [*book_fields.values(), book_id],
                        )
                if meta_fields:
                    save_manual(book_id, meta_fields)

                yield _log(f"  [{i+1}/{total}] {display} — ok")
                ok += 1
            except Exception as e:
                yield _log(f"  [{i+1}/{total}] {display} — error: {e}", "error")
                failed += 1

            yield _progress(i + 1, total, display)
            await asyncio.sleep(0)

        summary = {"ok": ok, "failed": failed, "total": total}
        yield _log(f"\nDone — {ok} updated, {failed} failed")
        yield _done(summary)

    return StreamingResponse(stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Batch delete metadata — SSE
# ---------------------------------------------------------------------------

@router.post("/metadata/delete")
def batch_delete(body: BatchDeleteRequest):
    """Delete metadata cache rows for a list of books."""
    async def stream():
        total = len(body.book_ids)
        ok = 0

        yield _log(
            f"Deleting metadata for {total} books"
            + (" (keeping manual entries)" if body.keep_manual else " (including manual)")
        )
        yield _progress(0, total, "Starting…")

        for i, book_id in enumerate(body.book_ids):
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT title, series FROM books WHERE id = ?", (book_id,)
                ).fetchone()
            display = ""
            if row:
                display = dict(row).get("series") or dict(row).get("title") or f"#{book_id}"

            with get_conn() as conn:
                if body.keep_manual:
                    conn.execute(
                        "DELETE FROM metadata_cache WHERE book_id = ? AND is_manual = 0",
                        (book_id,),
                    )
                else:
                    conn.execute(
                        "DELETE FROM metadata_cache WHERE book_id = ?",
                        (book_id,),
                    )
            ok += 1
            yield _log(f"  [{i+1}/{total}] {display or book_id} — cleared")
            yield _progress(i + 1, total, display or str(book_id))
            await asyncio.sleep(0)

        from services.metadata import _sidecar_path
        for book_id in body.book_ids:
            sp = _sidecar_path(book_id)
            if sp.exists():
                sp.unlink(missing_ok=True)

        summary = {"ok": ok, "total": total}
        yield _log(f"\nDone — {ok} cleared")
        yield _done(summary)

    return StreamingResponse(stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Preview (dry-run)
# ---------------------------------------------------------------------------

@router.post("/preview")
def batch_preview(body: BatchEditRequest):
    """
    Return what batch_edit would change, without applying anything.
    Synchronous — returns JSON directly.
    """
    preview = []
    with get_conn() as conn:
        for book_id in body.book_ids:
            row = conn.execute(
                "SELECT id, title, series, volume, category, type FROM books WHERE id = ?",
                (book_id,),
            ).fetchone()
            if not row:
                continue
            book = dict(row)
            changes = {}
            for k, new_val in body.edits.items():
                if k in _BOOK_TABLE_FIELDS:
                    old_val = book.get(k)
                    if old_val != new_val:
                        changes[k] = {"from": old_val, "to": new_val}
                else:
                    changes[k] = {"from": "(metadata)", "to": new_val}
            preview.append({
                "id":      book["id"],
                "title":   book.get("title"),
                "series":  book.get("series"),
                "changes": changes,
            })
    return {"count": len(preview), "items": preview}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pick_best(results: list[dict], min_score: float) -> dict | None:
    """Pick the result with the highest score above min_score, or first if no scores."""
    scored = [r for r in results if r.get("score") and r["score"] >= min_score]
    if scored:
        return max(scored, key=lambda r: r["score"])
    if min_score == 0.0 and results:
        return results[0]
    return None


# ---------------------------------------------------------------------------
# Batch WebP conversion — SSE
# ---------------------------------------------------------------------------

class BatchWebpRequest(BaseModel):
    book_ids:    list[int]
    webp_quality: int  = 85
    delete_old:  bool = False   # delete original CBZ after conversion


@router.post("/convert/webp")
def batch_convert_webp(body: BatchWebpRequest):
    """
    Convert CBZ/CBR files to WebP images in-archive for a list of books.
    Streams progress via SSE. Only CBZ/CBR files are processed.
    """
    from services.standardizer import standardize_book

    async def stream():
        total  = len(body.book_ids)
        ok = skipped = failed = 0

        yield _log(f"Starting WebP conversion — {total} books (quality={body.webp_quality})")
        yield _progress(0, total, "Starting…")

        for i, book_id in enumerate(body.book_ids):
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT title, series, type FROM books WHERE id = ?", (book_id,)
                ).fetchone()
            if not row:
                skipped += 1
                yield _progress(i + 1, total, f"#{book_id} not found")
                continue

            book    = dict(row)
            display = book.get("series") or book.get("title") or f"#{book_id}"
            btype   = book.get("type", "")

            if btype not in ("cbz", "cbr"):
                yield _log(f"  [{i+1}/{total}] {display} — skip ({btype}, not CBZ/CBR)")
                skipped += 1
                yield _progress(i + 1, total, f"Skipped: {display}")
                continue

            try:
                lines = list(standardize_book(
                    book_id=book_id,
                    webp=True,
                    webp_quality=body.webp_quality,
                    delete_old=body.delete_old,
                ))
                # Last line is DONE or ERROR
                last = lines[-1] if lines else ""
                if last.startswith("ERROR:"):
                    yield _log(f"  [{i+1}/{total}] {display} — ✗ {last[6:]}", "error")
                    failed += 1
                else:
                    yield _log(f"  [{i+1}/{total}] {display} — ✓ converted")
                    ok += 1
            except Exception as e:
                yield _log(f"  [{i+1}/{total}] {display} — error: {e}", "error")
                failed += 1

            yield _progress(i + 1, total, display)
            await asyncio.sleep(0)

        summary = {"ok": ok, "skipped": skipped, "failed": failed, "total": total}
        yield _log(f"\nDone — {ok} converted, {skipped} skipped, {failed} failed")
        yield _done(summary)

    return StreamingResponse(stream(), media_type="text/event-stream")
