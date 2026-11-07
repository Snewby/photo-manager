"""The catalog's row types.

Plain frozen dataclasses, one per table in REQUIREMENTS.md §5.1. They carry no
database access — the repositories in :mod:`photo_manager.catalog` do that — so
they can be built from EXIF, from a card scan, or from a sidecar without a
catalog existing yet.

``id`` is ``None`` until a row has been written. Repository ``add`` methods
return a new instance with the id filled in rather than mutating, which is what
makes freezing them affordable.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

__all__ = ["Photo", "Session", "Location", "Person", "Tag"]


@dataclass(frozen=True, slots=True)
class Photo:
    """One imported photo: always a JPEG, optionally with its RAF (D-2)."""

    #: SHA-256 of the image bytes, hex. The identity of the photo (C-3).
    content_hash: str

    #: Relative to the library root, forward-slashed. Never absolute — the
    #: library is expected to move volumes (REQUIREMENTS.md §4.2). Use
    #: ``Catalog.absolute_path`` to get back to disk.
    library_path: str

    #: The camera's name for it, e.g. 'DSCF1024.JPG'. Kept after the rename in
    #: D-7 so a file is still traceable back to the card.
    original_name: str

    captured_at: dt.datetime
    imported_at: dt.datetime

    id: int | None = None

    #: Relative like :attr:`library_path`, and present only when the RAF was
    #: flagged during culling (D-2).
    raw_path: str | None = None

    #: True when EXIF carried no capture date and mtime was used (FR-8.2a).
    #: Surfaced in the migration manifest so an inferred date can be corrected
    #: rather than silently believed.
    captured_at_inferred: bool = False

    width: int | None = None
    height: int | None = None
    camera_model: str | None = None
    lens: str | None = None
    exposure: str | None = None
    f_number: float | None = None
    iso: int | None = None
    focal_length: float | None = None
    orientation: int | None = None

    rating: int = 0
    session_id: int | None = None

    #: False once *Free up Mac space* has moved this photograph's files off the
    #: Mac; the archive drive then holds the only copy.
    on_mac: bool = True

    #: None follows the configured rule; True always keeps it on the Mac; False
    #: removes it at the next free-up regardless.
    keep_on_mac: bool | None = None

    @property
    def has_raw(self) -> bool:
        """Derived, never stored on the instance, so the two cannot disagree.

        The column exists in SQLite to keep FR-7.2's has-RAW filter an index
        lookup; a CHECK constraint keeps it in step with ``raw_path``.
        """
        return self.raw_path is not None


@dataclass(frozen=True, slots=True)
class Session:
    """An outing — the unit that tagging actually operates on (§6.3, FR-3.3)."""

    started_at: dt.datetime
    #: None while a session is still being assembled during an ingest.
    ended_at: dt.datetime | None = None
    id: int | None = None
    location_id: int | None = None
    event: str | None = None


@dataclass(frozen=True, slots=True)
class Location:
    """A named place. There is no GPS to fall back on (C-1)."""

    name: str

    #: Used verbatim in filenames (D-7), which is why it is unique in the
    #: schema. Generating one from a name belongs to WU-3.2; this layer only
    #: stores what it is given.
    slug: str

    id: int | None = None

    #: Reserved for D-5's map. Always None today; here so adding a map later
    #: needs no migration.
    latitude: float | None = None
    longitude: float | None = None

    #: How often this has been applied. Ranks autocomplete (FR-5.1).
    use_count: int = 0


@dataclass(frozen=True, slots=True)
class Person:
    """Someone in a photo. Tagged by hand — face recognition is §11."""

    name: str
    id: int | None = None
    use_count: int = 0


@dataclass(frozen=True, slots=True)
class Tag:
    """A freeform tag: the bag that absorbs whatever the fixed fields miss (D-6)."""

    name: str
    id: int | None = None
    use_count: int = 0
