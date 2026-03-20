"""
routers/debug.py — Debug information endpoint.

Only mounted when debug=True in config.
Exposes system info, DB stats, path checks, and recent log lines.
"""

from __future__ import annotations

import platform
import sqlite3
import sys
from pathlib import Path

from fastapi import APIRouter

import db.config as cfg
from db.database import DB_PATH, get_conn
from services.scanner import SUPPORTED

router = APIRouter(prefix="/debug", tags=["debug"])


@router.get("")
def debug_info():
    """
    Return a full debug snapshot:
      - Python / OS / FastAPI versions
      - Config (with masked API key)
      - Library path status + file counts
      - Database stats (table row counts, file size)
      - Dependency availability (Pillow, pymupdf, httpx)
    """
    settings = cfg.get_all()

    return {
        "system":     _system_info(),
        "config":     _safe_config(settings),
        "library":    _library_info(settings.get("library_path", "")),
        "database":   _db_info(),
        "deps":       _dep_info(),
    }


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _system_info() -> dict:
    try:
        import fastapi
        fv = fastapi.__version__
    except Exception:
        fv = "unknown"
    try:
        import uvicorn
        uv = uvicorn.__version__
    except Exception:
        uv = "unknown"

    return {
        "python":  sys.version,
        "platform": platform.platform(),
        "os":      platform.system(),
        "arch":    platform.machine(),
        "fastapi": fv,
        "uvicorn": uv,
    }


def _safe_config(settings: dict) -> dict:
    out = dict(settings)
    if out.get("comicvine_api_key"):
        out["comicvine_api_key"] = f"set ({len(settings['comicvine_api_key'])} chars)"
    else:
        out["comicvine_api_key"] = "not set"
    return out


def _library_info(library_path: str) -> dict:
    if not library_path:
        return {"status": "not configured"}

    target = Path(library_path).expanduser().resolve()

    if not target.exists():
        return {"status": "missing", "path": str(target)}
    if not target.is_dir():
        return {"status": "not_a_directory", "path": str(target)}

    counts: dict[str, int] = {}
    total = 0
    try:
        for root, _, files in __import__("os").walk(target):
            for fname in files:
                ext = Path(fname).suffix.lower()
                if ext in SUPPORTED:
                    key = ext.lstrip(".")
                    counts[key] = counts.get(key, 0) + 1
                    total += 1
        return {
            "status": "ok",
            "path": str(target),
            "total_files": total,
            "by_format": counts,
        }
    except PermissionError:
        return {"status": "permission_denied", "path": str(target)}


def _db_info() -> dict:
    if not DB_PATH.exists():
        return {"status": "not_initialized", "path": str(DB_PATH)}

    size_bytes = DB_PATH.stat().st_size
    tables: dict[str, int] = {}

    try:
        with get_conn() as conn:
            for table in ("books", "tags", "book_tags", "reading_status", "metadata_cache"):
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                tables[table] = row[0] if row else 0
        return {
            "status": "ok",
            "path": str(DB_PATH),
            "size_bytes": size_bytes,
            "size_human": _human_size(size_bytes),
            "tables": tables,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _dep_info() -> dict:
    deps = {}

    # Pillow
    try:
        from PIL import Image
        import PIL
        deps["pillow"] = {"available": True, "version": PIL.__version__}
    except ImportError:
        deps["pillow"] = {"available": False, "note": "Required for WebP conversion"}

    # pymupdf
    try:
        import fitz
        deps["pymupdf"] = {"available": True, "version": fitz.version[0]}
    except ImportError:
        deps["pymupdf"] = {"available": False, "note": "Required for PDF cover extraction"}

    # httpx
    try:
        import httpx
        deps["httpx"] = {"available": True, "version": httpx.__version__}
    except ImportError:
        deps["httpx"] = {"available": False, "note": "Required for metadata scraping"}

    # pydantic
    try:
        import pydantic
        deps["pydantic"] = {"available": True, "version": pydantic.__version__}
    except ImportError:
        deps["pydantic"] = {"available": False, "note": "Required — install via requirements.txt"}

    return deps


def _human_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b //= 1024
    return f"{b:.1f} TB"
