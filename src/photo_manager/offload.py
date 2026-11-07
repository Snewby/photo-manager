"""Free up Mac space: let go of photographs the archive drive holds.

The Mac is a working set (see :mod:`photo_manager.archive`). This is the step
that makes it one — and the only thing in this app that removes a photograph
from the library folder, so it is built the way the import and the migrations
are: **a plan that changes nothing, then a separate step that acts on it.**

**What goes is decided by a rule, never by a click on a folder.** A photograph
stays on the Mac when any of these is true:

* it was taken within ``keep_on_mac_days`` (default 90) — what you are still
  working on;
* it is rated ``keep_on_mac_min_rating`` (default 4) or more — what you look at;
* it is pinned with *Keep on Mac*.

Everything else that is **verified on the archive drive** may go, and so may
anything explicitly marked *Remove from Mac*. A photograph not yet archived is
never a candidate, whatever the rule says.

**Checked again at the last moment.** The plan trusts the catalog's record of
what the drive holds; removing does not. Right before a photograph's files
leave the Mac, each one is hashed on both sides. A sidecar whose tags changed
since it was archived is archived again first. Any disagreement about a JPEG or
RAF leaves that photograph where it is and says so.

**To the Trash, not gone.** Files are moved to the system Trash, so a mistake is
a drag back out of it until the Trash is emptied. *Bring back to Mac* is the
in-app way: it copies from the drive, verifies the copy, and pins the
photograph so the next free-up leaves it alone.

**One copy.** A photograph removed from the Mac exists only on the archive
drive. The status strip counts these, every time, rather than letting it be
forgotten.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import archive
from .catalog import Catalog
from .config import Settings
from .hashing import content_hash
from .models import Photo
from .sidecars import sidecar_path

__all__ = [
    "Candidate",
    "OffloadError",
    "OffloadPlan",
    "OffloadResult",
    "RestoreResult",
    "execute_offload",
    "plan_offload",
    "restore_to_mac",
]

Progress = Callable[[int, int, str], None]
Trash = Callable[[str], None]

_LOG_DIR = "offload"


class OffloadError(ValueError):
    """A refusal raised before anything is moved."""


@dataclass(frozen=True)
class Candidate:
    """One photograph the plan would take off the Mac."""

    photo_id: int
    library_path: str
    #: Every file of it on the Mac, relative: JPEG, RAF, and their sidecars.
    files: tuple[str, ...]
    bytes: int
    #: True when it is going because somebody said so, not because of the rule.
    marked: bool = False


@dataclass(frozen=True)
class OffloadPlan:
    """What *Free up Mac space* would do now. Changes nothing to produce."""

    candidates: tuple[Candidate, ...] = ()
    #: Photographs on the Mac, and the bytes their files take there.
    on_mac: int = 0
    mac_bytes: int = 0
    #: Why the others stay: one count per reason, each photograph counted once,
    #: in the order the reasons are checked.
    kept_pinned: int = 0
    kept_recent: int = 0
    kept_rated: int = 0
    #: On the Mac and not yet verified on the drive, so not removable at all.
    not_archived: int = 0

    @property
    def bytes(self) -> int:
        return sum(candidate.bytes for candidate in self.candidates)


@dataclass
class OffloadResult:
    removed: int = 0
    bytes_freed: int = 0
    #: Sidecars archived again on the way, because their tags had changed.
    sidecars_refreshed: int = 0
    failures: list[str] = field(default_factory=list)
    seconds: float = 0.0
    log: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass
class RestoreResult:
    restored: int = 0
    bytes_copied: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


# ----------------------------------------------------------------------
# Planning
# ----------------------------------------------------------------------


def plan_offload(
    settings: Settings, catalog: Catalog, *, today: dt.date | None = None
) -> OffloadPlan:
    """Apply the rule to every photograph on the Mac. Reads; never writes."""
    today = today or dt.date.today()
    recent_from = today - dt.timedelta(days=settings.keep_on_mac_days)
    archived = catalog.archive.archived_photo_ids()

    candidates: list[Candidate] = []
    counts = {"pinned": 0, "recent": 0, "rated": 0, "not_archived": 0}
    on_mac = mac_bytes = 0

    for photo in _photos_on_mac(catalog):
        files, size = _mac_files(settings, photo)
        on_mac += 1
        mac_bytes += size
        if photo.id not in archived:
            counts["not_archived"] += 1
            continue
        if photo.keep_on_mac is True:
            counts["pinned"] += 1
            continue
        marked = photo.keep_on_mac is False
        if not marked:
            if photo.captured_at.date() >= recent_from:
                counts["recent"] += 1
                continue
            if photo.rating >= settings.keep_on_mac_min_rating:
                counts["rated"] += 1
                continue
        assert photo.id is not None
        candidates.append(Candidate(photo.id, photo.library_path, files, size, marked))

    return OffloadPlan(
        candidates=tuple(candidates), on_mac=on_mac, mac_bytes=mac_bytes,
        kept_pinned=counts["pinned"], kept_recent=counts["recent"],
        kept_rated=counts["rated"], not_archived=counts["not_archived"],
    )


def _photos_on_mac(catalog: Catalog) -> list[Photo]:
    from .catalog import PhotoRepository

    rows = catalog.connection.execute(
        f"SELECT {PhotoRepository.column_list()} FROM photo WHERE on_mac = 1 "
        "ORDER BY captured_at, id"
    )
    return [PhotoRepository.from_row(row) for row in rows]


def _relatives(photo: Photo) -> list[str]:
    """Every file a photograph can have, relative: JPEG, RAF, their sidecars."""
    paths = [photo.library_path] + ([photo.raw_path] if photo.raw_path else [])
    return paths + [sidecar_path(path).as_posix() for path in paths]


def _mac_files(settings: Settings, photo: Photo) -> tuple[tuple[str, ...], int]:
    found: list[str] = []
    size = 0
    for relative in _relatives(photo):
        try:
            size += settings.resolve(relative).stat().st_size
        except OSError:
            continue
        found.append(relative)
    return tuple(found), size


# ----------------------------------------------------------------------
# Removing
# ----------------------------------------------------------------------


def execute_offload(
    settings: Settings,
    catalog: Catalog,
    plan: OffloadPlan,
    *,
    progress: Progress | None = None,
    trash: Trash | None = None,
    now: Callable[[], dt.datetime] = dt.datetime.now,
) -> OffloadResult:
    """Take the plan's photographs off the Mac, each only once its every file
    has been matched, byte for byte, against the archive drive.

    Refuses outright — :class:`OffloadError` — when the archive drive is not
    attached: the last-moment check needs it. One photograph that does not
    check out is reported and left on the Mac; the rest carry on.
    """
    started = time.perf_counter()
    try:
        root = archive.check_archive(settings)
    except archive.ArchiveError as exc:
        raise OffloadError(f"Nothing was removed. {exc}") from None
    if trash is None:
        from send2trash import send2trash as trash  # type: ignore[no-redef]

    log = Path(settings.state_dir) / _LOG_DIR / f"{now().strftime('%Y-%m-%d')}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    result = OffloadResult(log=log)

    for index, candidate in enumerate(plan.candidates, start=1):
        if progress is not None:
            progress(index, len(plan.candidates), candidate.library_path)
        _offload_one(settings, catalog, root, candidate, trash, result, log, now)

    result.seconds = time.perf_counter() - started
    return result


def _offload_one(
    settings: Settings, catalog: Catalog, root: Path, candidate: Candidate,
    trash: Trash, result: OffloadResult, log: Path, now: Callable[[], dt.datetime],
) -> None:
    photo = catalog.photos.get(candidate.photo_id)
    if photo is None or not photo.on_mac:
        return  # removed, or already off the Mac, since the plan was made
    if photo.id not in catalog.archive.archived_photo_ids():
        result.failures.append(f"{photo.library_path}: no longer verified on the archive drive")
        return

    files, size = _mac_files(settings, photo)
    try:
        for relative in files:
            _confirm_on_drive(settings, catalog, root, photo, relative, result, now)
    except _Mismatch as problem:
        result.failures.append(f"{photo.library_path}: {problem} — left on the Mac")
        return
    except OSError as exc:
        result.failures.append(f"{photo.library_path}: {exc} — left on the Mac")
        return

    # Sidecars and the RAF first, the JPEG last: if the Trash refuses part way,
    # what is left on the Mac is still a photograph rather than a stray sidecar.
    order = sorted(files, key=lambda path: path == photo.library_path)
    moved: list[str] = []
    try:
        for relative in order:
            trash(str(settings.resolve(relative)))
            moved.append(relative)
    except Exception as exc:  # send2trash raises its own OSError subclasses
        result.failures.append(
            f"{photo.library_path}: the Trash refused {relative} ({exc}); "
            f"{len(moved)} of {len(order)} file(s) had already gone to the Trash"
        )
        if photo.library_path not in moved:
            return

    catalog.photos.update(replace(photo, on_mac=False))
    result.removed += 1
    result.bytes_freed += size
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "at": now().isoformat(timespec="seconds"),
            "photo_id": photo.id,
            "files": moved,
            "bytes": size,
            "content_hash": photo.content_hash,
        }) + "\n")


class _Mismatch(Exception):
    pass


def _confirm_on_drive(
    settings: Settings, catalog: Catalog, root: Path, photo: Photo, relative: str,
    result: OffloadResult, now: Callable[[], dt.datetime],
) -> None:
    """The drive's copy of this one file is the Mac's, byte for byte — or, for
    a sidecar, is made so by archiving it again."""
    mac = settings.resolve(relative)
    drive = settings.resolve_archive(relative)
    assert drive is not None  # check_archive passed
    here = content_hash(mac)
    if relative == photo.library_path and here != photo.content_hash:
        raise _Mismatch("the Mac's copy no longer matches the catalog")
    there = content_hash(drive) if drive.is_file() else None
    if there == here:
        return
    if not relative.endswith(".xmp"):
        raise _Mismatch(f"the archive drive's copy of {Path(relative).name} does not match")

    refreshed = archive.ArchiveResult()
    archive._archive_one(
        archive._Work(relative, mac, None, True), root, {}, refreshed, catalog, now
    )
    if refreshed.failures:
        raise _Mismatch(refreshed.failures[0])
    result.sidecars_refreshed += 1


# ----------------------------------------------------------------------
# Bringing back
# ----------------------------------------------------------------------


def restore_to_mac(
    settings: Settings, catalog: Catalog, photo_ids: Iterable[int],
    *, pin: bool = True,
) -> RestoreResult:
    """Copy photographs back from the archive drive, verified, and (with *pin*)
    mark them *Keep on Mac* so the next free-up does not take them again."""
    try:
        archive.check_archive(settings)
    except archive.ArchiveError as exc:
        raise OffloadError(f"Nothing was copied back. {exc}") from None

    result = RestoreResult()
    for photo_id in photo_ids:
        photo = catalog.photos.get(photo_id)
        if photo is None:
            result.failures.append(f"No photograph with id {photo_id}")
            continue
        if photo.on_mac:
            if pin and photo.keep_on_mac is not True:
                catalog.photos.update(replace(photo, keep_on_mac=True))
            continue
        try:
            copied = _restore_one(settings, photo)
        except (OSError, _Mismatch) as exc:
            result.failures.append(f"{photo.library_path}: {exc}")
            continue
        catalog.photos.update(replace(photo, on_mac=True, keep_on_mac=True if pin else photo.keep_on_mac))
        result.restored += 1
        result.bytes_copied += copied
    return result


def _restore_one(settings: Settings, photo: Photo) -> int:
    copied = 0
    for relative in _relatives(photo):
        drive = settings.resolve_archive(relative)
        assert drive is not None
        if not drive.is_file():
            if relative in (photo.library_path, photo.raw_path):
                raise _Mismatch(f"{Path(relative).name} is not on the archive drive")
            continue  # no sidecar to bring back
        expected = content_hash(drive)
        if relative == photo.library_path and expected != photo.content_hash:
            raise _Mismatch("the archive drive's copy does not match the catalog")
        mac = settings.resolve(relative)
        if mac.exists():
            if content_hash(mac) == expected:
                continue  # dragged back out of the Trash already
            if not relative.endswith(".xmp"):
                raise _Mismatch(f"a different {mac.name} is already on the Mac")
        mac.parent.mkdir(parents=True, exist_ok=True)
        partial = mac.with_name(mac.name + archive.PARTIAL_SUFFIX)
        landed = _copy(drive, partial)
        if landed != expected or content_hash(partial) != expected:
            partial.unlink(missing_ok=True)
            raise _Mismatch(f"{mac.name} did not copy back intact")
        os.replace(partial, mac)
        copied += mac.stat().st_size
    return copied


def _copy(source: Path, target: Path) -> str:
    from . import backup
    import shutil

    digest = backup._copy_and_hash(source, target)
    shutil.copystat(source, target)
    return digest


def photos_in(catalog: Catalog, session_id: int) -> Sequence[int]:
    """Every photograph in one outing, for the outing-wide buttons."""
    return [photo.id for photo in catalog.photos.in_session(session_id) if photo.id is not None]
