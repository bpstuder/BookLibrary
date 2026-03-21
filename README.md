# BookLibrary

A self-hosted web application to manage your manga, comics, and ebook collection.  
Built with **FastAPI** (Python) + vanilla JavaScript. No external database required — uses SQLite.

---

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure your library path
cp .env.example .env
# Edit .env: set LIBRARY_PATH=/path/to/your/library

# 3. Start the server
python main.py

# 4. Open in browser
open http://localhost:8000
```

### Docker

```bash
docker-compose up -d
```

The `docker-compose.yml` mounts your library as read-only and persists the database in a named volume.

---

## Configuration

Settings can be changed in two ways — **environment variables** always take priority over the Settings UI:

| Variable           | Default          | Description                              |
|--------------------|------------------|------------------------------------------|
| `LIBRARY_PATH`     | `./library`      | Path to your book folder                 |
| `PORT`             | `8000`           | HTTP port                                |
| `DEBUG`            | `false`          | Enable /debug, /docs, auto-reload        |
| `COMICVINE_API_KEY`| *(empty)*        | ComicVine API key (free at comicvine.com)|
| `HARDCOVER_API_KEY`| *(empty)*        | Hardcover API key (free at hardcover.app)|

All other settings (WebP quality, metadata storage mode, enabled providers…) are managed in the Settings page and persisted in `data/config.json`.

---

## REST API

Base URL: `http://localhost:8000`

When `DEBUG=true`, interactive API docs are available at `/docs` (Swagger UI) and `/redoc`.

### Books

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/books` | List books with filters, sorting, pagination |
| `GET` | `/books/series` | Group books by series |
| `GET` | `/books/stats/summary` | Counts by type/category/status |
| `GET` | `/books/tags/all` | All tags |
| `GET` | `/books/{id}` | Get one book |
| `PATCH` | `/books/{id}` | Update book fields (title, series, volume, category, type) |
| `DELETE` | `/books/{id}` | Remove book from DB |
| `GET` | `/books/{id}/cover` | Cover thumbnail (JPEG) |
| `PUT` | `/books/{id}/status` | Set reading status and progress |
| `POST` | `/books/{id}/tags/{name}` | Add tag |
| `DELETE` | `/books/{id}/tags/{name}` | Remove tag |
| `GET` | `/books/{id}/metadata` | List all cached metadata rows |
| `POST` | `/books/{id}/move` | Move/rename the file |
| `POST` | `/books/{id}/move/preview` | Preview move without applying |

#### Query parameters for `GET /books`

| Parameter  | Type   | Default | Description |
|------------|--------|---------|-------------|
| `q`        | string | —       | Full-text search on title and series |
| `category` | string | —       | Filter: `manga`, `comics`, `book`, `unknown` |
| `type`     | string | —       | Filter: `cbz`, `cbr`, `epub`, `pdf`, `mobi`, `azw3` |
| `status`   | string | —       | Filter: `unread`, `reading`, `read` |
| `series`   | string | —       | Filter by series name (partial match) |
| `tag`      | string | —       | Filter by tag name |
| `sort`     | string | `title` | Sort field: `title`, `series`, `date_added`, `volume` |
| `order`    | string | `asc`   | `asc` or `desc` |
| `limit`    | int    | `50`    | Max results (≤ 500) |
| `offset`   | int    | `0`     | Pagination offset |

### Metadata

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/metadata/sources` | List providers with enabled/key status |
| `GET` | `/metadata/{book_id}` | All cached rows for a book |
| `POST` | `/metadata/fetch` | Scrape from a provider |
| `POST` | `/metadata/{book_id}/pin/{id}` | Pin result as canonical |
| `POST` | `/metadata/{book_id}/apply/{id}` | Copy fields to book record |
| `PUT` | `/metadata/{book_id}/manual` | Save manual metadata |
| `DELETE` | `/metadata/{book_id}/{id}` | Delete one row |
| `DELETE` | `/metadata/{book_id}` | Delete all rows for book |

#### `POST /metadata/fetch` body

```json
{
  "book_id": 42,
  "source": "anilist",
  "query": "Dragon Ball Super"
}
```

Sources: `anilist`, `comicvine`, `googlebooks`, `hardcover`, `openlib`

