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

| Variable            | Default              | Description                               |
|---------------------|----------------------|-------------------------------------------|
| `LIBRARY_PATH`      | `<project>/library`  | Path to your book folder                  |
| `PORT`              | `8000`               | HTTP port                                 |
| `DEBUG`             | `false`              | Enable /debug, /docs, auto-reload         |
| `SCAN_INCLUDE`      | *(empty)*            | Whitelist: scan only these top-level folders (comma-separated) |
| `SCAN_EXCLUDE`      | *(empty)*            | Blacklist: skip these top-level folders (comma-separated)      |
| `COMICVINE_API_KEY` | *(empty)*            | ComicVine API key (free at comicvine.com) |
| `HARDCOVER_API_KEY` | *(empty)*            | Hardcover API key (free at hardcover.app) |

All other settings (WebP quality, metadata storage mode, enabled providers, custom categories…) are managed in the Settings page and persisted in `data/config.json`.

### Scan folder filters

Three modes are available in **Settings → Scan filters**:

- **Scan all** — every subfolder is scanned (default)
- **Whitelist** — only checked folders are scanned
- **Blacklist** — checked folders are skipped

Filters apply to **1st-level subdirectories** of `LIBRARY_PATH` only. A `.scanignore` file at the library root (one folder name per line) always takes effect regardless of the UI setting.

### Custom categories

Built-in categories (`manga`, `comics`, `book`, `unknown`) are matched by folder keyword. You can add custom categories from **Settings → Categories** — each has a name, label, folder list, and colour. Custom categories take priority over built-ins when their folder names overlap.

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
| `POST` | `/books/{id}/standardize` | Convert CBZ/CBR file (SSE stream) |

#### Query parameters for `GET /books`

| Parameter  | Type   | Default | Description |
|------------|--------|---------|-------------|
| `q`        | string | —       | Full-text search on title and series |
| `category` | string | —       | Filter: `manga`, `comics`, `book`, `unknown`, or any custom category name |
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

### Library scan

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/scan` | Start a library scan (SSE stream) |
| `DELETE` | `/scan` | Cancel the running scan |

`POST /scan` streams progress via **Server-Sent Events**:

```
event: count
data: {"total": 142}

event: progress
data: {"done": 12, "total": 142, "file": "One Piece T01.cbz", "action": "added"}

event: removed
data: {"count": 2, "paths": ["deleted.cbz"]}

event: done
data: {"added": 10, "removed": 2, "errors": []}
```

#### `POST /books/{id}/standardize` body

```json
{
  "webp": true,
  "webp_quality": 85,
  "delete_old": false
}
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
| `GET` | `/config/scan-folders` | List library subfolders with scan status |
| `GET` | `/config/categories` | List all categories (built-in + custom) |
| `POST` | `/config/categories` | Create a custom category |
| `PATCH` | `/config/categories/{name}` | Update a custom category |
| `DELETE` | `/config/categories/{name}` | Delete a custom category |
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

The CBZ conversion pipeline (`POST /books/{id}/standardize`):
1. Extract archive to a temp folder
2. Flatten nested image directories
3. Remove non-image files (ComicInfo.xml, macOS `._*` files, etc.)
4. Optionally convert images to WebP (requires Pillow)
5. Repack as a CBZ with the same filename

`cbz_standardize.py` is also usable as a standalone CLI tool:

```bash
python cbz_standardize.py manga.cbz --webp --webp-quality 85
python cbz_standardize.py ./library/Manga/ --dry-run
```

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
# Debug mode (auto-reload, /docs endpoint, verbose logs)
python main.py --debug

# Override library path at startup
python main.py --library /Volumes/NAS/manga

# Override port
python main.py --port 9000
```

### Project structure

```
manga-collection/
├── main.py                 # Entry point, FastAPI app factory
├── cbz_standardize.py      # CBZ conversion pipeline (also usable as CLI)
├── .pylintrc               # Pylint configuration
├── db/
│   ├── config.py           # Settings management (env > disk > defaults)
│   ├── database.py         # SQLite schema, migrations, connection context
│   └── models.py           # Pydantic models (request/response schemas)
├── routers/
│   ├── _utils.py           # Shared SSE streaming + file counting helpers
│   ├── books.py            # Book CRUD, tags, status, move/rename
│   ├── library.py          # Scan (SSE), scan cancellation, CBZ conversion
│   ├── metadata.py         # Metadata CRUD and scraping
│   ├── batch.py            # Bulk operations (SSE streaming)
│   ├── config.py           # Settings API, categories, scan filters, batch rename
│   └── debug.py            # System diagnostics (debug mode only)
├── services/
│   ├── scanner.py          # Library folder walker + category heuristics
│   ├── covers.py           # Cover thumbnail extraction (CBZ/EPUB/PDF)
│   ├── metadata.py         # External API scrapers (AniList, Google Books…)
│   └── standardizer.py     # CBZ conversion wrapper for FastAPI
├── static/
│   ├── app.js              # Single-file vanilla JS frontend
│   ├── favicon.svg         # App icon
│   └── favicon.png         # App icon (32×32 PNG)
└── templates/
    └── index.html          # SPA entry point (CSS + layout)
```
