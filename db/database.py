"""
db/database.py — SQLite connection and schema bootstrap.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "collection.db"


def init_db() -> None:
    """Create all tables if they don't exist yet."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS books (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            path        TEXT    NOT NULL UNIQUE,
            title       TEXT    NOT NULL,
            series      TEXT,
            volume      INTEGER,
            type        TEXT    NOT NULL,          -- cbz, cbr, epub, pdf, mobi, azw3
            file_size   INTEGER,
            cover_path  TEXT,
            date_added  TEXT    NOT NULL DEFAULT (datetime('now')),
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
            status    TEXT    NOT NULL DEFAULT 'unread',  -- unread / reading / read
            progress  INTEGER DEFAULT 0,                  -- page or % depending on type
            last_read TEXT                                -- ISO datetime
        );

        CREATE TABLE IF NOT EXISTS metadata_cache (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id   INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            source    TEXT    NOT NULL,   -- comicvine / googlebooks / anilist
            synopsis  TEXT,
            publisher TEXT,
            year      INTEGER,
            language  TEXT,
            authors   TEXT,               -- JSON array
            genres    TEXT,               -- JSON array
            score     REAL,
            raw_json  TEXT,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (book_id, source)
        );

        CREATE INDEX IF NOT EXISTS idx_books_series  ON books(series);
        CREATE INDEX IF NOT EXISTS idx_books_type    ON books(type);
        CREATE INDEX IF NOT EXISTS idx_books_title   ON books(title);
        CREATE INDEX IF NOT EXISTS idx_status_status ON reading_status(status);
        """)


@contextmanager
def get_conn():
    """Yield a SQLite connection with row_factory and foreign keys enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
