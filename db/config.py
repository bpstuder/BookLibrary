"""
db/config.py — Persistent configuration.

Priority (highest wins):
  1. CLI overrides (cfg.update() after load)
  2. Environment variables / .env  — always applied, override everything else
  3. data/config.json              — values saved via the Settings UI
  4. Built-in defaults

Key design rules:
  - Env vars are the "deployment config". They always win. If LIBRARY_PATH
    is in .env, it is used regardless of what was saved in the UI.
  - Env-locked values are NOT written to config.json (they live in .env).
  - The UI can save any value; it takes effect when the env var is removed.
  - get_all() returns env-locked keys marked so the UI can show them as read-only.

Scan folder filtering (1st-level subdirectories of library_path only):
  scan_include  — list of folder names to scan exclusively.
                  Empty list (default) = scan everything.
  scan_exclude  — list of folder names to always skip.
                  Takes priority over scan_include.
  .scanignore   — plain-text file at library root, one folder name per line.
                  Lines starting with # are comments. Merged with scan_exclude
                  at scan time; never written to config.json.
  Env overrides:
    SCAN_INCLUDE=Manga,BD        (comma-separated)
    SCAN_EXCLUDE=Downloads,Inbox (comma-separated)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_PATH   = Path(__file__).parent.parent / "data" / "config.json"
# Default library path anchored to the project root so it resolves correctly
# regardless of the working directory when the server is started.
_DEFAULT_LIBRARY = str(Path(__file__).parent.parent / "library")

_BASE_DEFAULTS: dict[str, Any] = {
    "library_path":               _DEFAULT_LIBRARY,
    "scan_on_startup":            False,
    "supported_formats":          ["cbz", "cbr", "epub", "pdf", "mobi", "azw3"],
    # Scan folder filtering — 1st-level subdirectories only.
    # scan_include: [] means "scan everything" (default behaviour, fully backward-compatible).
    # scan_exclude always wins over scan_include.
    "scan_include":               [],   # e.g. ["Manga", "BD"]
    "scan_exclude":               [],   # e.g. ["Downloads", "Inbox"]
    # Custom categories — list of {name, label, folders, color} dicts.
    # Built-in categories (manga, comics, book, unknown) are always available.
    # Custom categories extend the built-ins; they are never env-locked.
    "custom_categories":          [],
    "std_webp":                   False,
    "std_webp_quality":           85,
    "std_rename":                 True,
    "std_flatten":                True,
    "std_cleanup":                True,
    "std_delete_old":             False,
    "comicvine_api_key":          "",
    "hardcover_api_key":          "",
    "metadata_providers_enabled": ["anilist", "comicvine", "googlebooks", "hardcover", "openlib"],
    "metadata_storage":           "db",
    "metadata_files_dir":         "data/metadata",
    "auto_fetch_metadata":        False,
    "debug":                      False,
    "port":                       8000,
}

_settings:  dict[str, Any] = {}
_env_keys:  set[str]        = set()   # keys currently locked by an env var


def _read_env() -> dict[str, Any]:
    """Read only env vars that are explicitly set (non-empty)."""
    out: dict[str, Any] = {}
    if v := os.getenv("LIBRARY_PATH", ""):
        out["library_path"] = v
    if v := os.getenv("COMICVINE_API_KEY", ""):
        out["comicvine_api_key"] = v
    if v := os.getenv("HARDCOVER_API_KEY", ""):
        out["hardcover_api_key"] = v
    if os.getenv("DEBUG", "").lower() in ("1", "true", "yes"):
        out["debug"] = True
    if v := os.getenv("PORT", ""):
        try:
            out["port"] = int(v)
        except ValueError:
            pass
    # SCAN_INCLUDE / SCAN_EXCLUDE: comma-separated folder names, spaces trimmed.
    # e.g.  SCAN_INCLUDE=Manga,BD,Webtoons
    if v := os.getenv("SCAN_INCLUDE", ""):
        out["scan_include"] = [f.strip() for f in v.split(",") if f.strip()]
    if v := os.getenv("SCAN_EXCLUDE", ""):
        out["scan_exclude"] = [f.strip() for f in v.split(",") if f.strip()]
    return out


def load() -> dict[str, Any]:
    """
    Priority: defaults < disk < env.
    Env vars always win over anything saved on disk.
    """
    global _settings, _env_keys  # pylint: disable=global-statement
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    on_disk: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            on_disk = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:  # pylint: disable=broad-exception-caught
            on_disk = {}

    env       = _read_env()
    _env_keys = set(env.keys())

    # Build merged settings: defaults < disk < env
    _settings = {**_BASE_DEFAULTS, **on_disk, **env}

    # Ensure every known key exists (handles new keys added in later versions)
    for k, v in _BASE_DEFAULTS.items():
        _settings.setdefault(k, v)

    # Write disk config (without env values — they belong in .env)
    _write_disk(on_disk)
    return _settings


def get(key: str, default: Any = None) -> Any:
    """Return a single config value, loading config if needed."""
    if not _settings:
        load()
    return _settings.get(key, default)


def get_all() -> dict[str, Any]:
    """Return all settings plus meta-info about env-locked keys."""
    if not _settings:
        load()
    result = dict(_settings)
    result["_env_locked"] = sorted(_env_keys)   # let the UI show these as read-only
    return result


def is_env_locked(key: str) -> bool:
    """Return True if key is currently overridden by an environment variable."""
    return key in _env_keys


def update(patch: dict[str, Any]) -> dict[str, Any]:
    """
    Apply patch, persist to disk, re-apply env on top.
    Env-locked values in patch are saved to disk (as fallback for when env is removed)
    but immediately overridden by the live env value.
    """
    if not _settings:
        load()

    # Read current disk state
    on_disk: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            on_disk = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    # Apply patch to disk state
    for key, value in patch.items():
        if key in _BASE_DEFAULTS:
            on_disk[key] = value
            _settings[key] = value

    # Write to disk (without current env values)
    _write_disk(on_disk)

    # Re-apply env on top of in-memory settings
    env = _read_env()
    _env_keys.update(env.keys())
    for k, v in env.items():
        _settings[k] = v

    return get_all()


def _write_disk(on_disk: dict) -> None:
    """Write config.json — excludes keys that are env-locked."""
    to_save = {k: v for k, v in on_disk.items() if k in _BASE_DEFAULTS}
    CONFIG_PATH.write_text(
        json.dumps(to_save, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
