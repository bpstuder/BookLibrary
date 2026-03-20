"""
db/models.py — Pydantic schemas used by FastAPI routes.
"""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Book
# ---------------------------------------------------------------------------

BookType = Literal["cbz", "cbr", "epub", "pdf", "mobi", "azw3", "unknown"]
ReadStatus = Literal["unread", "reading", "read"]


class BookBase(BaseModel):
    title: str
    series: Optional[str] = None
    volume: Optional[int] = None
    type: BookType = "unknown"


class BookCreate(BookBase):
    path: str
    file_size: Optional[int] = None


class BookUpdate(BaseModel):
    title: Optional[str] = None
    series: Optional[str] = None
    volume: Optional[int] = None


class BookOut(BookBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    path: str
    file_size: Optional[int] = None
    cover_path: Optional[str] = None
    date_added: str
    tags: list[str] = []
    status: Optional[ReadStatus] = None
    progress: Optional[int] = None
    last_read: Optional[str] = None


# ---------------------------------------------------------------------------
# Tag
# ---------------------------------------------------------------------------

class TagOut(BaseModel):
    id: int
    name: str


# ---------------------------------------------------------------------------
# Reading status
# ---------------------------------------------------------------------------

class StatusUpdate(BaseModel):
    status: ReadStatus
    progress: Optional[int] = None


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

class MetadataOut(BaseModel):
    source: str
    synopsis: Optional[str] = None
    publisher: Optional[str] = None
    year: Optional[int] = None
    language: Optional[str] = None
    authors: list[str] = []
    genres: list[str] = []
    score: Optional[float] = None
    fetched_at: str


# ---------------------------------------------------------------------------
# Search / filter query params
# ---------------------------------------------------------------------------

class BookFilters(BaseModel):
    q: Optional[str] = None          # full-text search on title / series
    type: Optional[BookType] = None
    status: Optional[ReadStatus] = None
    series: Optional[str] = None
    tag: Optional[str] = None
    sort: Literal["title", "series", "date_added", "volume"] = "title"
    order: Literal["asc", "desc"] = "asc"
    limit: int = 50
    offset: int = 0


# ---------------------------------------------------------------------------
# Scan result
# ---------------------------------------------------------------------------

class ScanResult(BaseModel):
    added: int
    updated: int
    removed: int
    errors: list[str] = []
