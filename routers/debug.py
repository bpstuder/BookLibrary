"""
routers/debug.py — Debug information endpoint.

Only mounted when `debug=True` in config (set via Settings or DEBUG=true in .env).
Exposes system info, DB stats, path checks, and dependency availability.

DO NOT expose this endpoint in production.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

from fastapi import APIRouter

import db.config as cfg
from db.database import DB_PATH, get_conn
from routers._utils import count_supported_files

router = APIRouter(prefix="/debug", tags=["debug"])

# Table names are hardcoded constants — no user input reaches this query.
_KNOWN_TABLES = ("books", "tags", "book_tags", "reading_status", "metadata_cache")


@router.get("", summary="System diagnostics")
def debug_info() -> dict:
    """
    Return a full debug snapshot:
    - Python / OS / FastAPI versions
    - Config summary (API keys masked)
    - Library path status and file counts
    - Database stats (table row counts, file size)
    - Optional dependency availability (Pillow, pymupdf, httpx, pydantic)
    """
    settings = cfg.get_all()
    return {
        "system":   _system_info(),
        "config":   _safe_config(settings),
        "library":  _library_info(settings.get("library_path", "")),
        "database": _db_info(),
        "deps":     _dep_info(),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _system_info() -> dict:
    def _ver(mod: str) -> str:
        try:
            return __import__(mod).__version__
        except Exception:  # pylint: disable=broad-exception-caught
            return "unknown"

    return {
        "python":   sys.version,
        "platform": platform.platform(),
        "os":       platform.system(),
        "arch":     platform.machine(),
        "fastapi":  _ver("fastapi"),
        "uvicorn":  _ver("uvicorn"),
    }


def _safe_config(settings: dict) -> dict:
    """Return config with API keys replaced by safe placeholders."""
    out = dict(settings)
    for key in ("comicvine_api_key", "hardcover_api_key"):
        val = out.get(key)
        out[key] = f"set ({len(val)} chars)" if val else "not set"
    return out


def _library_info(library_path: str) -> dict:
    if not library_path:
        return {"status": "not_configured"}

    target = Path(library_path).expanduser().resolve()

    if not target.exists():
        return {"status": "missing", "path": str(target)}
    if not target.is_dir():
        return {"status": "not_a_directory", "path": str(target)}

    try:
        total, counts = count_supported_files(target)
        return {
            "status":      "ok",
            "path":        str(target),
            "total_files": total,
            "by_format":   counts,
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
            for table in _KNOWN_TABLES:
                # Safe: table names are module-level constants, not user input.
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
                tables[table] = row[0] if row else 0
        return {
            "status":     "ok",
            "path":       str(DB_PATH),
            "size_bytes": size_bytes,
            "size_human": _human_size(size_bytes),
            "tables":     tables,
        }
    except Exception as e:  # pylint: disable=broad-exception-caught
        return {"status": "error", "error": str(e)}


def _dep_info() -> dict:
    """Check availability and version of optional/required dependencies."""
    results: dict[str, dict] = {}

    checks = [
        ("pillow",  "PIL",      "Required for WebP conversion (--webp flag)"),
        ("pymupdf", "fitz",     "Required for PDF cover extraction"),
        ("httpx",   "httpx",    "Required for metadata scraping"),
        ("pydantic","pydantic", "Required — install via requirements.txt"),
    ]

    for name, mod, note in checks:
        try:
            m = __import__(mod)
            # fitz (pymupdf) uses a tuple version
            ver = m.version[0] if name == "pymupdf" else m.__version__
            results[name] = {"available": True, "version": ver}
        except ImportError:
            results[name] = {"available": False, "note": note}

    return results


def _human_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b //= 1024
    return f"{b:.1f} TB"
