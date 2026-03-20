"""
services/covers.py — Extract the cover image from various file formats.

Outputs a JPEG thumbnail saved to data/covers/<book_id>.jpg.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

COVERS_DIR = Path(__file__).parent.parent / "data" / "covers"
COVER_SIZE = (400, 600)   # max thumbnail dimensions


def _covers_dir() -> Path:
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    return COVERS_DIR


def extract_cover(book_path: Path, book_id: int) -> Path | None:
    """
    Extract the first image from a book and save it as a JPEG thumbnail.
    Returns the path to the saved thumbnail, or None on failure.
    """
    ext = book_path.suffix.lower()
    try:
        if ext in (".cbz", ".cbr"):
            return _cover_from_cbz(book_path, book_id)
        elif ext == ".epub":
            return _cover_from_epub(book_path, book_id)
        elif ext == ".pdf":
            return _cover_from_pdf(book_path, book_id)
        # MOBI / AZW3 — cover extraction requires heavy deps, skip for now
        return None
    except Exception:
        return None


def _save_thumbnail(img_bytes: bytes, book_id: int, fmt: str = "JPEG") -> Path:
    """Resize and save a cover image. Requires Pillow."""
    from PIL import Image

    out_path = _covers_dir() / f"{book_id}.jpg"
    with Image.open(io.BytesIO(img_bytes)) as im:
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.thumbnail(COVER_SIZE)
        im.save(out_path, "JPEG", quality=85, optimize=True)
    return out_path


def _cover_from_cbz(path: Path, book_id: int) -> Path | None:
    """First image (natural sort) inside a CBZ/CBR (both are ZIP-compatible)."""
    import re

    def _key(name: str) -> list:
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]

    image_exts = {".jpg", ".jpeg", ".png", ".webp"}
    try:
        with zipfile.ZipFile(path, "r") as zf:
            images = sorted(
                [n for n in zf.namelist() if Path(n).suffix.lower() in image_exts],
                key=_key,
            )
            if not images:
                return None
            data = zf.read(images[0])
            return _save_thumbnail(data, book_id)
    except zipfile.BadZipFile:
        return None


def _cover_from_epub(path: Path, book_id: int) -> Path | None:
    """
    Parse the EPUB OPF manifest to find the cover-image item.
    Falls back to the first image in the ZIP if not declared.
    """
    from xml.etree import ElementTree as ET

    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()

            # 1. Find OPF file via META-INF/container.xml
            opf_path: str | None = None
            if "META-INF/container.xml" in names:
                container = ET.fromstring(zf.read("META-INF/container.xml"))
                for el in container.iter():
                    if el.tag.endswith("rootfile"):
                        opf_path = el.get("full-path")
                        break

            if opf_path and opf_path in names:
                opf = ET.fromstring(zf.read(opf_path))
                ns = {"opf": "http://www.idpf.org/2007/opf"}
                opf_dir = str(Path(opf_path).parent)

                # Find cover id from metadata
                cover_id: str | None = None
                for meta in opf.findall(".//opf:meta", ns):
                    if meta.get("name") == "cover":
                        cover_id = meta.get("content")
                        break

                # Find item href from manifest
                for item in opf.findall(".//opf:item", ns):
                    if cover_id and item.get("id") == cover_id:
                        href = item.get("href", "")
                        full = (
                            href if opf_dir == "." else f"{opf_dir}/{href}"
                        ).lstrip("/")
                        if full in names:
                            return _save_thumbnail(zf.read(full), book_id)

            # Fallback: first image in the ZIP
            images = sorted(
                [n for n in names if Path(n).suffix.lower() in image_exts]
            )
            if images:
                return _save_thumbnail(zf.read(images[0]), book_id)
    except Exception:
        pass
    return None


def _cover_from_pdf(path: Path, book_id: int) -> Path | None:
    """
    Render the first page of a PDF as a JPEG thumbnail.
    Requires pymupdf (fitz): pip install pymupdf
    """
    try:
        import fitz  # type: ignore

        doc = fitz.open(str(path))
        if not doc.page_count:
            return None
        page = doc.load_page(0)
        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
        return _save_thumbnail(pix.tobytes("jpeg"), book_id)
    except ImportError:
        return None
    except Exception:
        return None