### Library

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/scan` | Scan library folder, sync DB |
| `POST` | `/books/{id}/standardize` | Convert CBZ/CBR (SSE stream) |

#### `POST /books/{id}/standardize` body

```json
{
  "webp": true,
  "webp_quality": 85,
  "delete_old": false
}
```

Response is a **Server-Sent Events** stream:
```
event: log
data: [flatten]  page001.jpg → page001.jpg

event: done
data: /path/to/converted.cbz
```

### Batch operations

All batch endpoints stream progress via SSE.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/batch/metadata/fetch` | Scrape metadata for multiple books |
| `POST` | `/batch/metadata/apply` | Apply pinned metadata to books table |
| `POST` | `/batch/metadata/edit` | Set field values on multiple books |
| `POST` | `/batch/metadata/delete` | Delete metadata for multiple books |
| `POST` | `/batch/preview` | Dry-run preview of batch edit |
| `POST` | `/batch/convert/webp` | Convert multiple CBZ/CBR files to WebP |

#### `POST /batch/metadata/fetch` body

```json
{
  "book_ids": [1, 2, 3],
  "source": "anilist",
  "auto_pin": true,
  "min_score": 6.0,
  "skip_existing": true,
  "query_field": "series"
}
```

### Settings

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/config` | Current settings (API keys masked) |
| `PATCH` | `/config` | Update settings |
| `GET` | `/config/browse` | Browse filesystem directories |
| `POST` | `/config/verify-path` | Validate a library path |
| `POST` | `/config/rename-all` | Batch rename files (SSE stream) |

### Debug (debug mode only)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/debug` | System info, DB stats, dependency versions |

---

## Metadata providers

| Provider | Free | Key required | Best for |
|----------|------|-------------|----------|
| AniList | ✓ | No | Manga and anime |
| Google Books | ✓ | No | Novels, comics |
| Open Library | ✓ | No | Books, ISBNs |
| ComicVine | ✓ | Yes | Western comics, manga |
| Hardcover | ✓ | Yes | Books, graphic novels |

---

## File conversion

The CBZ conversion pipeline:
1. Extract archive to a temp folder
2. Flatten nested image directories
3. Remove non-image files (ComicInfo.xml, macOS `._*` files, etc.)
4. Optionally convert images to WebP (requires Pillow)
5. Repack as a CBZ with the same filename

Supported formats for conversion: **CBZ**, **CBR**

---

## Database

SQLite database at `data/collection.db`. Schema is created automatically on first run; migrations run at startup to add new columns to existing databases.

Key tables:
- `books` — file path, title, series, volume, category, format
- `metadata_cache` — scraped/manual metadata (one row per source per book)
- `reading_status` — per-book reading status and progress
- `tags` / `book_tags` — tag system

---

## Development

```bash
# Debug mode (auto-reload, /docs endpoint)
python main.py --debug

# Override library path at startup
python main.py --library /Volumes/NAS/manga

# Override port
python main.py --port 9000
```

### Project structure

```
booklibrary/
├── main.py                 # Entry point, FastAPI app factory
├── cbz_standardize.py      # CBZ conversion pipeline (also usable as CLI)
├── db/
│   ├── config.py           # Settings management (env > disk > defaults)
│   ├── database.py         # SQLite schema, migrations, connection context
│   └── models.py           # Pydantic models (request/response schemas)
├── routers/
│   ├── books.py            # Book CRUD, tags, status, move/rename
│   ├── library.py          # Scan, CBZ conversion
│   ├── metadata.py         # Metadata CRUD and scraping
│   ├── batch.py            # Bulk operations (SSE streaming)
│   ├── config.py           # Settings API, folder browser, batch rename
│   └── debug.py            # System diagnostics (debug mode only)
├── services/
│   ├── scanner.py          # Library folder walker
│   ├── covers.py           # Cover thumbnail extraction
│   ├── metadata.py         # External API scrapers (AniList, Google Books…)
│   └── standardizer.py     # CBZ conversion wrapper for FastAPI
├── static/
│   ├── app.js              # Single-file vanilla JS frontend (~2200 lines)
│   ├── favicon.svg         # App icon
│   └── favicon.png         # App icon (32×32 PNG)
└── templates/
    └── index.html          # SPA entry point (CSS + layout)
```
