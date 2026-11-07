"""Rebuilding the catalog from the library and its sidecars (WU-3.5).

This is the unit that makes the rest of the design safe. NFR-2 calls the
catalog an **index** rather than the truth, and success criterion 4 says the
SQLite file can be deleted and reconstructed with no tag loss. Every other
decision in this project leans on that: forward-only migrations with no
down-migration, because the recovery path is a rebuild; a cull decision kept
*out* of the catalog because nothing in the library could reconstruct it; a
sidecar written inside the photograph's transaction, because a tag that exists
only in a database is a tag that a rebuild would lose.

Until now all of that was a promise. This module is where it is checked.

**Four sources, and knowing which is which is the whole job.**

1. **The file's bytes** — the content hash, which is the photograph's identity.
2. **Its EXIF** — capture time, camera, lens, exposure, dimensions. Facts the
   camera wrote and nobody edited.
3. **Where it sits** — `YYYY/YYYY-MM-DD/` is D-4, and a dated folder is a
   *decision that was recorded*, not a guess. It is the only place some things
   survive; see :func:`_capture_time`.
4. **Its sidecar** — everything a person typed. Most photographs do not have
   one, because §5.2 says a sidecar with nothing to say is not written, so the
   rebuild has to work from files first and treat tags as the overlay they are.

**What cannot come back, stated rather than papered over.**

- **`imported_at`** is not in any of the four. The file's mtime is the card's
  mtime (`copy2` preserves it), so it is not the import date either. It is
  filled from the mtime and reported, because a plausible wrong timestamp
  nobody mentions is worse than an obviously approximate one that is written
  down.
- **The shape of a session** — *which* frames a person judged to be one outing
  — has no XMP property and is reconstructed from the evidence that does exist
  (see :func:`_sessions`). A day tagged as one outing comes back as one. A day
  whose halves were tagged differently comes back as two. A day split but never
  tagged comes back as one, because nothing recorded the split. That is work
  lost, not tags lost, and success criterion 4 is about tags.
- **A per-photograph override that agreed with its session** is indistinguishable
  from no override at all, because a sidecar records the location that applies
  and not where it came from. The *effective* location is identical either way,
  so nothing a person would notice is lost.

**It writes a new catalog beside the old one and never in place.** The caller
compares the two and swaps deliberately — the same two-step habit as the
migration and the import, and for the same reason: something that rewrites the
index of an irreplaceable library should not do it as a side effect of being
run.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import db
from .catalog import Catalog
from .config import Settings
from .hashing import content_hash
from .metadata import DateSource, MetadataError, PhotoMetadata, read_metadata, resolve_capture_date
from .models import Location, Person, Photo, Session, Tag
from .sidecars import Sidecar, read_sidecar, sidecar_path
from .vocabulary import resolve_location, resolve_person, resolve_tag

__all__ = [
    "RebuildError",
    "Unreadable",
    "Rebuilt",
    "PhotoTags",
    "Difference",
    "Comparison",
    "rebuild",
    "tags_in",
    "compare",
    "original_name_for",
]

_JPEG_MAGIC = b"\xff\xd8\xff"
_RAF_MAGIC = b"FUJIFILMCCD-RAW"

#: `2026/2026-09-06` — D-4's layout, which is also evidence (see _capture_time).
_DATED_FOLDER = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: `2026-09-06_harbour-market_DSCF1006` — D-7's name, to be read backwards.
_D7_NAME = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<rest>.+)$")

#: What :func:`photo_manager.vocabulary.slugify` can produce, and nothing else.
#: Used to tell a slug segment from an original filename stem.
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: FR-6.5's collision suffix, which `suffixed()` appends and which is not part
#: of what the camera called the file. Only ever stripped from a D-7 name,
#: where the stem is always camera-written — `Birthday_1.jpg` keeps its
#: `_1`, because that name is not D-7's shape and was never renamed (FR-8.1).
_COLLISION_SUFFIX = re.compile(r"^(?P<stem>.+)_(?P<suffix>\d+)$")


class RebuildError(Exception):
    """A rebuild could not be carried out."""


@dataclass(frozen=True, slots=True)
class Unreadable:
    """A file that looked like a photograph and could not be read (FR-1.3c)."""

    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class Rebuilt:
    """What a rebuild produced, and everything it could not bring back."""

    database_path: Path
    library_root: Path
    photographs: int = 0
    sessions: int = 0
    locations: int = 0
    people: int = 0
    tags: int = 0
    #: Photographs whose tags came from a sidecar. The rest had none, because
    #: there was nothing to say about them (§5.2).
    sidecars_read: int = 0
    #: Capture times that came from the dated folder or from mtime (FR-8.2a).
    inferred_dates: int = 0
    seconds: float = 0.0
    bytes_hashed: int = 0
    unreadable: tuple[Unreadable, ...] = ()
    #: Files whose bytes are identical to another photograph's. The catalog
    #: keys on content hash (C-3), so only the first gets a row.
    duplicates: tuple[Unreadable, ...] = ()
    #: Plain sentences about things this rebuild could not recover. Always
    #: reported, never suppressed when empty — "nothing was lost" is a fact
    #: worth reading, the same argument as FR-6.6's zeroes.
    notes: tuple[str, ...] = ()


# ----------------------------------------------------------------------
# The rebuild
# ----------------------------------------------------------------------


def rebuild(
    settings: Settings,
    into: str | Path,
    *,
    progress: Callable[[int, int, Path], None] | None = None,
) -> Rebuilt:
    """Reconstruct a whole catalog at *into* from the library at *settings*.

    Reads the library and writes nothing inside it. *into* must not be the
    configured catalog: a rebuild is compared before it is adopted, and one
    that overwrote the thing it is meant to be checked against would leave
    nothing to check against.
    """
    import time

    root = settings.verify_library_root()
    destination = Path(into)
    if settings.database_path is not None and destination == Path(settings.database_path):
        raise RebuildError(
            f"A rebuild writes a new catalog beside the old one, never over it. "
            f"{destination} is the configured catalog."
        )
    if destination.exists():
        raise RebuildError(f"{destination} already exists; rebuild into a fresh path.")

    started = time.perf_counter()
    archive_root = _archive_root(settings)
    groups, unreadable_paths = _scan_both(root, archive_root)

    read: list[_Read] = []
    unreadable = list(unreadable_paths)
    total = len(groups)
    for index, (_, (group_root, paths)) in enumerate(sorted(groups.items()), start=1):
        if progress is not None:
            progress(index, total, paths[0])
        try:
            item = _read_group(paths, group_root)
        except (MetadataError, OSError) as exc:
            unreadable.extend(Unreadable(path, str(exc)) for path in paths)
            continue
        read.append(item if group_root == root else replace(item, on_mac=False))

    read.sort(key=lambda item: (item.captured_at, item.library_path))
    read, duplicates = _drop_duplicates(read)

    rebuilt_settings = _pointing_at(settings, destination)
    with Catalog.open(rebuilt_settings, verify_root=False) as catalog:
        with catalog.transaction():
            rows = [_write_photo(catalog, item) for item in read]
            sessions = _sessions(catalog, read, rows)
            _vocabulary(catalog, read, rows)
            if archive_root is not None:
                _archive_records(catalog, read, archive_root)

    return Rebuilt(
        database_path=destination,
        library_root=root,
        photographs=len(rows),
        sessions=sessions,
        locations=len({item.sidecar.location for item in read if item.sidecar.location}),
        people=len({name for item in read for name in item.sidecar.people}),
        tags=len({name for item in read for name in item.sidecar.tags}),
        sidecars_read=sum(1 for item in read if item.had_sidecar),
        inferred_dates=sum(1 for item in read if item.captured_at_inferred),
        seconds=time.perf_counter() - started,
        bytes_hashed=sum(item.total_bytes for item in read),
        unreadable=tuple(unreadable),
        duplicates=tuple(duplicates),
        notes=_notes(read, duplicates),
    )


def _archive_root(settings: Settings) -> Path | None:
    """The archive drive, when one is configured and attached.

    Refuses when it is configured, not attached, and the current catalog says
    some photographs are only on it: a rebuild then would be missing them,
    and the comparison would rightly refuse to adopt it anyway — better said
    plainly before an hour of hashing than after.
    """
    from . import archive  # here, because archive imports the catalog layer

    if settings.archive_root is None:
        return None
    try:
        return archive.check_archive(settings)
    except archive.ArchiveError as exc:
        reason = str(exc)
    database = settings.database_path
    if database is not None and Path(database).exists():
        with Catalog.open(settings, verify_root=False) as current:
            away = current.connection.execute(
                "SELECT count(*) FROM photo WHERE on_mac = 0"
            ).fetchone()[0]
        if away:
            raise RebuildError(
                f"{away} photograph(s) are only on the archive drive, so a rebuild "
                f"without it would lose them. Plug it in first. ({reason})"
            )
    return None


def _scan_both(
    root: Path, archive_root: Path | None
) -> tuple[dict[tuple[str, str], tuple[Path, list[Path]]], list[Unreadable]]:
    """Every photograph on the Mac, plus every one only on the archive drive.

    Keyed by the folder *relative to its root* and the stem, so the Mac and
    the drive — a mirror at the same paths — describe one photograph with one
    key. Where both have it, the Mac's copy is read: it is the faster disk,
    and the archive run has already checked that the two are the same bytes.
    """
    groups: dict[tuple[str, str], tuple[Path, list[Path]]] = {}
    mac, unreadable = _scan(root)
    for (folder, stem), paths in mac.items():
        groups[(Path(folder).relative_to(root).as_posix(), stem)] = (root, paths)
    if archive_root is None:
        return groups, unreadable

    drive, drive_unreadable = _scan(archive_root)
    for (folder, stem), paths in drive.items():
        key = (Path(folder).relative_to(archive_root).as_posix(), stem)
        groups.setdefault(key, (archive_root, paths))
    # Something odd on the drive is worth reporting once, not once per copy.
    unreadable.extend(
        entry for entry in drive_unreadable
        if not (root / entry.path.relative_to(archive_root)).exists()
    )
    return groups, unreadable


def _archive_records(catalog: Catalog, read: Sequence[_Read], archive_root: Path) -> None:
    """Note as archived the photographs that were read from the drive.

    Their bytes were just hashed off the drive, so the record is earned. A
    photograph read from the Mac gets none here: whether the drive's copy
    matches is the next *Archive now*'s question, and it answers it without
    copying anything.
    """
    now = dt.datetime.now()
    for item in read:
        if item.on_mac:
            continue
        catalog.archive.record(item.library_path, item.content_hash,
                               (archive_root / item.library_path).stat().st_size, now)
        extras = [item.raw_path] if item.raw_path else []
        extras += [sidecar_path(p).as_posix() for p in (item.library_path, item.raw_path) if p]
        for relative in extras:
            path = archive_root / relative
            if path.is_file():
                catalog.archive.record(relative, content_hash(path), path.stat().st_size, now)


def _drop_duplicates(read: list[_Read]) -> tuple[list[_Read], list[Unreadable]]:
    """Keep the first of any byte-identical files, and name the rest.

    ``content_hash`` is UNIQUE because it is the photograph's identity (C-3),
    so two identical files cannot both have a row. Nothing this app does
    creates that — the import discards a duplicate after copying it (FR-6.7)
    and the migration refuses one — but a library is a folder that a person can
    put things in, and **a recovery tool is run precisely when something has
    already gone wrong**. Falling over with a UNIQUE constraint error at that
    moment would be the worst possible time to be brittle (FR-1.3c).

    The kept row still describes the bytes, so nothing is *lost*; what the
    second file loses is its own row, and it is reported by name for that
    reason. *Which* one is kept is arbitrary — they are the same photograph —
    but it is the first in (capture time, library path) order and therefore the
    same on every run, because "rebuild if in doubt" has to mean the same thing
    twice.
    """
    seen: dict[str, str] = {}
    kept: list[_Read] = []
    duplicates: list[Unreadable] = []
    for item in read:
        first = seen.get(item.content_hash)
        if first is None:
            seen[item.content_hash] = item.library_path
            kept.append(item)
        else:
            duplicates.append(Unreadable(
                Path(item.library_path),
                f"byte-identical to {first}; the catalog keys on content (C-3)",
            ))
    return kept, duplicates


def _pointing_at(settings: Settings, destination: Path) -> Settings:
    from dataclasses import replace as _replace

    return _replace(settings, database_path=destination)


# ----------------------------------------------------------------------
# Reading the library
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Read:
    """One photograph, assembled from all four sources."""

    library_path: str
    raw_path: str | None
    content_hash: str
    metadata: PhotoMetadata
    captured_at: dt.datetime
    captured_at_inferred: bool
    date_source: str
    mtime: dt.datetime
    total_bytes: int
    sidecar: Sidecar
    had_sidecar: bool
    lone_raw: bool = False
    #: False when this photograph was read from the archive drive because the
    #: Mac no longer has it.
    on_mac: bool = True


def _scan(root: Path) -> tuple[dict[tuple[str, str], list[Path]], list[Unreadable]]:
    """Every image file under *root*, grouped into photographs.

    Grouped by folder and casefolded stem, which is FR-1.2's pairing rule —
    case-insensitively, because the library holds ``Birthday.RAF`` beside
    ``birthday.JPG`` and they are one photograph. Folder is part of the
    key because two days may both hold a ``DSCF0007``.
    """
    groups: dict[tuple[str, str], list[Path]] = defaultdict(list)
    unreadable: list[Unreadable] = []

    for path in sorted(root.rglob("*"), key=lambda p: str(p).casefold()):
        if not path.is_file():
            continue
        if path.suffix.lower() == ".xmp":
            continue  # read with its photograph, never on its own
        if path.name.endswith(".partial"):
            continue  # an unfinished copy, never a photograph in its own right
        kind = _file_kind(path)
        if kind is None:
            # Not silently skipped: something that is in the library and is not
            # a photograph is worth a human knowing about.
            unreadable.append(
                Unreadable(path, "not a JPEG or RAF (checked by magic bytes, FR-1.3a)")
            )
            continue
        groups[(str(path.parent), path.stem.casefold())].append(path)

    return groups, unreadable


def _file_kind(path: Path) -> str | None:
    """'jpeg', 'raf' or None, by magic bytes and never by suffix (FR-1.3a)."""
    try:
        with path.open("rb") as stream:
            magic = stream.read(len(_RAF_MAGIC))
    except OSError:
        return None
    if magic.startswith(_RAF_MAGIC):
        return "raf"
    if magic.startswith(_JPEG_MAGIC):
        return "jpeg"
    return None


def _read_group(paths: Sequence[Path], root: Path) -> _Read:
    """One photograph: its JPEG, its RAF if it kept one, and its sidecar."""
    kinds = {path: _file_kind(path) for path in paths}
    jpegs = [path for path in paths if kinds[path] == "jpeg"]
    raws = [path for path in paths if kinds[path] == "raf"]

    # A lone RAF is not ordinary (FR-1.2) but it is still a photograph in the
    # library, and a rebuild that dropped it would leave a file with no row —
    # breaking the very thing an index is for. It becomes its own photograph
    # and is named in the notes.
    lone_raw = not jpegs
    primary = jpegs[0] if jpegs else raws[0]
    raw = raws[0] if (raws and not lone_raw) else None

    metadata = read_metadata(primary)
    raw_metadata = read_metadata(raw) if raw is not None else None
    mtime = dt.datetime.fromtimestamp(primary.stat().st_mtime).replace(microsecond=0)

    captured_at, source = _capture_time(primary, root, metadata, raw_metadata, mtime)

    sidecar, had_sidecar = _tags_beside(primary, raw)

    return _Read(
        library_path=_relative(primary, root),
        raw_path=None if raw is None else _relative(raw, root),
        content_hash=content_hash(primary),
        metadata=metadata,
        captured_at=captured_at,
        captured_at_inferred=source != DateSource.EXIF and source != DateSource.PAIRED_RAW,
        date_source=source,
        mtime=mtime,
        total_bytes=sum(path.stat().st_size for path in paths),
        sidecar=sidecar,
        had_sidecar=had_sidecar,
        lone_raw=lone_raw,
    )


#: A capture time that came from the dated folder the file is filed under.
FOLDER = "folder"


def _capture_time(
    path: Path,
    root: Path,
    metadata: PhotoMetadata,
    raw_metadata: PhotoMetadata | None,
    mtime: dt.datetime,
) -> tuple[dt.datetime, str]:
    """When the shutter fired, from the best source that has an answer.

    EXIF first, then a paired RAF's EXIF — that is
    :func:`~photo_manager.metadata.resolve_capture_date`, unchanged, because
    two copies of FR-8.2a's rule would be two chances to disagree.

    **Then the dated folder, before mtime**, which is this module's own
    addition and the reason it recovers something mtime would lose. D-4 files a
    photograph under its capture date, so the folder is a *decision that was
    recorded* — including a decision a human made at the migration review. The
    real library has exactly one such file: `Birthday_1.jpg` has no EXIF
    date at all, its mtime is 2026-04-03, and it sits in `2026-04-02/` because
    the owner said so at the WU-1.5 gate. mtime alone would quietly move it a day
    and undo that decision; the folder gives it back.

    The time of day still comes from mtime, since a folder only knows a date.
    Both are guesses, so both set FR-8.2a's inferred flag either way — what
    changes is only which guess is better.
    """
    resolved, source = resolve_capture_date(metadata, mtime, raw_metadata)
    if source != DateSource.MTIME:
        return resolved, source

    folder = path.parent.name
    if _DATED_FOLDER.match(folder):
        try:
            day = dt.date.fromisoformat(folder)
        except ValueError:  # pragma: no cover — the regex has already checked
            return resolved, source
        if day != mtime.date():
            return dt.datetime.combine(day, mtime.time()), FOLDER
    return resolved, source


def _tags_beside(primary: Path, raw: Path | None) -> tuple[Sidecar, bool]:
    """The photograph's tags, from its sidecar or its RAF's.

    Both halves of a pair get one and they agree (§5.2), so either will do —
    but if the JPEG's has been lost and the RAF's has not, reading the RAF's is
    the difference between recovering the tags and losing them.
    """
    for candidate in (primary, raw):
        if candidate is None:
            continue
        found = read_sidecar(candidate)
        if found is not None:
            return found, True
    return Sidecar(), False


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


# ----------------------------------------------------------------------
# Writing it back out
# ----------------------------------------------------------------------


def _write_photo(catalog: Catalog, item: _Read) -> Photo:
    return catalog.photos.add(
        Photo(
            content_hash=item.content_hash,
            library_path=item.library_path,
            raw_path=item.raw_path,
            original_name=original_name_for(item.library_path),
            captured_at=item.captured_at,
            captured_at_inferred=item.captured_at_inferred,
            # Not recoverable from anything on disk; see the module docstring.
            imported_at=item.mtime,
            width=item.metadata.width,
            height=item.metadata.height,
            camera_model=item.metadata.camera_model,
            lens=item.metadata.lens,
            exposure=item.metadata.exposure,
            f_number=item.metadata.f_number,
            iso=item.metadata.iso,
            focal_length=item.metadata.focal_length,
            orientation=item.metadata.orientation,
            rating=item.sidecar.rating,
            on_mac=item.on_mac,
        )
    )


def original_name_for(library_path: str) -> str:
    """The camera's name for a file, read back out of D-7's filename.

    ``2026-09-06_harbour-market_DSCF1006.jpg`` → ``DSCF1006.JPG``. The migration
    deliberately did **not** rename (FR-8.1), so a name that is not D-7's shape
    is already the original and is returned untouched — which is most of this
    library, and includes every hand-named file.

    The extension is upper-cased for a D-7 name because D-7 lower-cases what
    the camera wrote (FR-6.3). Getting this wrong is not free but it is bounded:
    ``original_name`` drives FR-1.4's *cheap* dedup tier, which may skip a
    photograph but never discard one — the hash still settles it, and a wrong
    guess costs a re-read rather than a duplicate.
    """
    name = Path(library_path).name
    stem, dot, extension = name.rpartition(".")
    if not dot:
        return name

    match = _D7_NAME.match(stem)
    if match is None:
        return name

    rest = match.group("rest")
    head, sep, tail = rest.partition("_")
    # A slug is lower-case letters, digits and hyphens and nothing else, so it
    # cannot be mistaken for 'DSCF1006'. Without a slug there is no separator
    # at all and the remainder is the original stem.
    original = tail if (sep and _SLUG.match(head)) else rest

    # A pair is suffixed as a unit when its name is taken (FR-6.5), and the
    # import records what the *camera* called it rather than what the library
    # had to call it. Two photographs legitimately sharing an original name is
    # the whole reason the suffix exists.
    collision = _COLLISION_SUFFIX.match(original)
    if collision is not None:
        original = collision.group("stem")
    return f"{original}.{extension.upper()}"


def _sessions(catalog: Catalog, read: Sequence[_Read], rows: Sequence[Photo]) -> int:
    """Reconstruct the outings from the only evidence there is.

    A session's *shape* has no XMP property (§6.3), so it cannot simply be read
    back. What the sidecars do carry is what each photograph was tagged with,
    and that is evidence about which frames were dealt with together:

    * **The event is a session's own field**, so a change of event within a day
      is a change of session. A day whose halves carry different events comes
      back as two outings.
    * **A day with nothing tagged comes back as one session**, which is FR-3.1's
      default — and the honest answer, because nothing recorded a split.
    * **A located and an unlocated photograph are not put in one session**,
      because the one without would then inherit a location its sidecar never
      claimed. That is the one way this could invent a tag rather than lose one,
      and inventing is worse.

    Within a run, the session takes the **most common** location and the
    photographs that disagree get FR-3.4 overrides — which is exactly the shape
    "tag the outing, then correct one frame" produces, recovered without
    anything having recorded it.
    """
    by_photo = {item.library_path: row for item, row in zip(read, rows)}
    by_day: dict[dt.date, list[_Read]] = defaultdict(list)
    for item in read:
        by_day[item.captured_at.date()].append(item)

    made = 0
    for day in sorted(by_day):
        for run in _runs(sorted(by_day[day], key=lambda i: (i.captured_at, i.library_path))):
            location_name = _majority(item.sidecar.location for item in run)
            location = (
                None if location_name is None
                else resolve_location(catalog, location_name)
            )
            if location is not None:
                _carry_coordinates(catalog, location, run)

            session = catalog.sessions.add(
                Session(
                    started_at=run[0].captured_at,
                    ended_at=run[-1].captured_at,
                    location_id=None if location is None else location.id,
                    event=run[0].sidecar.event,
                )
            )
            assert session.id is not None
            made += 1

            catalog.sessions.assign(
                session.id, [by_photo[item.library_path].id for item in run]
            )
            for item in run:
                if item.sidecar.location is None or item.sidecar.location == location_name:
                    continue
                override = resolve_location(catalog, item.sidecar.location)
                assert override.id is not None
                photo_id = by_photo[item.library_path].id
                assert photo_id is not None
                catalog.photos.set_location(photo_id, override.id)
    return made


def _runs(day: Sequence[_Read]) -> list[list[_Read]]:
    """One day's photographs, cut where the outing evidently changed."""
    runs: list[list[_Read]] = []
    for item in day:
        key = (item.sidecar.event, item.sidecar.location is None)
        if runs and _key(runs[-1][-1]) == key:
            runs[-1].append(item)
        else:
            runs.append([item])
    return runs


