"""
db/models.py — Pydantic schemas used by FastAPI routes.
"""

from __future__ import annotations

from typing import Literal, Optional, Any
from pydantic import BaseModel, ConfigDict


BookType   = Literal["cbz", "cbr", "epub", "pdf", "mobi", "azw3", "unknown"]
BookCat    = Literal["manga", "comics", "book", "unknown"]
ReadStatus = Literal["unread", "reading", "read"]


# ---------------------------------------------------------------------------
# Book
# ---------------------------------------------------------------------------

class BookBase(BaseModel):
    title:    str
    series:   Optional[str] = None
    volume:   Optional[int] = None
    type:     BookType      = "unknown"
    category: BookCat       = "unknown"


class BookCreate(BookBase):
    path:      str
    file_size: Optional[int] = None


class BookUpdate(BaseModel):
    title:    Optional[str]      = None
    series:   Optional[str]      = None
    volume:   Optional[int]      = None
    type:     Optional[BookType] = None
    category: Optional[BookCat]  = None


class BookOut(BookBase):
    model_config = ConfigDict(from_attributes=True)

    id:         int
    path:       str
    file_size:  Optional[int]    = None
    cover_path: Optional[str]    = None
    date_added: str
    tags:       list[str]        = []
    authors:    list[str]        = []      # from pinned/best metadata row
    synopsis:   Optional[str]    = None    # from pinned/best metadata row
    status:     Optional[ReadStatus] = None
    progress:   Optional[int]    = None
    last_read:  Optional[str]    = None


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------

class SeriesOut(BaseModel):
    series:   str
    category: str
    count:    int
    cover_id: Optional[int] = None
    statuses: dict          = {}


# ---------------------------------------------------------------------------
# Tag
# ---------------------------------------------------------------------------

class TagOut(BaseModel):
    id:   int
    name: str


# ---------------------------------------------------------------------------
# Reading status
# ---------------------------------------------------------------------------

class StatusUpdate(BaseModel):
    status:   ReadStatus
    progress: Optional[int] = None


# ---------------------------------------------------------------------------
# Metadata — full enriched model
# ---------------------------------------------------------------------------

class MetadataRow(BaseModel):
    """Full metadata cache row returned to the frontend."""
    model_config = ConfigDict(from_attributes=True)

    id:           int
    book_id:      int
    source:       str
    # Display fields
    title:        Optional[str]   = None
    series:       Optional[str]   = None
    volume:       Optional[int]   = None
    synopsis:     Optional[str]   = None
    publisher:    Optional[str]   = None
    year:         Optional[int]   = None
    language:     Optional[str]   = None
    country:      Optional[str]   = None
    # People
    authors:      list[str]       = []
    artists:      list[str]       = []
    # Classification
    genres:       list[str]       = []
    tags:         list[str]       = []
    # Identifiers
    isbn:         Optional[str]   = None
    isbn13:       Optional[str]   = None
    external_id:  Optional[str]   = None
    # Ratings
    score:        Optional[float] = None
    score_count:  Optional[int]   = None
    popularity:   Optional[int]   = None
    # Cover
    cover_url:    Optional[str]   = None
    # Status
    pub_status:   Optional[str]   = None
    # Flags
    is_pinned:    bool            = False
    is_manual:    bool            = False
    fetched_at:   str             = ""


class MetadataWrite(BaseModel):
    """Fields the user can write manually (or apply from a scraped result)."""
    title:       Optional[str]   = None
    series:      Optional[str]   = None
    volume:      Optional[int]   = None
    synopsis:    Optional[str]   = None
    publisher:   Optional[str]   = None
    year:        Optional[int]   = None
    language:    Optional[str]   = None
    country:     Optional[str]   = None
    authors:     Optional[list[str]] = None
    artists:     Optional[list[str]] = None
    genres:      Optional[list[str]] = None
    tags:        Optional[list[str]] = None
    isbn:        Optional[str]   = None
    isbn13:      Optional[str]   = None
    score:       Optional[float] = None
    cover_url:   Optional[str]   = None
    pub_status:  Optional[str]   = None


class MetadataApply(BaseModel):
    """Apply selected fields from a cache row to the book record and/or manual row."""
    metadata_id: int
    fields:      list[str] = []   # field names to copy
    pin:         bool = True      # also pin this result as the best match


class MetadataSaveRequest(BaseModel):
    """Save a scraped result as the pinned metadata, optionally with overrides."""
    metadata_id: int
    overrides:   Optional[MetadataWrite] = None   # user edits on top of scraped data


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

class MoveRequest(BaseModel):
    pattern:    str  = "{series}/{title}"
    delete_old: bool = False


class StandardizeRequest(BaseModel):
    webp:         bool = False
    webp_quality: int  = 85
    delete_old:   bool = False


# ---------------------------------------------------------------------------
# Search / filter
# ---------------------------------------------------------------------------

class BookFilters(BaseModel):
    q:        Optional[str]        = None
    type:     Optional[BookType]   = None
    category: Optional[BookCat]    = None
    status:   Optional[ReadStatus] = None
    series:   Optional[str]        = None
    tag:      Optional[str]        = None
    sort:     Literal["title", "series", "date_added", "volume"] = "title"
    order:    Literal["asc", "desc"] = "asc"
    limit:    int = 50
    offset:   int = 0


# ---------------------------------------------------------------------------
# Scan result
# ---------------------------------------------------------------------------

class ScanResult(BaseModel):
    added:   int
    updated: int
    removed: int
    errors:  list[str] = []
