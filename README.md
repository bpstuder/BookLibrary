# Manga Collection

A self-hosted web app to manage your manga, comics, and ebook library.

**Formats:** CBZ · CBR · EPUB · PDF · MOBI · AZW3  
**Stack:** FastAPI · SQLite · vanilla JS · Docker

---

## Features

- **Library scan** — point to any local folder, the app indexes everything automatically
- **Catalogue** with search, format filters, and sort options
- **Book detail** — cover, series, volume, reading status, tags
- **Reading tracker** — Unread / Reading / Read + page progress
- **Tag system** — add and remove free-form tags per book
- **Metadata scraping** — fetch synopsis, authors, genres, score from:
  - [AniList](https://anilist.co) (manga, free GraphQL)
  - [ComicVine](https://comicvine.gamespot.com/api/) (comics, free key)
  - [Google Books](https://books.google.com) (ebooks, no key needed)
- **CBZ Standardizer** built in — flatten, WebP convert, rename to `<Series> - T<XX>.cbz` directly from the book detail panel
- **Cover extraction** from CBZ, EPUB, and PDF

---

## Quick start

### With Docker (recommended)

```bash
# 1. Clone the repo
git clone https://github.com/bpstuder/manga-collection.git
cd manga-collection

# 2. Configure
cp .env.example .env
# Edit .env: set LIBRARY_PATH to your manga folder

# 3. Run
docker compose up -d

# 4. Open
open http://localhost:8000

# 5. Scan your library (or click "Scan library" in the UI)
curl -X POST http://localhost:8000/scan
```

### Without Docker

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export LIBRARY_PATH=/path/to/your/manga
export COMICVINE_API_KEY=your_key_here     # optional

python main.py
# → http://localhost:8000
```

---

## Configuration

All configuration is via environment variables (or `.env` file):

| Variable | Default | Description |
|---|---|---|
| `LIBRARY_PATH` | `./library` | Path to your manga/comics/ebooks folder |
| `PORT` | `8000` | HTTP port |
| `COMICVINE_API_KEY` | _(empty)_ | ComicVine API key — get one free at comicvine.gamespot.com/api |

---

## Project structure

```
manga-collection/
├── main.py                  # FastAPI entry point
├── cbz_standardize.py       # CBZ pipeline (standalone CLI too)
│
├── db/
│   ├── database.py          # SQLite connection + schema bootstrap
│   └── models.py            # Pydantic schemas
│
├── services/
│   ├── scanner.py           # Library folder scanner
│   ├── covers.py            # Cover extraction (CBZ / EPUB / PDF)
│   ├── metadata.py          # AniList / ComicVine / Google Books scrapers
│   └── standardizer.py      # CBZ standardizer wrapper (SSE streaming)
│
├── routers/
│   ├── books.py             # CRUD + search + tags + status + stats
│   ├── library.py           # /scan + /standardize SSE route
│   └── metadata.py          # /metadata/fetch
│
├── templates/
│   └── index.html           # Single-page app
├── static/
│   └── app.js               # Frontend JS (no framework)
│
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── requirements.txt
```

---

## Database schema

```
books           — path, title, series, volume, type, file_size, cover_path
tags            — id, name
book_tags       — book_id ↔ tag_id
reading_status  — book_id, status (unread/reading/read), progress, last_read
metadata_cache  — book_id, source, synopsis, publisher, year, authors, genres, score
```

Data is stored in `data/collection.db` (SQLite). Covers are cached as JPEG thumbnails in `data/covers/`.

---

## API reference

| Method | Path | Description |
|---|---|---|
| `GET`    | `/books`                          | List/search books |
| `GET`    | `/books/{id}`                     | Book detail |
| `PATCH`  | `/books/{id}`                     | Update title/series/volume |
| `DELETE` | `/books/{id}`                     | Remove from collection |
| `GET`    | `/books/{id}/cover`               | Serve cover image |
| `PUT`    | `/books/{id}/status`              | Set reading status |
| `POST`   | `/books/{id}/tags/{name}`         | Add tag |
| `DELETE` | `/books/{id}/tags/{name}`         | Remove tag |
| `GET`    | `/books/{id}/metadata`            | Get cached metadata |
| `GET`    | `/books/stats/summary`            | Collection statistics |
| `GET`    | `/books/tags/all`                 | All tags |
| `POST`   | `/scan`                           | Scan library folder |
| `POST`   | `/books/{id}/standardize`         | Standardize CBZ (SSE stream) |
| `POST`   | `/metadata/fetch`                 | Fetch metadata from external API |

Query parameters for `GET /books`:

| Param | Description |
|---|---|
| `q` | Full-text search on title/series |
| `type` | Filter by format: `cbz`, `epub`, `pdf`… |
| `status` | Filter by reading status |
| `series` | Filter by series name |
| `tag` | Filter by tag |
| `sort` | `title` · `series` · `date_added` · `volume` |
| `order` | `asc` · `desc` |
| `limit` / `offset` | Pagination |

---

## CBZ Standardizer (standalone)

`cbz_standardize.py` works independently as a CLI tool:

```bash
python cbz_standardize.py ./manga/ --webp --webp-quality 80
```

See [cbz_standardize.py](cbz_standardize.py) for full documentation.

---

## Docker volumes

| Volume | Purpose |
|---|---|
| `manga_data` | Persistent SQLite database + cover cache |
| `${LIBRARY_PATH}` | Your library folder (mounted read-only) |

---

## License

MIT