def _key(item: _Read) -> tuple[str | None, bool]:
    return (item.sidecar.event, item.sidecar.location is None)


def _majority(names: Iterable[str | None]) -> str | None:
    """The commonest name, earliest winning a tie. None if there is none."""
    counted = Counter(name for name in names if name is not None)
    if not counted:
        return None
    best = max(counted.values())
    for name in counted:  # Counter preserves insertion order, so this is the
        if counted[name] == best:  # earliest of the joint-commonest.
            return name
    return None  # pragma: no cover — counted is non-empty


def _carry_coordinates(catalog: Catalog, location: Location, run: Sequence[_Read]) -> None:
    """Put a sidecar's coordinates back on the location row (D-5).

    Always None today — the camera writes no GPS (C-1) — but the columns exist
    from day one so a map needs no migration, and a rebuild that dropped them
    would make that promise false the moment they were used.
    """
    from dataclasses import replace as _replace

    for item in run:
        if item.sidecar.latitude is None or location.latitude is not None:
            continue
        catalog.locations.update(
            _replace(location, latitude=item.sidecar.latitude,
                     longitude=item.sidecar.longitude)
        )
        return


def _vocabulary(catalog: Catalog, read: Sequence[_Read], rows: Sequence[Photo]) -> None:
    """People and tags, straight off each photograph's sidecar."""
    for item, row in zip(read, rows):
        assert row.id is not None
        for name in item.sidecar.people:
            person = resolve_person(catalog, name)
            assert person.id is not None
            catalog.photos.add_person(row.id, person.id)
            catalog.people.increment_use(person.id)
        for name in item.sidecar.tags:
            tag = resolve_tag(catalog, name)
            assert tag.id is not None
            catalog.photos.add_tag(row.id, tag.id)
            catalog.tags.increment_use(tag.id)


