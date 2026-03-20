"""
main.py — FastAPI application entry point.

Run with:
  uvicorn main:app --reload
  # or
  python main.py
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from db.database import init_db
from routers.books import router as books_router
from routers.library import router as library_router
from routers.metadata import router as metadata_router

LIBRARY_PATH = Path(os.getenv("LIBRARY_PATH", "./library"))
TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR   = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    LIBRARY_PATH.mkdir(parents=True, exist_ok=True)
    yield
    # Shutdown — nothing to clean up


app = FastAPI(
    title="Manga Collection",
    description="Personal manga, comics, and ebook collection manager.",
    version="1.0.0",
    lifespan=lifespan,
)

# Static files (JS, CSS if any)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# API routers
app.include_router(books_router)
app.include_router(library_router)
app.include_router(metadata_router)


@app.get("/", response_class=HTMLResponse)
async def index():
    return (TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
