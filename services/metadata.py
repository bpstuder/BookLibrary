"""
services/metadata.py — Metadata scraping, storage, sidecar file support.

Sources:
  anilist     — free GraphQL, no key, manga/anime
  comicvine   — requires COMICVINE_API_KEY, comics/manga
  googlebooks — free, books
  hardcover   — requires HARDCOVER_API_KEY, books/manga
  openlib     — free, books (Open Library / Internet Archive)

Config keys:
  metadata_providers_enabled: list of enabled sources
  metadata_storage: "db" | "file" | "both"
  metadata_files_dir: path to sidecar JSON folder (default: data/metadata/)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import logging

import httpx

import db.config as cfg
from db.database import get_conn

log = logging.getLogger("manga.metadata")

TIMEOUT     = 14.0
MAX_RESULTS = 10

ALL_SOURCES = ("anilist", "comicvine", "googlebooks", "hardcover", "openlib")

# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _meta_files_dir() -> Path:
    d = Path(cfg.get("metadata_files_dir", "data/metadata")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sidecar_path(book_id: int) -> Path:
    return _meta_files_dir() / f"{book_id}.meta.json"


def _read_sidecar(book_id: int) -> list[dict]:
    p = _sidecar_path(book_id)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _write_sidecar(book_id: int, rows: list[dict]) -> None:
    _sidecar_path(book_id).write_text(
        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _sync_sidecar_from_db(book_id: int) -> None:
    """Write sidecar from current DB rows."""
    rows = get_cached(book_id, source="db_only")
    _write_sidecar(book_id, rows)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def enabled_sources() -> list[str]:
    enabled = cfg.get("metadata_providers_enabled", list(ALL_SOURCES))
    return [s for s in ALL_SOURCES if s in enabled]


async def fetch_and_store(book_id: int, source: str, query: str) -> list[dict]:
    """
    Fetch results from source, store them, return list of MetadataRow dicts.
    Each result stored as source_N (e.g. anilist_0 … anilist_9).
    """
    fetchers = {
        "anilist":     _fetch_anilist,
        "comicvine":   _fetch_comicvine,
        "googlebooks": _fetch_googlebooks,
        "hardcover":   _fetch_hardcover,
        "openlib":     _fetch_openlib,
    }
    log.debug("metadata: fetching book_id=%d from %s (query=%r)", book_id, source, query)
    results: list[dict] = await fetchers[source](query)
    log.debug("metadata: %s returned %d result(s) for book_id=%d", source, len(results), book_id)

    storage = cfg.get("metadata_storage", "db")

    # Purge previous results for this source
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM metadata_cache WHERE book_id = ? AND source LIKE ? AND is_manual = 0",
            (book_id, f"{source}_%"),
        )

    stored = []
    for i, result in enumerate(results):
        row_source = f"{source}_{i}"
        row = _build_db_row(book_id, row_source, result, is_manual=False)
        if storage in ("db", "both"):
            _upsert_row(row)
            log.debug("metadata: stored %s for book_id=%d — %r", row_source, book_id, result.get("title") or result.get("_title"))
        result["source"]     = row_source
        result["book_id"]    = book_id
        result["is_pinned"]  = False
        result["is_manual"]  = False
        result["fetched_at"] = ""
        stored.append(result)

    if storage in ("file", "both"):
        _sync_sidecar_from_db(book_id)
        log.debug("metadata: sidecar written for book_id=%d", book_id)

    return stored


def get_cached(book_id: int, source: str = "all") -> list[dict]:
    """
    Return all cached metadata rows for a book.
    source="db_only" skips the sidecar.
    Pinned rows first, then manual, then by score desc, then by date desc.
    """
    storage = cfg.get("metadata_storage", "db")

    rows_db: list[dict] = []
    if source != "file_only":
        with get_conn() as conn:
            db_rows = conn.execute(
                """
                SELECT * FROM metadata_cache WHERE book_id = ?
                ORDER BY is_pinned DESC, is_manual DESC,
                         COALESCE(score, 0) DESC, fetched_at DESC
                """,
                (book_id,),
            ).fetchall()
        rows_db = [_parse_db_row(r) for r in db_rows]

    # Merge sidecar if storage includes file
    if source not in ("db_only",) and storage in ("file", "both"):
        sidecar = _read_sidecar(book_id)
        db_sources = {r["source"] for r in rows_db}
        # Add sidecar rows missing from DB
        for r in sidecar:
            if r.get("source") not in db_sources:
                rows_db.append(r)

    return rows_db


def pin_metadata(book_id: int, metadata_id: int) -> None:
    """Mark one row as pinned; unpin all others for this book."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE metadata_cache SET is_pinned = 0 WHERE book_id = ?",
            (book_id,),
        )
        conn.execute(
            "UPDATE metadata_cache SET is_pinned = 1 WHERE id = ? AND book_id = ?",
            (metadata_id, book_id),
        )
    log.debug("metadata: pinned row id=%d for book_id=%d", metadata_id, book_id)
    storage = cfg.get("metadata_storage", "db")
    if storage in ("file", "both"):
        _sync_sidecar_from_db(book_id)


