"""
routers/config.py — Configuration API.

Endpoints:
  GET  /config              — return current settings
  PATCH /config             — update settings (partial)
  GET  /config/browse       — list filesystem directories (for folder picker)
  POST /config/verify-path  — check a path exists and count supported files
  POST /config/rename-all   — batch-rename all CBZ/CBR in library (SSE stream)
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import db.config as cfg
from db.database import get_conn
from services.scanner import SUPPORTED

router = APIRouter(prefix="/config", tags=["config"])


# ---------------------------------------------------------------------------
# GET / PATCH settings
# ---------------------------------------------------------------------------

@router.get("")
def get_config():
    """Return current configuration (masks API keys, exposes env-locked keys)."""
    settings = cfg.get_all()
    masked = dict(settings)
    for key in ("comicvine_api_key", "hardcover_api_key"):
        if masked.get(key):
            masked[key] = "••••••••"
    return masked


@router.patch("")
def patch_config(body: dict):
    """Partially update settings. Env-locked keys are saved as fallback but not applied now."""
    for key in ("comicvine_api_key", "hardcover_api_key"):
        if body.get(key) == "••••••••":
            body.pop(key)
    updated = cfg.update(body)
    masked = dict(updated)
    for key in ("comicvine_api_key", "hardcover_api_key"):
        if masked.get(key):
            masked[key] = "••••••••"
    return masked


# ---------------------------------------------------------------------------
# Filesystem browser
# ---------------------------------------------------------------------------

@router.get("/browse")
def browse_directory(path: str = ""):
    """
    List subdirectories of the given path.
    Used by the folder picker in the Settings UI.
    """
    if not path:
        # Default: home directory
        path = str(Path.home())

    target = Path(path).expanduser().resolve()

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Path does not exist: {target}")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {target}")

    try:
        entries = []
        for child in sorted(target.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                entries.append({
                    "name": child.name,
                    "path": str(child),
                    "is_dir": True,
                })
        return {
            "current": str(target),
            "parent": str(target.parent) if target.parent != target else None,
            "entries": entries,
        }
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")


# ---------------------------------------------------------------------------
# Verify library path
# ---------------------------------------------------------------------------

class VerifyRequest(BaseModel):
    path: str


@router.post("/verify-path")
def verify_path(body: VerifyRequest):
    """
    Check if a path exists and count supported media files inside it.
    Returns a breakdown by format.
    """
    target = Path(body.path).expanduser().resolve()

    if not target.exists():
        return {"valid": False, "error": f"Path does not exist: {target}"}
    if not target.is_dir():
        return {"valid": False, "error": f"Not a directory: {target}"}

    counts: dict[str, int] = {}
    total = 0
    try:
        for root, _, files in os.walk(target):
            for fname in files:
                ext = Path(fname).suffix.lower()
                if ext in SUPPORTED:
                    key = ext.lstrip(".")
                    counts[key] = counts.get(key, 0) + 1
                    total += 1
    except PermissionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "path": str(target),
        "total": total,
        "by_format": counts,
    }


# ---------------------------------------------------------------------------
# Batch rename — SSE stream
# ---------------------------------------------------------------------------

class RenameRequest(BaseModel):
    dry_run: bool = True          # default: preview only
    pattern: str = "{series} - T{volume:02d}"   # output filename template
    scope: str = "cbz"            # "cbz", "all"


@router.post("/rename-all")
def rename_all(body: RenameRequest):
    """
    Batch-rename every CBZ/CBR in the library that matches
    the standardized naming convention.
    Streams log lines via SSE, then a 'done' or 'error' event.
    """

    async def stream():
        loop = asyncio.get_event_loop()
        lines = await loop.run_in_executor(None, _do_rename, body)
        for line in lines:
            if line.startswith("DONE:"):
                yield f"event: done\ndata: {line[5:]}\n\n"
            elif line.startswith("ERROR:"):
                yield f"event: error\ndata: {line[6:]}\n\n"
            else:
                yield f"event: log\ndata: {line}\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(stream(), media_type="text/event-stream")


def _do_rename(body: RenameRequest) -> list[str]:
    """Run the rename pass, return list of log lines."""
    from db.config import get as cfg_get

    library = Path(cfg_get("library_path")).expanduser().resolve()
    lines: list[str] = []
    renamed = skipped = errors = 0

    if not library.exists():
        return [f"ERROR:Library path does not exist: {library}"]

    # Decide which extensions to process
    if body.scope == "all":
        exts = SUPPORTED
    else:
        exts = {".cbz", ".cbr"}

    # Collect candidates from DB to get series/volume/category/type info
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, path, title, series, volume, type, category FROM books"
        ).fetchall()

    db_map = {r["path"]: dict(r) for r in rows}

    for root, _, files in os.walk(library):
        for fname in sorted(files):
            ext = Path(fname).suffix.lower()
            if ext not in exts:
                continue

            fpath = Path(root) / fname
            info = db_map.get(str(fpath))

            # Determine series and volume
            series   = info.get("series")   if info else None
            volume   = info.get("volume")   if info else None
            title    = info.get("title")    if info else None
            category = info.get("category") if info else "unknown"
            ftype    = info.get("type")     if info else ext.lstrip(".")

            # Fallback: parse from current filename
            if not series or volume is None:
                series_p, volume_p = _parse_name(fpath.stem)
                series = series or series_p
                volume = volume if volume is not None else volume_p

            if not series or volume is None:
                lines.append(f"  [skip]    {fpath.name}  — could not determine series/volume")
                skipped += 1
                continue

            # Build new name — volume is int so {volume:02d} works
            # title in DB often contains "Series T01" — strip the volume suffix for cleaner output
            clean_title = _strip_volume_suffix(title or series or fpath.stem)

            try:
                rel_path = body.pattern.format(
                    series=series,
                    volume=volume,          # int, supports :02d
                    title=clean_title,
                    category=category or "unknown",
                    type=ftype or ext.lstrip("."),
                )
            except (KeyError, ValueError) as e:
                lines.append(f"  [error]   {fpath.name}  — bad pattern: {e}")
                errors += 1
                continue

            # Sanitize each path segment individually to preserve directory separators
            parts    = rel_path.replace("\\", "/").split("/")
            safe_rel = "/".join(_sanitize(p) for p in parts if p)
            new_path = library / (safe_rel + ext)

            if new_path == fpath:
                lines.append(f"  [ok]      {fpath.name}  — already correct")
                skipped += 1
                continue

            rel_display = str(new_path.relative_to(library))
            lines.append(
                f"  {'[dry-run]' if body.dry_run else '[rename] '}"
                f"  {fpath.name}  →  {rel_display}"
            )

            if not body.dry_run:
                try:
                    new_path.parent.mkdir(parents=True, exist_ok=True)
                    fpath.rename(new_path)
                    # Update DB path
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE books SET path = ?, date_updated = datetime('now') "
                            "WHERE path = ?",
                            (str(new_path), str(fpath)),
                        )
                    renamed += 1
                except Exception as e:
                    lines.append(f"  [error]   {fpath.name}  — {e}")
                    errors += 1
            else:
                renamed += 1

    suffix = " (dry run — no files modified)" if body.dry_run else ""
    summary = f"Rename complete{suffix}: {renamed} renamed, {skipped} skipped, {errors} errors"
    lines.append(f"DONE:{summary}")
    return lines


def _parse_name(stem: str):
    """Quick heuristic parse — mirrors cbz_standardize logic."""
    m = re.match(r"^(.+?)\s*-\s*[Tt](\d+)$", stem)
    if m:
        return m.group(1).strip(), int(m.group(2))
    m = re.search(
        r"^(.*?)[\s_\-\.]*(?:v|t|vol|tome|volume)[\s_\-\.]*(\d+)\s*$",
        stem, re.IGNORECASE,
    )
    if m:
        title = re.sub(r"[\s_\-\.]+", " ", m.group(1)).strip()
        return title or None, int(m.group(2))
    return None, None


def _sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()


def _strip_volume_suffix(title: str) -> str:
    """
    Remove trailing volume indicators from a title string.
    e.g. "Dragon Ball Super T10" → "Dragon Ball Super"
         "One Piece - T01"       → "One Piece"
         "Naruto Vol. 5"         → "Naruto"
    """
    cleaned = re.sub(
        r'[\s\-_]*(?:[-–]\s*)?[Tt](?:ome|om)?\s*\d+\s*$', '', title
    )
    cleaned = re.sub(
        r'[\s\-_]*(?:[-–]\s*)?(?:vol|volume|v)\.?\s*\d+\s*$', '', cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip(" -_") or title
