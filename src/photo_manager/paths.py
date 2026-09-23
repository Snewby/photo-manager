"""Where a photograph goes, and proving nothing else moved.

These four functions were all born in :mod:`photo_manager.migration`, because
the one-off library migration was the first thing that needed them. They are
here now because the card import needs the same three answers — what folder
does this date mean, what do I call a file when that name is taken, and did
anything change while I was looking — and two implementations of "resolve a
filename collision" would be two chances to resolve it differently.

``migration`` still re-exports them, so nothing that imported them from there
has to change.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

__all__ = [
    "target_directory",
    "suffixed",
    "free_names",
    "directory_snapshot",
]


def target_directory(captured_at: dt.datetime) -> str:
    """``YYYY/YYYY-MM`` for a capture time (D-4).

    A month, not a day, since 2026-09-23 — the library was re-filed that day by
    :mod:`photo_manager.refile`. The day is not lost from the path for anything
    imported, because D-7 filenames begin with it.

    Forward-slashed, because this is the stored relative form and the catalog
    keeps paths portable even on Windows (REQUIREMENTS.md §4.2).
    """
    return f"{captured_at.year:04d}/{captured_at.strftime('%Y-%m')}"


def suffixed(name: str, suffix: int) -> str:
    """``DSCF1024.jpg`` at suffix 2 becomes ``DSCF1024_2.jpg``. 0 is unchanged."""
    if suffix == 0:
        return name
    stem, dot, extension = name.rpartition(".")
    if not dot:
        return f"{name}_{suffix}"
    return f"{stem}_{suffix}{dot}{extension}"


def free_names(
    directory: str, names: list[str], claimed: set[str], root: Path
) -> tuple[int, list[str]]:
    """The lowest suffix at which *every* name in the set is free.

    Returns ``(suffix, names)``, a suffix of 0 meaning there was no collision.

    The "every name" part is the whole point: a photograph's JPEG and its RAF
    are suffixed **as a unit**, so they never stop sharing a basename and never
    stop being pairable (FR-1.2, FR-6.5). Both what is already on disk and what
    this batch has already claimed are checked, and both case-insensitively,
    because Windows will not distinguish ``Wall.JPG`` from ``wall.JPG`` in one
    folder even though Python would.
    """
    for suffix in range(0, 1000):
        candidates = [suffixed(name, suffix) for name in names]
        if all(
            f"{directory}/{candidate}".casefold() not in claimed
            and not (root / directory / candidate).exists()
            for candidate in candidates
        ):
            return suffix, candidates
    raise RuntimeError(f"Could not find a free name for {names} in {directory}")


def directory_snapshot(root: Path) -> dict[str, tuple[int, int] | None]:
    """Every path under *root*, with its size and modification time.

    Compared before and after an operation that promises not to write, this
    turns the promise into a check. Directories map to None; files to
    ``(size, mtime_ns)``.
    """
    snapshot: dict[str, tuple[int, int] | None] = {}
    for current, directories, files in os.walk(root):
        here = Path(current)
        for name in directories:
            snapshot[str((here / name).relative_to(root))] = None
        for name in files:
            path = here / name
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns)
    return snapshot
