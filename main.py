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
# Logging setup — called early so every module gets the right level
# ---------------------------------------------------------------------------

def _setup_logging(debug: bool) -> None:
    """
    Configure the root logger.
    - force=True replaces any handlers uvicorn may have installed.
    - All manga.* loggers inherit from root (they use NOTSET by default).
    - Called from main() before uvicorn.run(), and from lifespan for the
      'uvicorn main:app' external-server case.
    """
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        stream=sys.stdout,
        force=True,
    )
    # Silence noisy third-party loggers even in debug mode
    for noisy in ("httpx", "httpcore", "multipart", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


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
from routers.batch import router as batch_router

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

        _setup_logging(bool(settings.get("debug")))
        logger = logging.getLogger("manga")
        logger.info("Library : %s", library.resolve())
        logger.info("Database: %s", DB_PATH.resolve())
        logger.info("Debug   : %s", settings.get("debug", False))
        if settings.get("debug"):
            logger.debug("Config  : %s", {
                k: ("***" if "key" in k and v else v)
                for k, v in settings.items()
            })
            logger.debug(
                "Scan filters — include: %s  exclude: %s",
                settings.get("scan_include") or "(all)",
                settings.get("scan_exclude") or "(none)",
            )
            logger.debug(
                "Custom categories: %s",
                [c.get("name") for c in settings.get("custom_categories", [])] or "(none)",
            )

        if settings.get("scan_on_startup"):
            logger.info("Auto-scanning library on startup...")
            from services.scanner import scan_library
            result = scan_library(library)
            logger.info("Scan done: +%d added, %d removed, %d errors",
                        result.added, result.removed, len(result.errors))
        yield

    _app = FastAPI(
        title="BookLibrary",
        version="1.0.0",
        lifespan=lifespan,
        docs_url ="/docs"  if debug else None,
        redoc_url="/redoc" if debug else None,
    )

    _app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    _app.include_router(books_router)
    _app.include_router(library_router)
    _app.include_router(metadata_router)
    _app.include_router(batch_router)
    _app.include_router(config_router)

    if debug:
        from routers.debug import router as debug_router
        _app.include_router(debug_router)

    @_app.get("/", response_class=HTMLResponse)
    async def index():
        return (TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")

    @_app.head("/")
    async def index_head():
        """HEAD / for healthchecks and reverse-proxy probes."""
        return HTMLResponse(content="", status_code=200)

    @_app.head("/", status_code=200)
    async def index_head():
        """HEAD / — used by reverse proxies and health checkers."""
        return None

    if debug:
        import time
        from fastapi import Request

        @_app.middleware("http")
        async def _log_requests(request: Request, call_next):
            """Log every API request with method, path, status and duration."""
            _log = logging.getLogger("manga.http")
            t0 = time.perf_counter()
            response = await call_next(request)
            ms = (time.perf_counter() - t0) * 1000
            _log.debug(
                "%s %s → %d  (%.1f ms)",
                request.method, request.url.path,
                response.status_code, ms,
            )
            return response

    @_app.get("/health")
    async def health():
        """Lightweight health check endpoint. Returns 200 + status."""
        return {"status": "ok", "app": "BookLibrary"}

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

    # Set up logging immediately — before uvicorn starts and potentially
    # installs its own handlers that would shadow ours.
    _setup_logging(debug)

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
