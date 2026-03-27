"""
routers/_utils.py — Shared helpers for router modules.

Functions
---------
stream_lines(lines)
    Async generator: converts a list of DONE:/ERROR:/log lines to SSE events.

count_supported_files(target)
    Walk a directory and return (total, by_format) counts for supported file types.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import AsyncGenerator

from services.scanner import SUPPORTED


async def stream_lines(lines: list[str]) -> AsyncGenerator[str, None]:
    """
    Convert a list of standardizer/rename log lines into SSE event strings.

    Line prefixes:
      ``DONE:<json>``   → ``event: done``
      ``ERROR:<msg>``   → ``event: error``
      anything else     → ``event: log``

    Yields one SSE-formatted string per line, with an ``asyncio.sleep(0)``
    between each to allow other coroutines to run.
    """
    for line in lines:
        if line.startswith("DONE:"):
            yield f"event: done\ndata: {line[5:]}\n\n"
        elif line.startswith("ERROR:"):
            yield f"event: error\ndata: {line[6:]}\n\n"
        else:
            yield f"event: log\ndata: {line}\n\n"
        await asyncio.sleep(0)


def count_supported_files(target: Path) -> tuple[int, dict[str, int]]:
    """
    Recursively count supported media files under *target*.

    Returns:
        (total, by_format) where *by_format* maps extension (without dot)
        to file count, e.g. ``{"cbz": 12, "epub": 3}``.

    Raises:
        PermissionError — propagated from os.walk if a directory is unreadable.
    """
    counts: dict[str, int] = {}
    total = 0
    for _, _, files in os.walk(target):
        for fname in files:
            ext = Path(fname).suffix.lower()
            if ext in SUPPORTED:
                key = ext.lstrip(".")
                counts[key] = counts.get(key, 0) + 1
                total += 1
    return total, counts
