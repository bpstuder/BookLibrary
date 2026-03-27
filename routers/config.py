"""
routers/config.py — Configuration API.

Endpoints:
  GET  /config                     — return current settings
  PATCH /config                    — update settings (partial)
  GET  /config/browse              — list filesystem directories (for folder picker)
  POST /config/verify-path         — check a path exists and count supported files
  GET  /config/scan-folders        — list 1st-level library subfolders with scan status
  GET  /config/categories          — list all categories (built-in + custom)
  POST /config/categories          — create a custom category
  PATCH /config/categories/{name}  — update a custom category
  DELETE /config/categories/{name} — delete a custom category
  POST /config/rename-all          — batch-rename all CBZ/CBR in library (SSE stream)
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
from db.models import BUILTIN_CATEGORIES, CategoryDef
from routers._utils import count_supported_files, stream_lines
from services.scanner import _load_scanignore, SUPPORTED

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
        raise HTTPException(status_code=403, detail="Permission denied") from None


# ---------------------------------------------------------------------------
# Verify library path
# ---------------------------------------------------------------------------

class VerifyRequest(BaseModel):
    """Request body for path verification."""
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

    try:
        total, counts = count_supported_files(target)
    except PermissionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "path": str(target),
        "total": total,
        "by_format": counts,
    }


# ---------------------------------------------------------------------------
# Scan folder listing
# ---------------------------------------------------------------------------

@router.get("/scan-folders")
def list_scan_folders(subpath: str = ""):
    """
    List subdirectories of the library (or a subfolder) with scan status.

    subpath — relative path inside the library to browse (empty = library root).
              e.g. "Manga" lists children of <library>/Manga/

    Each folder entry:
      name        — bare folder name
      rel_path    — path relative to library root  (used as filter key)
      status      — "included" | "excluded" | "ignored"
      source      — "config" | "scanignore" | null
      file_count  — number of supported files (recursive)
      has_children — whether this folder contains sub-directories

    scan_include / scan_exclude store rel_path values (not bare names),
    so nested paths like "BD/Achille Talon" work correctly.
    """
    raw_path = cfg.get("library_path", "")
    library  = Path(raw_path).expanduser().resolve() if raw_path else None

    def _not_ok(reason: str):
        return {
            "library":      str(library) if library else "",
            "library_ok":   False,
            "library_error": reason,
            "subpath":      subpath,
            "scan_include": cfg.get("scan_include", []),
            "scan_exclude": cfg.get("scan_exclude", []),
            "scanignore":   [],
            "folders":      [],
        }

    if not library:
        return _not_ok("Library path is not configured. Set it in Settings → Library.")

    if not library.exists() or not library.is_dir():
        return _not_ok(f"Path does not exist: {library}")

    # Detect if library_path accidentally points to the application directory
    # (contains typical code files/folders — a common misconfiguration)
    code_markers = {"main.py", "requirements.txt", "Dockerfile", "routers", "services", "db"}
    children_names = {c.name for c in library.iterdir()} if library.exists() else set()
    if len(code_markers & children_names) >= 3:
        return _not_ok(
            f"This looks like the application directory, not a media library "
            f"({', '.join(sorted(code_markers & children_names))} found). "
            f"Set the correct library path in Settings → Library."
        )

    # Resolve target directory
    if subpath:
        # Prevent path traversal
        target = (library / subpath).resolve()
        if not str(target).startswith(str(library)):
            raise HTTPException(status_code=400, detail="Invalid subpath")
    else:
        target = library

    include: list[str] = cfg.get("scan_include", [])
    config_exclude: list[str] = cfg.get("scan_exclude", [])
    scanignore_exclude: set[str] = _load_scanignore(library)

    def _get_status(rel: str) -> tuple[str, str | None]:
        """Determine scan status for a folder given its relative path."""
        # Check exact match first, then check if any ancestor is excluded/included
        if rel in scanignore_exclude or any(
            rel == r or rel.startswith(r + "/") for r in scanignore_exclude
        ):
            return "ignored", "scanignore"
        if rel in config_exclude or any(
            rel == r or rel.startswith(r + "/") for r in config_exclude
        ):
            return "excluded", "config"
        if include:
            # Included if: rel matches exactly, rel is a child of an included path,
            # or an included path starts with rel (i.e. rel is a parent of included)
            matched = any(
                rel == r or rel.startswith(r + "/") or r.startswith(rel + "/")
                for r in include
            )
            if not matched:
                return "excluded", "config"
        return "included", None

    folders = []
    try:
        for child in sorted(target.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue

            try:
                rel = str(child.relative_to(library))
            except ValueError:
                continue

            status, source = _get_status(rel)

            try:
                file_count = sum(
                    1 for f in child.rglob("*")
                    if f.is_file() and f.suffix.lower() in SUPPORTED
                )
            except PermissionError:
                file_count = None

            has_children = any(
                c.is_dir() and not c.name.startswith(".")
                for c in child.iterdir()
            ) if child.exists() else False

            folders.append({
                "name":         child.name,
                "rel_path":     rel,
                "status":       status,
                "source":       source,
                "file_count":   file_count,
                "has_children": has_children,
            })
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied") from None

    return {
        "library":      str(library),
        "library_ok":   True,
        "subpath":      subpath,
        "scan_include": include,
        "scan_exclude": config_exclude,
        "scanignore":   sorted(scanignore_exclude),
        "folders":      folders,
    }


# ---------------------------------------------------------------------------
# Custom categories CRUD
# ---------------------------------------------------------------------------

@router.get("/categories")
def list_categories():
    """
    Return all categories: built-ins (manga, comics, book, unknown) plus
    any user-defined ones from config, each annotated with is_builtin.
    """
    custom: list[dict] = cfg.get("custom_categories", [])
    result = [
        {"name": n, "label": n.capitalize(), "folders": [], "color": "", "is_builtin": True}
        for n in BUILTIN_CATEGORIES
    ]
    for cat in custom:
        result.append({**cat, "is_builtin": False})
    return result


@router.post("/categories", status_code=201)
def create_category(body: CategoryDef):
    """
    Create a new custom category.
    - name must be a lowercase slug (letters, digits, hyphens only).
    - name must not collide with a built-in or existing custom category.
    - folders: optional list of 1st-level folder names that auto-assign
      this category during scans.
    """
    _validate_category_name(body.name)

    existing = cfg.get("custom_categories", [])
    all_names = {c["name"] for c in existing} | set(BUILTIN_CATEGORIES)
    if body.name in all_names:
        raise HTTPException(400, f"Category '{body.name}' already exists")

    updated = existing + [body.model_dump()]
    cfg.update({"custom_categories": updated})
    return body


@router.patch("/categories/{name}")
def update_category(name: str, body: CategoryDef):
    """
    Update an existing custom category.
    Built-in categories cannot be modified.
    The name slug itself cannot be changed (delete + recreate instead).
    """
    if name in BUILTIN_CATEGORIES:
        raise HTTPException(400, f"Built-in category '{name}' cannot be modified")

    existing: list[dict] = cfg.get("custom_categories", [])
    idx = next((i for i, c in enumerate(existing) if c["name"] == name), None)
    if idx is None:
        raise HTTPException(404, f"Custom category '{name}' not found")

    if body.name != name:
        raise HTTPException(400, "Category name cannot be changed. Delete and recreate instead.")

    existing[idx] = body.model_dump()
    cfg.update({"custom_categories": existing})
    return body


@router.delete("/categories/{name}", status_code=204)
def delete_category(name: str):
    """
    Delete a custom category.
    Books that use this category are NOT updated — they keep the category
    slug in DB until manually reassigned. Built-ins cannot be deleted.
    """
    if name in BUILTIN_CATEGORIES:
        raise HTTPException(400, f"Built-in category '{name}' cannot be deleted")

    existing: list[dict] = cfg.get("custom_categories", [])
    updated = [c for c in existing if c["name"] != name]
    if len(updated) == len(existing):
        raise HTTPException(404, f"Custom category '{name}' not found")

    cfg.update({"custom_categories": updated})


def _validate_category_name(name: str) -> None:
    """Enforce slug format: lowercase letters, digits, hyphens only."""
    if not name:
        raise HTTPException(400, "Category name cannot be empty")
    if not re.match(r'^[a-z0-9-]+$', name):
        raise HTTPException(
            400,
            f"Invalid category name '{name}'. "
            "Use lowercase letters, digits, and hyphens only (e.g. 'light-novel')."
        )


# ---------------------------------------------------------------------------
# Batch rename — SSE stream
# ---------------------------------------------------------------------------

class RenameRequest(BaseModel):
    """Request body for batch rename operation."""
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
        async for chunk in stream_lines(lines):
            yield chunk

    return StreamingResponse(stream(), media_type="text/event-stream")


def _compute_new_path(
    fpath: Path,
    ext: str,
    info: dict | None,
    pattern: str,
    library: Path,
):
    """
    Compute the destination Path for a file given its DB info and a rename pattern.

    Returns:
      (new_path, rel_display)  on success
      None                     if series/volume could not be determined
      str (error message)      if the pattern is invalid
    """
    series   = info.get("series")   if info else None
    volume   = info.get("volume")   if info else None
    title    = info.get("title")    if info else None
    category = info.get("category") if info else "unknown"
    ftype    = info.get("type")     if info else ext.lstrip(".")

    if not series or volume is None:
        series_p, volume_p = _parse_name(fpath.stem)
        series = series or series_p
        volume = volume if volume is not None else volume_p

    if not series or volume is None:
        return None

    clean_title = _strip_volume_suffix(title or series or fpath.stem)
    try:
        rel_path = pattern.format(
            series=series,
            volume=volume,
            title=clean_title,
            category=category or "unknown",
            type=ftype or ext.lstrip("."),
        )
    except (KeyError, ValueError) as e:
        return str(e)

    parts    = rel_path.replace("\\", "/").split("/")
    safe_rel = "/".join(_sanitize(p) for p in parts if p)
    new_path = library / (safe_rel + ext)
    return new_path, str(new_path.relative_to(library))


def _do_rename(body: RenameRequest) -> list[str]:
    """Run the rename pass, return list of log lines."""
    library = Path(cfg.get("library_path")).expanduser().resolve()
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

    for walk_root, _, files in os.walk(library):
        for fname in sorted(files):
            ext = Path(fname).suffix.lower()
            if ext not in exts:
                continue

            fpath = Path(walk_root) / fname
            info = db_map.get(str(fpath))

            result = _compute_new_path(fpath, ext, info, body.pattern, library)
            if result is None:
                lines.append(f"  [skip]    {fpath.name}  — could not determine series/volume")
                skipped += 1
                continue
            if isinstance(result, str):   # pattern error message
                lines.append(f"  [error]   {fpath.name}  — {result}")
                errors += 1
                continue
            new_path, rel_display = result

            if new_path == fpath:
                lines.append(f"  [ok]      {fpath.name}  — already correct")
                skipped += 1
                continue
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
                except Exception as e:  # pylint: disable=broad-exception-caught
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