def _notes(read: Sequence[_Read], duplicates: Sequence[Unreadable] = ()) -> tuple[str, ...]:
    """What this rebuild could not bring back, in sentences.

    Reported even when there is nothing to say, because FR-6.6's argument about
    zeroes applies here too: "imported_at was approximated for 300 rows" is a
    fact somebody should read, not a warning to be hidden when convenient.
    """
    notes = [
        f"imported_at is not recoverable and was taken from each file's "
        f"modification time for all {len(read)} photographs.",
        "Session shapes were reconstructed from the sidecars' events and "
        "locations; a split that was never tagged cannot be recovered.",
    ]
    from_folder = [item for item in read if item.date_source == FOLDER]
    if from_folder:
        notes.append(
            f"{len(from_folder)} capture date(s) came from the dated folder rather "
            f"than mtime, which preserves a decision mtime would have lost: "
            + ", ".join(sorted(Path(i.library_path).name for i in from_folder))
        )
    if duplicates:
        notes.append(
            f"{len(duplicates)} file(s) are byte-identical to another photograph and "
            f"have no row of their own: "
            + ", ".join(sorted(str(d.path) for d in duplicates))
        )
    away = [item for item in read if not item.on_mac]
    if away:
        notes.append(
            f"{len(away)} photograph(s) are only on the archive drive and were read from it."
        )
    lone = [item for item in read if item.lone_raw]
    if lone:
        notes.append(
            f"{len(lone)} RAF file(s) have no JPEG and were catalogued on their own: "
            + ", ".join(sorted(Path(i.library_path).name for i in lone))
        )
    return tuple(notes)


