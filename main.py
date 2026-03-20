"""
main.py — FastAPI application entry point.

Run normally:
  python main.py

Run in debug mode (enables /debug endpoint, /docs, verbose logs, auto-reload):
  python main.py --debug
  DEBUG=true python main.py

Override library path or port at startup:
  python main.py --library /Volumes/NAS/manga --port 9000
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path


# ---------------------------------------------------------------------------
# Load .env FIRST — before any other local import reads os.getenv()
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    """
    Minimal .env loader — no external dependency required.
    Falls back to python-dotenv if installed (richer syntax support).
    Looks for .env in the project root (same directory as this file).
    """
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return

    # Try python-dotenv first (handles comments, quotes, multiline, etc.)
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(env_path, override=False)  # don't override already-set vars
        return
    except ImportError:
        pass

    # Fallback: parse .env manually
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:   # don't override shell env vars
                os.environ[key] = value


_load_dotenv()  # must run before any local import that calls os.getenv()


# ---------------------------------------------------------------------------
# Local imports (after .env is loaded)
# ---------------------------------------------------------------------------

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import db.config as cfg
from db.database import DB_PATH  # import triggers auto-init
from routers.books import router as books_router
from routers.config import router as config_router
from routers.library import router as library_router
from routers.metadata import router as metadata_router

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR   = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# CLI args
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manga Collection server")
    parser.add_argument("--debug",   action="store_true",
                        help="Enable debug mode (verbose logs + /debug + /docs)")
    parser.add_argument("--port",    type=int, default=None,
                        help="HTTP port (overrides .env / config)")
    parser.add_argument("--library", type=str, default=None,
                        help="Library path (overrides .env / config)")
    return parser.parse_known_args()[0]   # ignore uvicorn's own args


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(debug: bool = False) -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = cfg.load()
        library  = Path(settings["library_path"]).expanduser()
        library.mkdir(parents=True, exist_ok=True)
        # init_db() is called automatically when db.database is imported

        log_level = logging.DEBUG if settings.get("debug") else logging.INFO
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            stream=sys.stdout,
            force=True,
        )
        logger = logging.getLogger("manga")
        logger.info("Library : %s", library.resolve())
        logger.info("Database: %s", DB_PATH.resolve())
        logger.info("Debug   : %s", settings.get("debug", False))
        if settings.get("debug"):
            logger.debug("Config  : %s", {
                k: ("***" if "key" in k and v else v)
                for k, v in settings.items()
            })

        if settings.get("scan_on_startup"):
            logger.info("Auto-scanning library on startup...")
            from services.scanner import scan_library
            result = scan_library(library)
            logger.info("Scan done: +%d added, %d removed, %d errors",
                        result.added, result.removed, len(result.errors))
        yield

    _app = FastAPI(
        title="Manga Collection",
        version="1.0.0",
        docs_url ="/docs"  if debug else None,
        redoc_url="/redoc" if debug else None,
    )

    _app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    _app.include_router(books_router)
    _app.include_router(library_router)
    _app.include_router(metadata_router)
    _app.include_router(config_router)

    if debug:
        from routers.debug import router as debug_router
        _app.include_router(debug_router)

    @_app.get("/", response_class=HTMLResponse)
    async def index():
        return (TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")

    return _app


# ---------------------------------------------------------------------------
# Module-level app — used when uvicorn is launched externally:
#   uvicorn main:app
# The reload case is handled below via the import-string form.
# ---------------------------------------------------------------------------

cfg.load()
app = create_app(debug=cfg.get("debug", False))


# ---------------------------------------------------------------------------
# Entry point — python main.py [--debug] [--port N] [--library PATH]
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    # Apply CLI overrides on top of .env + disk config
    overrides: dict = {}
    if args.debug:
        overrides["debug"] = True
    if args.port:
        overrides["port"] = args.port
    if args.library:
        overrides["library_path"] = args.library
    if overrides:
        cfg.update(overrides)

    debug = cfg.get("debug", False)
    port  = cfg.get("port",  8000)

    if debug:
        print("╔══════════════════════════════════════╗")
        print("║          DEBUG MODE ENABLED          ║")
        print("╠══════════════════════════════════════╣")
        print("║  /debug  — system diagnostics        ║")
        print("║  /docs   — interactive API docs       ║")
        print("║  auto-reload on file changes         ║")
        print("╚══════════════════════════════════════╝")
        print(f"  Library : {cfg.get('library_path')}")
        print(f"  Port    : {port}")

    if debug:
        # reload=True requires an import string, not an app object.
        # uvicorn will re-import "main:app" on every file change.
        uvicorn.run(
            "main:app",          # <-- import string
            host="0.0.0.0",
            port=port,
            reload=True,
            reload_dirs=[str(Path(__file__).parent)],
            log_level="debug",
        )
    else:
        # Normal mode: pass the already-built app object directly (faster startup).
        global app
        app = create_app(debug=False)
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            reload=False,
            log_level="info",
        )


if __name__ == "__main__":
    main()