def delete_metadata(book_id: int, metadata_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM metadata_cache WHERE id = ? AND book_id = ?",
            (metadata_id, book_id),
        )
    storage = cfg.get("metadata_storage", "db")
    if storage in ("file", "both"):
        _sync_sidecar_from_db(book_id)


def save_manual(book_id: int, data: dict) -> dict:
    """
    Create or update the 'manual' metadata row.
    Also syncs title/series/volume to the books table if provided.
    Returns the saved row as a dict.
    """
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM metadata_cache WHERE book_id = ? AND source = 'manual'",
            (book_id,),
        ).fetchone()

    current = _parse_db_row(existing) if existing else {}
    # Merge — only overwrite fields explicitly provided (not None)
    for k, v in data.items():
        if v is not None:
            current[k] = v

    row = _build_db_row(book_id, "manual", current, is_manual=True)
    row["is_pinned"] = current.get("is_pinned", False)
    _upsert_row(row)

    # Sync book-table fields (title, series, volume) automatically
    # so the main list/table always reflects manual metadata.
    # Whitelist is explicit: only these three columns may reach the SET clause.
    _BOOK_SYNC_FIELDS = ("title", "series", "volume")
    book_sync: dict[str, Any] = {}
    for field in _BOOK_SYNC_FIELDS:
        if field in data and data[field] is not None:
            book_sync[field] = data[field]
    if book_sync:
        set_clause = ", ".join(f"{k} = ?" for k in book_sync)
        with get_conn() as conn:
            conn.execute(
                f"UPDATE books SET {set_clause}, date_updated = datetime('now') WHERE id = ?",
                [*book_sync.values(), book_id],
            )

    storage = cfg.get("metadata_storage", "db")
    if storage in ("file", "both"):
        _sync_sidecar_from_db(book_id)

    with get_conn() as conn:
        saved = conn.execute(
            "SELECT * FROM metadata_cache WHERE book_id = ? AND source = 'manual'",
            (book_id,),
        ).fetchone()
    log.debug("metadata: manual row saved for book_id=%d — fields: %s", book_id, list(data.keys()))
    return _parse_db_row(saved) if saved else {}


def apply_to_book(book_id: int, metadata_id: int, fields: list[str], pin: bool) -> None:
    """Copy selected fields from a cache row to the books table."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM metadata_cache WHERE id = ? AND book_id = ?",
            (metadata_id, book_id),
        ).fetchone()
    if not row:
        return

    meta = _parse_db_row(row)
    # Whitelist: only title/series/volume may be copied to the books table.
    # The caller-supplied 'fields' list is validated here — no raw key
    # from user input reaches the SET clause.
    _ALLOWED_BOOK_COPY = {"title", "series", "volume"}
    book_fields: dict[str, Any] = {}
    for f in fields:
        if f in _ALLOWED_BOOK_COPY and meta.get(f) is not None:
            book_fields[f] = meta[f]

    if book_fields:
        set_clause = ", ".join(f"{k} = ?" for k in book_fields)
        with get_conn() as conn:
            conn.execute(
                f"UPDATE books SET {set_clause}, date_updated = datetime('now') WHERE id = ?",
                [*book_fields.values(), book_id],
            )

    if pin:
        pin_metadata(book_id, metadata_id)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _upsert_row(row: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO metadata_cache
                (book_id, source, title, series, volume, synopsis, publisher,
                 year, language, country, authors, artists, genres, tags,
                 isbn, isbn13, external_id, score, score_count, popularity,
                 cover_url, pub_status, is_pinned, is_manual, raw_json)
            VALUES
                (:book_id, :source, :title, :series, :volume, :synopsis,
                 :publisher, :year, :language, :country, :authors, :artists,
                 :genres, :tags, :isbn, :isbn13, :external_id, :score,
                 :score_count, :popularity, :cover_url, :pub_status,
                 :is_pinned, :is_manual, :raw_json)
            ON CONFLICT(book_id, source) DO UPDATE SET
                title       = excluded.title,
                series      = excluded.series,
                volume      = excluded.volume,
                synopsis    = excluded.synopsis,
                publisher   = excluded.publisher,
                year        = excluded.year,
                language    = excluded.language,
                country     = excluded.country,
                authors     = excluded.authors,
                artists     = excluded.artists,
                genres      = excluded.genres,
                tags        = excluded.tags,
                isbn        = excluded.isbn,
                isbn13      = excluded.isbn13,
                external_id = excluded.external_id,
                score       = excluded.score,
                score_count = excluded.score_count,
                popularity  = excluded.popularity,
                cover_url   = excluded.cover_url,
                pub_status  = excluded.pub_status,
                is_manual   = excluded.is_manual,
                raw_json    = excluded.raw_json,
                fetched_at  = datetime('now')
            """,
            row,
        )