# ----------------------------------------------------------------------
# Comparing — the acceptance criterion, made measurable
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PhotoTags:
    """Everything about a photograph that a person put there.

    Deliberately *not* everything in the row. Success criterion 4 is about tag
    loss, and comparing `imported_at` — which the module docstring says up front
    cannot come back — would bury the answer in noise that is already known.
    The *effective* location is used rather than the override, because that is
    what a person sees and what the sidecar records.
    """

    rating: int = 0
    location: str | None = None
    event: str | None = None
    people: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return self == PhotoTags()


def tags_in(catalog: Catalog) -> dict[str, PhotoTags]:
    """Every photograph's tags, keyed by content hash.

    The hash is the key because it is the photograph's identity (C-3) and is
    the one thing a rebuild cannot get wrong without the bytes having changed.
    """
    result: dict[str, PhotoTags] = {}
    for photo in catalog.photos.all():
        assert photo.id is not None
        location = catalog.photos.location(photo.id)
        session = (
            None if photo.session_id is None else catalog.sessions.get(photo.session_id)
        )
        result[photo.content_hash] = PhotoTags(
            rating=photo.rating,
            location=None if location is None else location.name,
            event=None if session is None else session.event,
            people=tuple(sorted(p.name for p in catalog.photos.people(photo.id))),
            tags=tuple(sorted(t.name for t in catalog.photos.tags(photo.id))),
        )
    return result


