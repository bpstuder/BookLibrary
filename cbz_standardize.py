#!/usr/bin/env python3
"""
cbz_standardize.py — Standardize manga CBZ files.

Pipeline:
  1. Extract the CBZ archive
  2. Move all JPG/PNG/WebP images (any depth) to the root of the extracted folder
  3. Delete all non-image files and subdirectories
  4. (Optional) Convert images to WebP without upscaling
  5. (Optional) Rename pages via internal ComicInfo.xml or an external CSV
  6. Repack as a CBZ named  "<Manga> - T<XX>.cbz"
     → Name source: ComicInfo.xml first, filename heuristics as fallback
     → If still unknown: interactive prompt

Usage:
  python cbz_standardize.py <file.cbz> [options]
  python cbz_standardize.py <folder/>  [options]   # process all CBZ files in folder

Options:
  --webp                  Convert images to WebP (requires Pillow)
  --webp-quality <0-100>  WebP quality (default: 85)
  --csv <file.csv>        Page rename CSV (columns: filename, title, volume, chapter, page_start)
  --metadata              Rename pages using ComicInfo.xml
  --output-dir <folder>   Output folder (default: same folder as source)
  --dry-run               Simulate without modifying any files
  -v, --verbose           Verbose output

Optional dependencies:
  pip install Pillow       # required only for --webp
"""

import argparse
import csv
import re
import shutil
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
WEBP_DEFAULT_QUALITY = 85

# Characters forbidden in filenames on Windows and Linux
_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def log(msg: str, verbose: bool = False, force: bool = False) -> None:
    if force or verbose:
        print(msg)