def _build_db_row(book_id: int, source: str, data: dict, is_manual: bool) -> dict:
    """Normalise a result dict into a flat DB row dict."""
    return {
        "book_id":     book_id,
        "source":      source,
        "title":       data.get("title") or data.get("_title"),
        "series":      data.get("series"),
        "volume":      data.get("volume"),
        "synopsis":    data.get("synopsis"),
        "publisher":   data.get("publisher"),
        "year":        data.get("year"),
        "language":    data.get("language"),
        "country":     data.get("country"),
        "authors":     json.dumps(data.get("authors") or []),
        "artists":     json.dumps(data.get("artists") or []),
        "genres":      json.dumps(data.get("genres")  or []),
        "tags":        json.dumps(data.get("tags")    or []),
        "isbn":        data.get("isbn"),
        "isbn13":      data.get("isbn13"),
        "external_id": data.get("external_id"),
        "score":       data.get("score"),
        "score_count": data.get("score_count"),
        "popularity":  data.get("popularity"),
        "cover_url":   data.get("cover_url"),
        "pub_status":  data.get("pub_status"),
        "is_pinned":   int(bool(data.get("is_pinned", False))),
        "is_manual":   int(is_manual),
        "raw_json":    json.dumps(data.get("raw") or {}),
    }


def _parse_db_row(row) -> dict:
    if row is None:
        return {}
    d = dict(row)
    for arr_field in ("authors", "artists", "genres", "tags"):
        d[arr_field] = json.loads(d.get(arr_field) or "[]")
    d["is_pinned"] = bool(d.get("is_pinned", 0))
    d["is_manual"] = bool(d.get("is_manual", 0))
    return d


# ---------------------------------------------------------------------------
# AniList
# ---------------------------------------------------------------------------

_ANILIST_QUERY = """
query ($search: String, $page: Int) {
  Page(page: $page, perPage: 10) {
    media(search: $search, type: MANGA) {
      id
      title { romaji english native }
      description(asHtml: false)
      startDate { year }
      status
      volumes
      genres
      tags { name rank isMediaSpoiler }
      averageScore
      popularity
      coverImage { large medium }
      staff(perPage: 8) {
        edges {
          role
          node { name { full } }
        }
      }
      externalLinks { url site }
    }
  }
}
"""


async def _fetch_anilist(query: str) -> list[dict]:
    payload = {"query": _ANILIST_QUERY, "variables": {"search": query, "page": 1}}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post("https://graphql.anilist.co", json=payload)
        resp.raise_for_status()
        data = resp.json()

    media_list = ((data.get("data") or {}).get("Page") or {}).get("media") or []
    results = []
    for media in media_list[:MAX_RESULTS]:
        authors = [
            e["node"]["name"]["full"]
            for e in (media.get("staff") or {}).get("edges", [])
            if e.get("role") in ("Story", "Story & Art")
        ]
        artists = [
            e["node"]["name"]["full"]
            for e in (media.get("staff") or {}).get("edges", [])
            if e.get("role") in ("Art", "Story & Art")
        ]
        title_obj  = media.get("title") or {}
        en_title   = title_obj.get("english") or title_obj.get("romaji") or ""
        score      = media.get("averageScore")
        # Tags: exclude spoilers, take top 10 by rank
        al_tags = sorted(
            [t["name"] for t in (media.get("tags") or [])
             if not t.get("isMediaSpoiler") and t.get("rank", 0) >= 60],
            key=lambda x: x
        )[:10]
        status_map = {
            "FINISHED":         "Finished",
            "RELEASING":        "Ongoing",
            "NOT_YET_RELEASED": "Upcoming",
            "CANCELLED":        "Cancelled",
            "HIATUS":           "On Hiatus",
        }
        cover = (media.get("coverImage") or {})
        results.append({
            "_title":      en_title,
            "_subtitle":   _anilist_subtitle(media),
            "title":       en_title,
            "series":      en_title,
            "synopsis":    media.get("description"),
            "year":        (media.get("startDate") or {}).get("year"),
            "language":    "ja",
            "country":     "JP",
            "authors":     authors,
            "artists":     list({*artists} - {*authors}),  # deduplicate
            "genres":      media.get("genres") or [],
            "tags":        al_tags,
            "score":       score / 10 if score else None,
            "score_count": None,
            "popularity":  media.get("popularity"),
            "cover_url":   cover.get("large") or cover.get("medium"),
            "pub_status":  status_map.get(media.get("status") or "", media.get("status")),
            "external_id": str(media.get("id") or ""),
            "raw":         media,
        })
    return results or [_empty()]