@dataclass(frozen=True, slots=True)
class Difference:
    """One photograph whose tags did not survive unchanged."""

    content_hash: str
    library_path: str
    before: PhotoTags
    after: PhotoTags

    def fields(self) -> list[str]:
        return [
            name for name in ("rating", "location", "event", "people", "tags")
            if getattr(self.before, name) != getattr(self.after, name)
        ]


@dataclass(frozen=True, slots=True)
class Comparison:
    """Two catalogs, and whether any tag failed to make the crossing."""

    compared: int = 0
    #: In the original and not in the rebuild — a photograph that was lost.
    missing: tuple[str, ...] = ()
    #: In the rebuild and not the original — a file the old catalog had no row
    #: for, which is the index having drifted rather than the rebuild inventing.
    added: tuple[str, ...] = ()
    changed: tuple[Difference, ...] = ()

    @property
    def lossless(self) -> bool:
        """Success criterion 4, as a single boolean."""
        return not self.missing and not self.changed

    def summary(self) -> str:
        parts = [
            f"{self.compared} photographs compared",
            f"{len(self.missing)} lost",
            f"{len(self.changed)} with changed tags",
            f"{len(self.added)} new to the catalog",
        ]
        return ", ".join(parts)


def compare(before: Catalog, after: Catalog) -> Comparison:
    """What the rebuild did to the tags. The acceptance criterion, measured."""
    original = tags_in(before)
    rebuilt = tags_in(after)
    paths = {photo.content_hash: photo.library_path for photo in before.photos.all()}

    changed = [
        Difference(digest, paths.get(digest, "?"), original[digest], rebuilt[digest])
        for digest in original
        if digest in rebuilt and original[digest] != rebuilt[digest]
    ]
    return Comparison(
        compared=len(original),
        missing=tuple(sorted(digest for digest in original if digest not in rebuilt)),
        added=tuple(sorted(digest for digest in rebuilt if digest not in original)),
        changed=tuple(changed),
    )
