"""
db/database.py — SQLite connection and schema bootstrap.
init_db() is called automatically at import time.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "collection.db"


def init_db() -> None:
    """Create all tables. Safe to call multiple times (IF NOT EXISTS)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS books (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            path         TEXT    NOT NULL UNIQUE,
            title        TEXT    NOT NULL,
            series       TEXT,
            volume       INTEGER,
            type         TEXT    NOT NULL,
            category     TEXT    NOT NULL DEFAULT 'unknown',
            file_size    INTEGER,
            cover_path   TEXT,
            date_added   TEXT    NOT NULL DEFAULT (datetime('now')),
            date_updated TEXT
        );

        CREATE TABLE IF NOT EXISTS tags (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT    NOT NULL UNIQUE COLLATE NOCASE
        );

        CREATE TABLE IF NOT EXISTS book_tags (
            book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            tag_id  INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
            PRIMARY KEY (book_id, tag_id)
        );

        CREATE TABLE IF NOT EXISTS reading_status (
            book_id   INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
            status    TEXT    NOT NULL DEFAULT 'unread',
            progress  INTEGER DEFAULT 0,
            last_read TEXT
        );

        -- Enriched metadata table
        CREATE TABLE IF NOT EXISTS metadata_cache (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id       INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            source        TEXT    NOT NULL,
            -- Core fields
            title         TEXT,
            series        TEXT,
            volume        INTEGER,
            synopsis      TEXT,
            publisher     TEXT,
            year          INTEGER,
            language      TEXT,
            country       TEXT,
            -- People
            authors       TEXT    DEFAULT '[]',   -- JSON array of strings
            artists       TEXT    DEFAULT '[]',   -- JSON array (manga: artist != author)
            -- Classification
            genres        TEXT    DEFAULT '[]',   -- JSON array
            tags          TEXT    DEFAULT '[]',   -- JSON array (more granular than genres)
            -- Identifiers
            isbn          TEXT,
            isbn13        TEXT,
            external_id   TEXT,   -- provider-specific ID (AniList media ID, CV volume ID…)
            -- Ratings
            score         REAL,
            score_count   INTEGER,
            popularity    INTEGER,
            -- Cover
            cover_url     TEXT,
            -- Status (ongoing/finished/…)
            pub_status    TEXT,
            -- Flags
            is_pinned     INTEGER NOT NULL DEFAULT 0,  -- 1 = user selected this as "best"
            is_manual     INTEGER NOT NULL DEFAULT 0,  -- 1 = user-entered, not scraped
            -- Raw payload for future re-parsing
            raw_json      TEXT,
            fetched_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (book_id, source)
        );

        CREATE INDEX IF NOT EXISTS idx_books_series  ON books(series);
        CREATE INDEX IF NOT EXISTS idx_books_type    ON books(type);
        CREATE INDEX IF NOT EXISTS idx_books_title   ON books(title);
        CREATE INDEX IF NOT EXISTS idx_status_status ON reading_status(status);
        CREATE INDEX IF NOT EXISTS idx_meta_book     ON metadata_cache(book_id);
        """)


def migrate_db() -> None:
    """Add columns introduced after initial deploy. Idempotent."""
    with get_conn() as conn:
        existing = {r[1] for r in conn.execute("PRAGMA table_info(books)").fetchall()}
        if "category" not in existing:
            conn.execute("ALTER TABLE books ADD COLUMN category TEXT NOT NULL DEFAULT 'unknown'")

        meta_cols = {r[1] for r in conn.execute("PRAGMA table_info(metadata_cache)").fetchall()}
        new_meta_cols = {
            "title":       "TEXT",
            "series":      "TEXT",
            "volume":      "INTEGER",
            "artists":     "TEXT DEFAULT '[]'",
            "tags":        "TEXT DEFAULT '[]'",
            "isbn":        "TEXT",
            "isbn13":      "TEXT",
            "external_id": "TEXT",
            "score_count": "INTEGER",
            "popularity":  "INTEGER",
            "cover_url":   "TEXT",
            "pub_status":  "TEXT",
            "country":     "TEXT",
            "is_pinned":   "INTEGER NOT NULL DEFAULT 0",
            "is_manual":   "INTEGER NOT NULL DEFAULT 0",
        }
        for col, typedef in new_meta_cols.items():
            if col not in meta_cols:
                conn.execute(f"ALTER TABLE metadata_cache ADD COLUMN {col} {typedef}")

        # Index on is_pinned — only safe after the column exists
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_meta_pinned "
            "ON metadata_cache(book_id, is_pinned)"
        )


@contextmanager
def get_conn():
    """
    Context manager for SQLite connections.
    - WAL mode for better concurrency (readers don't block writers)
    - 5-second busy timeout to handle concurrent requests gracefully
    - foreign_keys enforced on every connection
    - Automatic commit on success, rollback on exception
    """
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")   # faster writes, safe with WAL
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


init_db()
migrate_db()