def _anilist_subtitle(media: dict) -> str:
    parts = []
    if (media.get("startDate") or {}).get("year"):
        parts.append(str(media["startDate"]["year"]))
    if media.get("volumes"):
        parts.append(f"{media['volumes']} vol.")
    status_map = {"FINISHED": "Finished", "RELEASING": "Ongoing", "HIATUS": "Hiatus"}
    st = status_map.get(media.get("status") or "")
    if st:
        parts.append(st)
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# ComicVine
# ---------------------------------------------------------------------------

async def _fetch_comicvine(query: str) -> list[dict]:
    api_key = cfg.get("comicvine_api_key", "") or ""
    if not api_key:
        raise RuntimeError(
            "ComicVine API key not set. Add it in Settings → Metadata sources."
        )

    params = {
        "api_key":    api_key,
        "format":     "json",
        "resources":  "volume",
        "query":      query,
        "field_list": (
            "name,description,publisher,start_year,count_of_issues,"
            "genres,image,site_detail_url,id,deck"
        ),
        "limit": MAX_RESULTS,
    }
    headers = {"User-Agent": "manga-collection/1.0 (personal library manager)"}
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as client:
        resp = await client.get("https://comicvine.gamespot.com/api/search/", params=params)
        resp.raise_for_status()
        data = resp.json()

    items = data.get("results") or []
    results = []
    for item in items[:MAX_RESULTS]:
        img = (item.get("image") or {})
        results.append({
            "_title":    item.get("name", ""),
            "_subtitle": _comicvine_subtitle(item),
            "title":     item.get("name"),
            "series":    item.get("name"),
            "synopsis":  _strip_html(item.get("description") or item.get("deck") or ""),
            "publisher": (item.get("publisher") or {}).get("name"),
            "year":      _safe_int(item.get("start_year")),
            "genres":    [g["name"] for g in item.get("genres") or []],
            "cover_url": img.get("original_url") or img.get("medium_url"),
            "external_id": str(item.get("id") or ""),
            "raw":       item,
        })
    return results or [_empty()]


def _comicvine_subtitle(item: dict) -> str:
    parts = []
    if item.get("start_year"):
        parts.append(str(item["start_year"]))
    if item.get("count_of_issues"):
        parts.append(f"{item['count_of_issues']} issues")
    pub = (item.get("publisher") or {}).get("name")
    if pub:
        parts.append(pub)
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Google Books
# ---------------------------------------------------------------------------

