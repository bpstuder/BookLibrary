"""
services/metadata.py — Fetch metadata from external APIs.

Supported sources:
  - ComicVine  (comics & manga)  — requires COMICVINE_API_KEY env var
  - Google Books                 — free, no key required
  - AniList    (manga)           — free GraphQL API
"""

import json
import os
from typing import Any

import httpx

from db.database import get_conn

TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def fetch_and_store(book_id: int, source: str, query: str) -> dict:
    """
    Fetch metadata for `query` from `source`, store in metadata_cache,
    and return the parsed result dict.
    """
    fetchers = {
        "comicvine": _fetch_comicvine,
        "googlebooks": _fetch_googlebooks,
        "anilist": _fetch_anilist,
    }
    if source not in fetchers:
        raise ValueError(f"Unknown source: {source}. Choose from {list(fetchers)}")

    result = await fetchers[source](query)

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO metadata_cache
                (book_id, source, synopsis, publisher, year, language,
                 authors, genres, score, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(book_id, source) DO UPDATE SET
                synopsis   = excluded.synopsis,
                publisher  = excluded.publisher,
                year       = excluded.year,
                language   = excluded.language,
                authors    = excluded.authors,
                genres     = excluded.genres,
                score      = excluded.score,
                raw_json   = excluded.raw_json,
                fetched_at = datetime('now')
            """,
            (
                book_id,
                source,
                result.get("synopsis"),
                result.get("publisher"),
                result.get("year"),
                result.get("language"),
                json.dumps(result.get("authors", [])),
                json.dumps(result.get("genres", [])),
                result.get("score"),
                json.dumps(result.get("raw")),
            ),
        )

    return result


def get_cached(book_id: int) -> list[dict]:
    """Return all cached metadata rows for a book."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM metadata_cache WHERE book_id = ?", (book_id,)
        ).fetchall()
    results = []
    for row in rows:
        d = dict(row)
        d["authors"] = json.loads(d.get("authors") or "[]")
        d["genres"] = json.loads(d.get("genres") or "[]")
        results.append(d)
    return results


# ---------------------------------------------------------------------------
# ComicVine
# ---------------------------------------------------------------------------

async def _fetch_comicvine(query: str) -> dict:
    api_key = os.getenv("COMICVINE_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "COMICVINE_API_KEY environment variable is not set. "
            "Get a free key at https://comicvine.gamespot.com/api/"
        )

    url = "https://comicvine.gamespot.com/api/search/"
    params = {
        "api_key": api_key,
        "format": "json",
        "resources": "volume",
        "query": query,
        "field_list": "name,description,publisher,start_year,genres",
        "limit": 1,
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results", [])
    if not results:
        return _empty()

    item = results[0]
    return {
        "synopsis": _strip_html(item.get("description", "")),
        "publisher": (item.get("publisher") or {}).get("name"),
        "year": _safe_int(item.get("start_year")),
        "language": None,
        "authors": [],
        "genres": [g["name"] for g in item.get("genres") or []],
        "score": None,
        "raw": item,
    }


# ---------------------------------------------------------------------------
# Google Books
# ---------------------------------------------------------------------------

async def _fetch_googlebooks(query: str) -> dict:
    url = "https://www.googleapis.com/books/v1/volumes"
    params = {"q": query, "maxResults": 1, "printType": "books"}

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    items = data.get("items", [])
    if not items:
        return _empty()

    info: dict[str, Any] = items[0].get("volumeInfo", {})
    return {
        "synopsis": info.get("description"),
        "publisher": info.get("publisher"),
        "year": _safe_int((info.get("publishedDate") or "")[:4]),
        "language": info.get("language"),
        "authors": info.get("authors") or [],
        "genres": info.get("categories") or [],
        "score": None,
        "raw": info,
    }


# ---------------------------------------------------------------------------
# AniList  (GraphQL)
# ---------------------------------------------------------------------------

_ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: MANGA) {
    title { romaji english native }
    description(asHtml: false)
    startDate { year }
    genres
    averageScore
    staff(perPage: 5) {
      edges { role node { name { full } } }
    }
  }
}
"""


async def _fetch_anilist(query: str) -> dict:
    url = "https://graphql.anilist.co"
    payload = {"query": _ANILIST_QUERY, "variables": {"search": query}}

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()

    media = (data.get("data") or {}).get("Media")
    if not media:
        return _empty()

    authors = [
        edge["node"]["name"]["full"]
        for edge in (media.get("staff") or {}).get("edges", [])
        if edge.get("role") in ("Story", "Art", "Story & Art")
    ]

    score = media.get("averageScore")
    return {
        "synopsis": media.get("description"),
        "publisher": None,
        "year": (media.get("startDate") or {}).get("year"),
        "language": "ja",
        "authors": authors,
        "genres": media.get("genres") or [],
        "score": score / 10 if score else None,  # normalise to /10
        "raw": media,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty() -> dict:
    return {
        "synopsis": None, "publisher": None, "year": None,
        "language": None, "authors": [], "genres": [], "score": None, "raw": {},
    }


def _safe_int(val: Any) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _strip_html(text: str) -> str:
    """Very lightweight HTML tag stripper — avoids pulling in BeautifulSoup."""
    import re
    return re.sub(r"<[^>]+>", "", text).strip()
