"""Tagging: the one place where a change lands in both stores or in neither.

D-1 is two stores on purpose. SQLite makes filtering instant; the XMP sidecar
is what survives this application — it is why NFR-2 can call the catalog
rebuildable and why success criterion 4 is "delete the database, rebuild, lose
no tags". FR-5.6 is the rule that keeps the promise: **every change writes to
both, and a failed sidecar is surfaced rather than swallowed.**

:mod:`photo_manager.vocabulary` does the catalog half and deliberately stops
there. This module is the other half, and it exists as its own layer rather
than as a habit callers are asked to remember, because "remember to write the
sidecar too" is not a design — it is a bug waiting for a distracted afternoon.

**The order is chosen so that a crash leaves the sidecars ahead of the
catalog.** Every change here runs as: open a catalog transaction, make the
change, render each affected photograph's sidecar to a temporary, put every
temporary in place, and only then commit. A failure before the renames leaves
nothing changed at all. A crash between the renames and the commit leaves
sidecars saying something the catalog has not caught up with — and that is the
direction to be wrong in, because the catalog is the rebuildable one (NFR-2)
and the sidecars are not.

**Why two phases and not fifty-four one-phase writes.** Tagging a session of 54
frames touches 54 files. The failure worth designing against is the thirtieth:
one-at-a-time, twenty-nine photographs would be left claiming a location that
the catalog and the other twenty-five disagree with, and nothing would say so.
Preparing every sidecar before committing any of them turns that into an error
message and an unchanged library.

**A sidecar with nothing to say is still not written** (§5.2). Untagging the
last tag off a photograph that has a sidecar updates it, because the *removal*
is a change worth recording; a photograph that has never had one does not get
an empty file. Hundreds of files saying "nothing here" would work directly against
success criterion 5.

**Both halves of a RAW pair are written**, which is what keeping the whole
filename in §5.2 was for.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from . import vocabulary
from .catalog import Catalog
from .hashing import content_hash
from .models import Photo
from .sidecars import PendingSidecar, prepare_sidecar, sidecar_for, sidecar_path

__all__ = [
    "TagError",
    "TagOutcome",
    "set_session_location",
    "set_session_event",
    "set_session_people",
    "set_session_tags",
    "set_photo_location",
    "set_photo_people",
    "set_photo_tags",
    "set_photo_rating",
    "resync_sidecars",
]


class TagError(Exception):
    """A tagging change could not be made."""


@dataclass(frozen=True, slots=True)
class TagOutcome:
    """What a change touched — the receipt, not a status code."""

    #: Catalog rows whose tags now differ.
    photo_ids: tuple[int, ...] = ()
    #: Sidecars written or updated on disk.
    sidecars: tuple[Path, ...] = ()
    #: Photographs with nothing to say, so deliberately given no sidecar.
    skipped: int = 0

    @property
    def count(self) -> int:
        return len(self.photo_ids)


# ----------------------------------------------------------------------
# The write-through itself
# ----------------------------------------------------------------------


def _apply(catalog: Catalog, change: Callable[[], Iterable[int]]) -> TagOutcome:
    """Run *change* against the catalog and carry it into the sidecars.

    *change* returns the ids of the photographs whose tags it altered. It runs
    inside the transaction, so everything the sidecars are rendered from sees
    the new state, and nothing is committed until the files are in place.
    """
    prepared: list[PendingSidecar] = []
    #: For each prepared sidecar on the archive drive, its library-relative
    #: path, so the archive's record of it can move with it.
    on_drive: dict[int, str] = {}
    written: list[Path] = []
    skipped = 0

    try:
        with catalog.transaction():
            photo_ids = tuple(dict.fromkeys(change()))

            photos: list[Photo] = []
            for photo_id in photo_ids:
                photo = catalog.photos.get(photo_id)
                if photo is None:  # pragma: no cover — change() just touched it
                    raise TagError(f"No photograph with id {photo_id}")
                photos.append(photo)
            _refuse_if_drive_needed(catalog, photos)

            for photo in photos:
                tags = sidecar_for(catalog, photo)
                for relative, path in _photo_files(catalog, photo):
                    # §5.2: nothing to say and nothing already saying it.
                    if tags.empty and not sidecar_path(path).exists():
                        skipped += 1
                        continue
                    if not photo.on_mac:
                        on_drive[len(prepared)] = sidecar_path(relative).as_posix()
                    prepared.append(prepare_sidecar(path, tags))

            # Renames first, commit second. A crash in between leaves the
            # sidecars ahead of the catalog, which is the recoverable
            # direction: the catalog is rebuildable from them (NFR-2), and
            # nothing rebuilds them from it.
            for index, pending in enumerate(prepared):
                landed = pending.commit()
                written.append(landed)
                if index in on_drive:
                    # Written straight onto the archive drive, so the drive's
                    # copy is the current one: record it as verified.
                    catalog.archive.record(
                        on_drive[index], content_hash(landed), landed.stat().st_size,
                        dt.datetime.now(),
                    )
    except BaseException:
        for pending in prepared:
            pending.discard()
        raise

    return TagOutcome(photo_ids=photo_ids, sidecars=tuple(written), skipped=skipped)


def _photo_files(catalog: Catalog, photo: Photo) -> list[tuple[str, Path]]:
    """The JPEG, and the RAF when it was kept (D-2). Both get a sidecar.

    Each as its stored relative path and where it is now: on the Mac, or on
    the archive drive for a photograph that has been removed from the Mac —
    the sidecar belongs beside the file it describes, wherever that is.
    """
    relatives = [photo.library_path] + ([photo.raw_path] if photo.raw_path else [])
    settings = catalog.settings
    if photo.on_mac:
        return [(relative, settings.resolve(relative)) for relative in relatives]
    found: list[tuple[str, Path]] = []
    for relative in relatives:
        path = settings.resolve_archive(relative)
        assert path is not None  # _refuse_if_drive_needed ran first
        found.append((relative, path))
    return found


def _refuse_if_drive_needed(catalog: Catalog, photos: Sequence[Photo]) -> None:
    """Refuse the whole change, before anything is written, when some of these
    photographs are only on the archive drive and it is not plugged in.

    All or nothing, like every other change here: tagging the forty frames of
    an outing that are on the Mac and quietly skipping the fourteen that are
    not would leave the outing saying two things.
    """
    away = [photo for photo in photos if not photo.on_mac]
    if not away:
        return
    settings = catalog.settings
    missing = [
        photo for photo in away
        if (folder := _archive_folder(settings, photo)) is None or not folder.is_dir()
    ]
    if missing:
        count = len(missing)
        raise TagError(
            f"{count} photograph{'s are' if count != 1 else ' is'} only on the archive "
            f"drive, which is not plugged in. Plug it in to tag "
            f"{'them' if count != 1 else 'it'} — nothing has been changed."
        )


def _archive_folder(settings, photo: Photo) -> Path | None:
    path = settings.resolve_archive(photo.library_path)
    return None if path is None else path.parent


# ----------------------------------------------------------------------
# Sessions — the primary path (FR-3.3)
# ----------------------------------------------------------------------


def set_session_location(catalog: Catalog, session_id: int, name: str | None) -> TagOutcome:
    """Give a whole outing a location in one action (FR-3.3).

    Fifty-four photographs, one typed word. Frames carrying their own override
    keep it (FR-3.4) — but their sidecars are still rewritten, because a
    session with a location is also a session with an *event*, and that reaches
    every frame in it.
    """
    def change() -> Iterable[int]:
        if name is None or not name.strip():
            return vocabulary.clear_session_location(catalog, session_id)
        _, affected = vocabulary.set_session_location(catalog, session_id, name)
        return affected

    return _apply(catalog, change)


def set_session_event(catalog: Catalog, session_id: int, event: str | None) -> TagOutcome:
    """Free text describing the outing (FR-5.3).

    Not a vocabulary: an event is a sentence about one day rather than a word
    reached for again and again, so there is nothing to autocomplete and no
    ``use_count`` to keep. It lives on the session row and reaches every
    photograph through `Iptc4xmpExt:Event`.
    """
    def change() -> Iterable[int]:
        session = catalog.sessions.get(session_id)
        if session is None:
            raise TagError(f"No session with id {session_id}")
        cleaned = None if event is None or not event.strip() else " ".join(event.split())
        catalog.sessions.update(replace(session, event=cleaned))
        return _session_photo_ids(catalog, session_id)

    return _apply(catalog, change)


def set_session_people(
    catalog: Catalog, session_id: int, names: Sequence[str]
) -> TagOutcome:
    """The same people on every photograph of an outing (FR-3.3, FR-5.2).

    People are per photograph in the schema (§5.1) because a person is in a
    frame or is not, so this is genuinely N changes — but it is **one action**,
    which is what FR-3.3 asks for. It *replaces* the set on each frame rather
    than adding to it, so the panel shows what will be true afterwards rather
    than what will be added to whatever is already there.
    """
    return _for_each(catalog, session_id, lambda photo_id: _set_people(catalog, photo_id, names))


def set_session_tags(catalog: Catalog, session_id: int, names: Sequence[str]) -> TagOutcome:
    """The same freeform tags across an outing (FR-3.3, FR-5.4)."""
    return _for_each(catalog, session_id, lambda photo_id: _set_tags(catalog, photo_id, names))


# ----------------------------------------------------------------------
# Single photographs — the exception (FR-3.4)
# ----------------------------------------------------------------------


def set_photo_location(catalog: Catalog, photo_id: int, name: str | None) -> TagOutcome:
    """Override one frame's location, or clear the override (FR-3.4).

    Clearing gives the session's answer back rather than leaving a hole, which
    is what makes this precedence rather than replacement.
    """
    def change() -> Iterable[int]:
        if name is None or not name.strip():
            return vocabulary.clear_photo_location(catalog, photo_id)
        _, affected = vocabulary.set_photo_location(catalog, photo_id, name)
        return affected

    return _apply(catalog, change)


def set_photo_people(catalog: Catalog, photo_id: int, names: Sequence[str]) -> TagOutcome:
    return _apply(catalog, lambda: _set_people(catalog, photo_id, names))


def set_photo_tags(catalog: Catalog, photo_id: int, names: Sequence[str]) -> TagOutcome:
    return _apply(catalog, lambda: _set_tags(catalog, photo_id, names))


def set_photo_rating(catalog: Catalog, photo_id: int, rating: int) -> TagOutcome:
    """0-5 (FR-5.5). 0 is a real answer — unrated — not an absence."""
    if not 0 <= rating <= 5:
        raise TagError(f"A rating is 0-5, got {rating}")

    def change() -> Iterable[int]:
        photo = catalog.photos.get(photo_id)
        if photo is None:
            raise TagError(f"No photograph with id {photo_id}")
        catalog.photos.update(replace(photo, rating=rating))
        return [photo_id]

    return _apply(catalog, change)


# ----------------------------------------------------------------------
# Repair
# ----------------------------------------------------------------------


def resync_sidecars(catalog: Catalog, photo_ids: Sequence[int] | None = None) -> TagOutcome:
    """Rewrite sidecars from the catalog, for the named photographs or all.

    The recovery path for the one window this design leaves open: a crash
    between putting sidecars in place and committing the catalog, and anything
    that wrote the catalog without coming through here — `vocabulary` on its
    own, or a cull rating applied before this module existed.

    It is deliberately one-directional. Reading *back* from sidecars is the
    catalog rebuild, which is WU-3.5's, and a function that guessed which side
    was fresher would be a function that silently picked one.
    """
    def change() -> Iterable[int]:
        if photo_ids is not None:
            return list(photo_ids)
        return [p.id for p in catalog.photos.all() if p.id is not None]

    return _apply(catalog, change)


# ----------------------------------------------------------------------
# Small shared pieces
# ----------------------------------------------------------------------


def _for_each(
    catalog: Catalog, session_id: int, per_photo: Callable[[int], Iterable[int]]
) -> TagOutcome:
    def change() -> Iterable[int]:
        photo_ids = _session_photo_ids(catalog, session_id)
        if not photo_ids and catalog.sessions.get(session_id) is None:
            raise TagError(f"No session with id {session_id}")
        touched: list[int] = []
        for photo_id in photo_ids:
            touched.extend(per_photo(photo_id))
        return touched

    return _apply(catalog, change)


def _session_photo_ids(catalog: Catalog, session_id: int) -> list[int]:
    return [p.id for p in catalog.photos.in_session(session_id) if p.id is not None]


def _set_people(catalog: Catalog, photo_id: int, names: Sequence[str]) -> list[int]:
    """Make the photograph's people exactly *names*, adding and removing.

    Resolving happens first, so a typo that cannot be a name fails before
    anything has been removed.
    """
    wanted = {}
    for name in names:
        if not name.strip():
            continue
        person = vocabulary.resolve_person(catalog, name)
        assert person.id is not None
        wanted[person.id] = person

    current = {person.id for person in catalog.photos.people(photo_id)}
    for person_id in current - set(wanted):
        vocabulary.remove_person_from_photo(catalog, photo_id, person_id)
    for person_id, person in wanted.items():
        if person_id not in current:
            vocabulary.add_person_to_photo(catalog, photo_id, person.name)
    return [photo_id]


def _set_tags(catalog: Catalog, photo_id: int, names: Sequence[str]) -> list[int]:
    """Make the photograph's tags exactly *names*."""
    wanted = {}
    for name in names:
        if not name.strip():
            continue
        tag = vocabulary.resolve_tag(catalog, name)
        assert tag.id is not None
        wanted[tag.id] = tag

    current = {tag.id for tag in catalog.photos.tags(photo_id)}
    for tag_id in current - set(wanted):
        vocabulary.remove_tag_from_photo(catalog, photo_id, tag_id)
    for tag_id, tag in wanted.items():
        if tag_id not in current:
            vocabulary.add_tag_to_photo(catalog, photo_id, tag.name)
    return [photo_id]