async def _fetch_googlebooks(query: str) -> list[dict]:
    params = {"q": query, "maxResults": MAX_RESULTS, "printType": "books"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get("https://www.googleapis.com/books/v1/volumes", params=params)
        resp.raise_for_status()
        data = resp.json()

    items = data.get("items") or []
    results = []
    for item in items[:MAX_RESULTS]:
        info  = item.get("volumeInfo", {})
        ids   = item.get("industryIdentifiers", [])
        isbn  = next((x["identifier"] for x in ids if x["type"] == "ISBN_10"), None)
        isbn13= next((x["identifier"] for x in ids if x["type"] == "ISBN_13"), None)
        imgs  = info.get("imageLinks") or {}
        sub   = []
        if info.get("publishedDate"): sub.append(info["publishedDate"][:4])
        if info.get("pageCount"):     sub.append(f"{info['pageCount']} p.")
        if info.get("publisher"):     sub.append(info["publisher"])
        results.append({
            "_title":    info.get("title", ""),
            "_subtitle": " · ".join(sub),
            "title":     info.get("title"),
            "series":    (info.get("seriesInfo") or {}).get("bookDisplayNumber") or None,
            "synopsis":  info.get("description"),
            "publisher": info.get("publisher"),
            "year":      _safe_int((info.get("publishedDate") or "")[:4]),
            "language":  info.get("language"),
            "authors":   info.get("authors") or [],
            "genres":    info.get("categories") or [],
            "isbn":      isbn,
            "isbn13":    isbn13,
            "cover_url": imgs.get("large") or imgs.get("thumbnail"),
            "external_id": item.get("id"),
            "raw":       info,
        })
    return results or [_empty()]


# ---------------------------------------------------------------------------
# Hardcover
# ---------------------------------------------------------------------------

_HARDCOVER_QUERY = """
query SearchBooks($query: String!) {
  search(query: $query, query_type: "Book", per_page: 10, page: 1) {
    results
  }
}
"""


async def _fetch_hardcover(query: str) -> list[dict]:
    api_key = cfg.get("hardcover_api_key", "") or ""
    if not api_key:
        raise RuntimeError(
            "Hardcover API key not set. Add it in Settings → Metadata sources."
        )

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"query": _HARDCOVER_QUERY, "variables": {"query": query}}

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            "https://api.hardcover.app/v1/graphql", json=payload, headers=headers
        )
        resp.raise_for_status()
        data = resp.json()

    raw = ((data.get("data") or {}).get("search") or {}).get("results", "[]")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    hits = raw if isinstance(raw, list) else raw.get("hits", [])

    results = []
    for hit in hits[:MAX_RESULTS]:
        doc = hit.get("document") or hit
        authors = [str(a) for a in (doc.get("author_names") or [])]
        sub = []
        if doc.get("release_year"): sub.append(str(doc["release_year"])[:4])
        results.append({
            "_title":    doc.get("title", ""),
            "_subtitle": " · ".join(sub),
            "title":     doc.get("title"),
            "synopsis":  doc.get("description"),
            "year":      _safe_int(str(doc.get("release_year") or "")[:4]),
            "authors":   authors,
            "genres":    doc.get("genres") or [],
            "score":     _safe_float(doc.get("rating")),
            "cover_url": doc.get("image", {}).get("url") if isinstance(doc.get("image"), dict) else None,
            "external_id": str(doc.get("id") or ""),
            "raw":       doc,
        })
    return results or [_empty()]


# ---------------------------------------------------------------------------
# Open Library
# ---------------------------------------------------------------------------

async def _fetch_openlib(query: str) -> list[dict]:
    params = {
        "q":      query,
        "limit":  MAX_RESULTS,
        "fields": (
            "key,title,author_name,first_publish_year,subject,"
            "publisher,language,ratings_average,ratings_count,"
            "isbn,number_of_pages_median,edition_count"
        ),
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get("https://openlibrary.org/search.json", params=params)
        resp.raise_for_status()
        data = resp.json()

    docs = data.get("docs") or []
    results = []
    for doc in docs[:MAX_RESULTS]:
        isbn_list  = doc.get("isbn") or []
        isbn10     = next((x for x in isbn_list if len(x) == 10), None)
        isbn13_val = next((x for x in isbn_list if len(x) == 13), None)
        sub = []
        if doc.get("first_publish_year"): sub.append(str(doc["first_publish_year"]))
        if doc.get("edition_count"):      sub.append(f"{doc['edition_count']} ed.")
        results.append({
            "_title":    doc.get("title", ""),
            "_subtitle": " · ".join(sub),
            "title":     doc.get("title"),
            "synopsis":  None,
            "publisher": (doc.get("publisher") or [None])[0],
            "year":      _safe_int(doc.get("first_publish_year")),
            "language":  (doc.get("language") or [None])[0],
            "authors":   doc.get("author_name") or [],
            "genres":    (doc.get("subject") or [])[:10],
            "isbn":      isbn10,
            "isbn13":    isbn13_val,
            "score":     _safe_float(doc.get("ratings_average")),
            "score_count": _safe_int(doc.get("ratings_count")),
            "external_id": doc.get("key"),
            "raw":       doc,
        })
    return results or [_empty()]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty() -> dict:
    return {
        "_title": "", "_subtitle": "",
        "title": None, "synopsis": None, "publisher": None,
        "year": None, "language": None, "country": None,
        "authors": [], "artists": [], "genres": [], "tags": [],
        "isbn": None, "isbn13": None, "external_id": None,
        "score": None, "score_count": None, "popularity": None,
        "cover_url": None, "pub_status": None, "raw": {},
    }


def _safe_int(val: Any) -> int | None:
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return None


def _safe_float(val: Any) -> float | None:
    try:
        return round(float(val), 2)
    except (TypeError, ValueError):
        return None


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()