def natural_sort_key(path: Path) -> list:
    """Natural sort key: page2 sorts before page10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.name)]


def sanitize(name: str) -> str:
    """Replace forbidden filename characters with '_' and strip surrounding whitespace."""
    return _FORBIDDEN_RE.sub("_", name).strip()


# ---------------------------------------------------------------------------
# Output CBZ naming  →  "<Manga> - T<XX>.cbz"
# ---------------------------------------------------------------------------

def _parse_volume_from_filename(stem: str) -> tuple[str | None, int | None]:
    """
    Try to extract (title, volume_number) from a filename stem.
    Recognises common patterns:
      one_piece_v01, one-piece-t02, One Piece 03, One_Piece_003, ...
    Returns (None, None) if nothing is found.
    """
    # Pattern: <title> followed by [v|t|vol|tome|volume] + digits
    m = re.search(
        r"^(.*?)[\s_\-\.]*(?:v|t|vol|tome|volume)[\s_\-\.]*(\d+)\s*$",
        stem,
        re.IGNORECASE,
    )
    if m:
        title = re.sub(r"[\s_\-\.]+", " ", m.group(1)).strip()
        volume = int(m.group(2))
        return (title or None, volume)

    # Pattern: <title> followed by digits at the end of the name
    m = re.search(r"^(.*?)[\s_\-\.]+(\d+)\s*$", stem)
    if m:
        title = re.sub(r"[\s_\-\.]+", " ", m.group(1)).strip()
        volume = int(m.group(2))
        return (title or None, volume)

    return (None, None)


def resolve_cbz_name(cbz_path: Path, dry_run: bool) -> str:
    """
    Determine the output CBZ name in the format "<Manga> - T<XX>.cbz".

    Priority:
      1. ComicInfo.xml  (Series + Volume fields)
      2. Filename       (heuristic parsing)
      3. Interactive prompt  (if still incomplete)

    In dry-run mode, placeholder values are used instead of prompting.
    Returns the full filename (with .cbz extension).
    """
    manga_title: str | None = None
    volume_num: int | None = None

    # --- 1. ComicInfo.xml ---
    meta = _read_comicinfo(cbz_path)
    if meta:
        raw_series = meta.get("Series", "").strip()
        raw_volume = meta.get("Volume", "").strip()
        if raw_series:
            manga_title = sanitize(raw_series)
        if raw_volume:
            try:
                volume_num = int(float(raw_volume))
            except ValueError:
                pass
        if manga_title or volume_num is not None:
            log(
                f"  [name]    ComicInfo.xml -> title='{manga_title}', volume={volume_num}",
                force=True,
            )

    # --- 2. Fallback: filename ---
    if manga_title is None or volume_num is None:
        parsed_title, parsed_vol = _parse_volume_from_filename(cbz_path.stem)
        if manga_title is None and parsed_title:
            manga_title = sanitize(parsed_title)
        if volume_num is None and parsed_vol is not None:
            volume_num = parsed_vol
        if manga_title or volume_num is not None:
            log(
                f"  [name]    Filename -> title='{manga_title}', volume={volume_num}",
                force=True,
            )

    # --- 3. Interactive prompt if still incomplete ---
    if dry_run:
        if not manga_title:
            manga_title = "<Title>"
        if volume_num is None:
            volume_num = 0
    else:
        if not manga_title:
            manga_title = _ask(
                f"  > Could not detect manga title for '{cbz_path.name}'. Enter title: "
            ).strip()
            manga_title = sanitize(manga_title) or "Manga"
        if volume_num is None:
            raw = _ask(
                f"  > Could not detect volume number for '{cbz_path.name}'. Enter number: "
            ).strip()
            try:
                volume_num = int(raw)
            except ValueError:
                print("    (non-numeric input, volume set to 0)", file=sys.stderr)
                volume_num = 0

    output_name = f"{manga_title} - T{volume_num:02d}.cbz"
    log(f"  [name]    -> {output_name}", force=True)
    return output_name


def _ask(prompt: str) -> str:
    """Interactive prompt that exits cleanly on Ctrl+C or EOF."""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# ComicInfo.xml reader
# ---------------------------------------------------------------------------

def _read_comicinfo(cbz_path: Path) -> dict:
    """Read ComicInfo.xml from a CBZ archive and return its fields as a dict."""
    try:
        with zipfile.ZipFile(cbz_path, "r") as zf:
            names = [n for n in zf.namelist() if n.lower() == "comicinfo.xml"]
            if not names:
                return {}
            data = zf.read(names[0])
            root = ET.fromstring(data)
            return {child.tag: (child.text or "") for child in root}
    except Exception as e:
        log(f"  [meta]    Error reading ComicInfo.xml: {e}", force=True)
        return {}


# ---------------------------------------------------------------------------
# Step 1: Extract
# ---------------------------------------------------------------------------

def extract_cbz(cbz_path: Path, work_dir: Path, dry_run: bool, verbose: bool) -> Path:
    """Extract the CBZ into work_dir/<stem>/."""
    extract_to = work_dir / cbz_path.stem
    if not dry_run:
        extract_to.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(cbz_path, "r") as zf:
            zf.extractall(extract_to)
    log(f"  [extract] -> {extract_to}", verbose)
    return extract_to


# ---------------------------------------------------------------------------
# Step 2: Flatten images
# ---------------------------------------------------------------------------

def flatten_images(extract_dir: Path, dry_run: bool, verbose: bool) -> list[Path]:
    """
    Move all JPG/PNG/WebP images (any depth) to the root of extract_dir.
    Handles filename collisions by appending a numeric suffix.
    Returns the sorted image list.
    """
    for img in sorted(extract_dir.rglob("*"), key=natural_sort_key):
        if img.suffix.lower() in IMAGE_EXTENSIONS and img.parent != extract_dir:
            dest = extract_dir / img.name
            counter = 1
            while dest.exists() and dest != img:
                dest = extract_dir / f"{img.stem}_{counter}{img.suffix}"
                counter += 1
            log(f"  [flatten] {img.relative_to(extract_dir)} -> {dest.name}", verbose)
            if not dry_run:
                img.rename(dest)

    images = [
        f for f in extract_dir.iterdir()
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
    ]
    return sorted(images, key=natural_sort_key)


# ---------------------------------------------------------------------------
# Step 3: Clean up
# ---------------------------------------------------------------------------

def cleanup(extract_dir: Path, dry_run: bool, verbose: bool) -> None:
    """Remove all non-image files and subdirectories from the root."""
    for item in list(extract_dir.iterdir()):
        if item.is_dir():
            log(f"  [delete]  dir  {item.name}/", verbose)
            if not dry_run:
                shutil.rmtree(item)
        elif item.suffix.lower() not in IMAGE_EXTENSIONS:
            log(f"  [delete]  file {item.name}", verbose)
            if not dry_run:
                item.unlink()


# ---------------------------------------------------------------------------
# Step 4: WebP conversion (optional)
# ---------------------------------------------------------------------------

def check_pillow() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def convert_to_webp(
    images: list[Path],
    quality: int,
    dry_run: bool,
    verbose: bool,
) -> list[Path]:
    """
    Convert each image to WebP without upscaling (original resolution preserved).
    Deletes the source file after conversion.
    Requires Pillow: pip install Pillow
    """
    from PIL import Image  # late import — Pillow is optional

    converted: list[Path] = []
    total = len(images)

    for i, img_path in enumerate(images, 1):
        dest = img_path.with_suffix(".webp")
        log(f"  [webp]    ({i}/{total}) {img_path.name} -> {dest.name}", verbose)
        if not dry_run:
            with Image.open(img_path) as im:
                # Preserve original resolution — no resize
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                im.save(dest, "WEBP", quality=quality, method=6)
            img_path.unlink()
        converted.append(dest)

    return converted


# ---------------------------------------------------------------------------
# Step 5a: Page renaming via CSV
# ---------------------------------------------------------------------------

def load_csv(csv_path: Path) -> dict[str, dict]:
    """
    Required column : filename
    Optional columns: title, volume, chapter, page_start
    """
    mapping: dict[str, dict] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = row.get("filename", "").strip()
            if key:
                mapping[key] = {k.strip(): v.strip() for k, v in row.items()}
    return mapping


def rename_with_csv(
    images: list[Path],
    csv_mapping: dict[str, dict],
    dry_run: bool,
    verbose: bool,
) -> list[Path]:
    renamed: list[Path] = []
    for img in images:
        row = csv_mapping.get(img.name)
        if row:
            new_name = _build_page_name_from_row(row, img.suffix)
            dest = img.parent / new_name
            log(f"  [rename]  {img.name} -> {new_name}", verbose)
            if not dry_run:
                img.rename(dest)
            renamed.append(dest)
        else:
            renamed.append(img)
    return renamed


def _build_page_name_from_row(row: dict, suffix: str) -> str:
    parts = []
    if row.get("title"):
        parts.append(row["title"])
    if row.get("volume"):
        parts.append(f"Vol.{int(row['volume']):02d}")
    if row.get("chapter"):
        parts.append(f"Ch.{float(row['chapter']):05.1f}")
    if row.get("page_start"):
        parts.append(f"p{int(row['page_start']):04d}")
    return "_".join(parts) + suffix if parts else row.get("filename", "page") + suffix


# ---------------------------------------------------------------------------
# Step 5b: Page renaming via ComicInfo.xml
# ---------------------------------------------------------------------------

def rename_pages_with_metadata(
    images: list[Path],
    cbz_path: Path,
    dry_run: bool,
    verbose: bool,
) -> list[Path]:
    """Rename pages using a prefix built from ComicInfo.xml fields."""
    meta = _read_comicinfo(cbz_path)
    if not meta:
        log("  [meta]    No ComicInfo.xml found — page renaming skipped.", force=True)
        return images

    prefix = _build_page_prefix(meta)
    log(f"  [meta]    Page prefix: {prefix}", verbose)

    renamed: list[Path] = []
    for i, img in enumerate(images, start=1):
        new_name = f"{prefix}_p{i:04d}{img.suffix}"
        dest = img.parent / new_name
        log(f"  [rename]  {img.name} -> {new_name}", verbose)
        if not dry_run:
            img.rename(dest)
        renamed.append(dest)
    return renamed


def _build_page_prefix(meta: dict) -> str:
    parts = []
    if meta.get("Series"):
        parts.append(sanitize(meta["Series"]))
    if meta.get("Volume"):
        try:
            parts.append(f"Vol{int(float(meta['Volume'])):02d}")
        except ValueError:
            pass
    if meta.get("Number"):
        try:
            parts.append(f"Ch{float(meta['Number']):05.1f}")
        except ValueError:
            parts.append(f"Ch_{meta['Number']}")
    return "_".join(parts) if parts else "manga"


# ---------------------------------------------------------------------------
# Step 6: Repack as CBZ
# ---------------------------------------------------------------------------

def repack_cbz(extract_dir: Path, output_path: Path, dry_run: bool, verbose: bool) -> None:
    """Repack the folder as a CBZ (ZIP_STORED — optimal for already-compressed images)."""
    images = sorted(
        [f for f in extract_dir.iterdir() if f.suffix.lower() in IMAGE_EXTENSIONS],
        key=natural_sort_key,
    )
    log(f"  [pack]    {len(images)} images -> {output_path.name}", verbose)
    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_STORED) as zf:
            for img in images:
                zf.write(img, img.name)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_cbz(
    cbz_path: Path,
    output_dir: Path | None,
    csv_mapping: dict | None,
    rename_pages_meta: bool,
    webp: bool,
    webp_quality: int,
    dry_run: bool,
    verbose: bool,
) -> Path:
    """Process a single CBZ file and return the path of the output file."""
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}Processing: {cbz_path.name}")

    # Resolve output name BEFORE extraction so the original CBZ is still intact for ComicInfo.xml
    output_name = resolve_cbz_name(cbz_path, dry_run)
    dest_dir = output_dir if output_dir else cbz_path.parent
    output_cbz = dest_dir / output_name

    # Temporary working directory
    work_dir = cbz_path.parent / "_cbz_work"
    if not dry_run:
        work_dir.mkdir(exist_ok=True)

    try:
        # 1. Extract
        extract_dir = extract_cbz(cbz_path, work_dir, dry_run, verbose)

        # 2. Flatten images
        images = flatten_images(extract_dir, dry_run, verbose)
        log(f"  [info]    {len(images)} image(s) found", verbose)

        # 3. Clean up
        cleanup(extract_dir, dry_run, verbose)

        # 4. WebP conversion (optional)
        if webp:
            print(f"  [webp]    Converting to WebP (quality={webp_quality})...")
            images = convert_to_webp(images, webp_quality, dry_run, verbose)

        # 5. Page renaming (optional)
        if csv_mapping is not None:
            images = rename_with_csv(images, csv_mapping, dry_run, verbose)
        elif rename_pages_meta:
            images = rename_pages_with_metadata(images, cbz_path, dry_run, verbose)

        # 6. Repack
        repack_cbz(extract_dir, output_cbz, dry_run, verbose)
        print(f"  [done]    {output_cbz}")
        return output_cbz

    finally:
        if not dry_run and work_dir.exists():
            shutil.rmtree(work_dir)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Standardize CBZ manga files: flatten, clean, convert, rename to "<Manga> - T<XX>.cbz".',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source", help="A .cbz file or a folder containing .cbz files")
    parser.add_argument(
        "--webp", action="store_true",
        help="Convert images to WebP (requires Pillow)",
    )
    parser.add_argument(
        "--webp-quality", type=int, default=WEBP_DEFAULT_QUALITY, metavar="0-100",
        help=f"WebP quality, 0=smallest, 100=lossless (default: {WEBP_DEFAULT_QUALITY})",
    )
    parser.add_argument(
        "--csv", metavar="FILE",
        help="Page rename CSV (columns: filename, title, volume, chapter, page_start)",
    )
    parser.add_argument(
        "--metadata", action="store_true",
        help="Rename pages using ComicInfo.xml metadata",
    )
    parser.add_argument(
        "--output-dir", metavar="FOLDER",
        help="Output folder (default: same folder as source)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate without modifying any files",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Verbose output",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    source = Path(args.source)
    if not source.exists():
        print(f"Error: {source} does not exist.", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output_dir) if args.output_dir else None

    # Load CSV if provided
    csv_mapping: dict | None = None
    if args.csv:
        csv_path = Path(args.csv)
        if not csv_path.exists():
            print(f"Error: CSV not found: {csv_path}", file=sys.stderr)
            sys.exit(1)
        csv_mapping = load_csv(csv_path)
        print(f"CSV loaded: {len(csv_mapping)} entries")

    # Check Pillow availability
    if args.webp:
        if not check_pillow():
            print(
                "Error: Pillow is required for --webp.\n  pip install Pillow",
                file=sys.stderr,
            )
            sys.exit(1)
        if not (0 <= args.webp_quality <= 100):
            print("Error: --webp-quality must be between 0 and 100.", file=sys.stderr)
            sys.exit(1)

    # Collect CBZ files
    if source.is_dir():
        cbz_files = sorted(source.glob("*.cbz"))
    elif source.suffix.lower() == ".cbz":
        cbz_files = [source]
    else:
        print(f"Error: {source} is neither a .cbz file nor a folder.", file=sys.stderr)
        sys.exit(1)

    if not cbz_files:
        print("No .cbz files found.")
        sys.exit(0)

    print(f"{len(cbz_files)} CBZ file(s) to process.")

    ok, ko = 0, 0
    for cbz in cbz_files:
        try:
            process_cbz(
                cbz_path=cbz,
                output_dir=output_dir,
                csv_mapping=csv_mapping,
                rename_pages_meta=args.metadata,
                webp=args.webp,
                webp_quality=args.webp_quality,
                dry_run=args.dry_run,
                verbose=args.verbose,
            )
            ok += 1
        except Exception as e:
            print(f"  [error]   {cbz.name}: {e}", file=sys.stderr)
            ko += 1

    print(f"\nDone: {ok} succeeded, {ko} failed.")


if __name__ == "__main__":
    main()
