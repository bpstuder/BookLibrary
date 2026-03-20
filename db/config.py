"""
db/config.py — Persistent configuration stored as JSON alongside the database.

Priority at load time (highest wins):
  1. CLI overrides applied via cfg.update() after load
  2. data/config.json  (written by the UI — user-set values persist across restarts)
  3. Environment variables / .env  (initial seeding only — do NOT override saved values)
  4. Built-in defaults

This means:
  - .env / env vars seed the DB on first run if no config.json exists yet.
  - Once the user saves via the Settings page, the saved values take over permanently.
  - ENV vars will NOT silently undo what the user saved in the UI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).parent.parent / "data" / "config.json"

_BASE_DEFAULTS: dict[str, Any] = {
    "library_path":        "./library",
    "scan_on_startup":     False,
    "supported_formats":   ["cbz", "cbr", "epub", "pdf", "mobi", "azw3"],
    # CBZ standardizer
    "std_webp":            False,
    "std_webp_quality":    85,
    "std_rename":          True,
    "std_flatten":         True,
    "std_cleanup":         True,
    "std_delete_old":      False,
    # Metadata
    "comicvine_api_key":   "",
    "hardcover_api_key":   "",
    "metadata_providers_enabled": ["anilist", "comicvine", "googlebooks", "hardcover", "openlib"],
    "metadata_storage":    "db",      # "db" | "file" | "both"
    "metadata_files_dir":  "data/metadata",
    # App
    "auto_fetch_metadata": False,
    "debug":               False,
    "port":                8000,
}

_settings: dict[str, Any] = {}


def _env_seeds() -> dict[str, Any]:
    """
    Read env vars as *seeds* — only used when a key has no saved value on disk.
    Called inside load() BEFORE merging disk values, so disk always wins over env.
    """
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
    return out


def load() -> dict[str, Any]:
    """
    Load with priority: base defaults < env seeds < disk (config.json).
    Disk always wins — user-saved settings are never silently overridden by env.
    """
    global _settings
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    on_disk: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            on_disk = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            on_disk = {}

    # Merge order: defaults < env seeds < saved disk values
    _settings = {**_BASE_DEFAULTS, **_env_seeds(), **on_disk}

    # Ensure all known keys exist (handles new keys added after first run)
    for k, v in _BASE_DEFAULTS.items():
        if k not in _settings:
            _settings[k] = v

    _flush()
    return _settings


def get(key: str, default: Any = None) -> Any:
    if not _settings:
        load()
    return _settings.get(key, default)


def get_all() -> dict[str, Any]:
    if not _settings:
        load()
    return dict(_settings)


def update(patch: dict[str, Any]) -> dict[str, Any]:
    """
    Merge patch into settings and persist immediately.
    Only known keys are accepted.
    Empty strings for API keys are stored as-is (allows clearing a key).
    """
    if not _settings:
        load()
    for key, value in patch.items():
        if key in _BASE_DEFAULTS:
            _settings[key] = value
    _flush()
    return dict(_settings)


def _flush() -> None:
    CONFIG_PATH.write_text(
        json.dumps(_settings, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
