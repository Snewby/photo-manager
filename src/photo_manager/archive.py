"""The archive drive: every photograph, at the same path it has on the Mac.

The Mac holds a **working set**; the archive drive holds **everything**. That is
the whole of this module's job, and it changes what "backed up" means. A dated
snapshot (:mod:`photo_manager.backup`) copies whatever the Mac has, so it only
works while the Mac has every photograph; the archive is the place a photograph
can live once the Mac has let go of it.

**A plain mirror.** ``<archive_root>/2026/2026-11/2026-11-07_DSCF0866.jpg`` is the
same file as ``<library_root>/2026/2026-11/2026-11-07_DSCF0866.jpg``. Nothing is
dated, renamed or wrapped, so the drive can be opened in Finder, Lightroom or
digiKam with no help from this app.

**Add and update, never delete.** A photograph lands once and is never touched
again — photographs are not modified here (§4.1), so a different file already
at a photograph's path is a refusal, never an overwrite. Only ``.xmp`` sidecars
are updated, because tags change. Nothing in this module removes anything from
the drive.

**Verified before it counts.** Each file is written as ``.partial``, fsynced,
read back and hashed, and renamed into place only when the bytes match. Only
then does it get a row in the catalog's ``archive_file`` table, which is why
the app can say "on My Passport, verified 7 Nov" for a photograph while the
drive is in a drawer.

**The first run is nearly free.** A photograph already in the newest dated
snapshot on the same drive is *hard-linked* from it rather than copied: same
bytes, no extra space, and still re-read and hashed before it counts.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import backup
from .catalog import ARCHIVED, Catalog
from .config import Settings
from .hashing import content_hash
from .macos import mount_point
from .sidecars import sidecar_path

__all__ = [
    "ArchiveError",
    "ArchiveResult",
    "ArchiveStatus",
    "archive_status",
    "check_archive",
    "run_archive",
]

PARTIAL_SUFFIX = ".partial"

Progress = Callable[[int, int, str], None]


class ArchiveError(ValueError):
    """A refusal raised before anything is written."""


@dataclass
class ArchiveResult:
    """What one archive run did."""

    files: int = 0
    copied: int = 0
    linked: int = 0
    unchanged: int = 0
    bytes_copied: int = 0
    bytes_linked: int = 0
    seconds: float = 0.0
    failures: list[str] = field(default_factory=list)
    #: The dated snapshot of the app's own state taken after the photographs,
    #: or None when there is no backup_root to take it to.
    snapshot: Path | None = None
    #: Why that snapshot was not taken or did not verify, when it wasn't.
    snapshot_problem: str | None = None

    @property
    def ok(self) -> bool:
        return not self.failures and self.snapshot_problem is None


@dataclass(frozen=True)
class ArchiveStatus:
    """Everything the page needs to say about the archive, in one answer."""

    configured: bool
    attached: bool
    message: str
    destination: Path | None = None
    volume: str | None = None
    free_bytes: int | None = None
    #: When the newest file was verified on the drive.
    last_verified: dt.datetime | None = None
    photos: int = 0
    photos_archived: int = 0
    #: Archived photographs whose files are no longer on the Mac — the ones
    #: with a single copy, which the page has to be honest about.
    only_on_drive: int = 0
    #: Sidecars on the Mac whose tags have changed since they were archived.
    #: None when that could not be worked out.
    sidecars_changed: int | None = None

    @property
    def photos_not_archived(self) -> int:
        return self.photos - self.photos_archived


# ----------------------------------------------------------------------
# Where
# ----------------------------------------------------------------------


def check_archive(settings: Settings) -> Path:
    """The archive root, or :class:`ArchiveError` saying why it can't be used.

    The same rules as a backup: another physical drive, not inside the
    library, and an unplugged drive is called that rather than "same disk".
    """
    if settings.archive_root is None:
        raise ArchiveError(
            "No archive_root is set in config.toml, so there is no archive drive."
        )
    root = Path(settings.archive_root).expanduser()
    try:
        backup.check_destination(Path(settings.library_root), root)
    except backup.BackupError as exc:
        raise ArchiveError(str(exc).replace("backup drive", "archive drive")) from None
    return root


def archive_status(settings: Settings, catalog: Catalog | None) -> ArchiveStatus:
    """How the archive stands. Reads the catalog; asks the drive only for its
    name and free space, so most of this is true with the drive unplugged."""
    if settings.archive_root is None:
        return ArchiveStatus(
            configured=False, attached=False,
            message="No archive drive is set up. Add archive_root to config.toml.",
        )
    root = Path(settings.archive_root).expanduser()
    try:
        check_archive(settings)
        attached, message = True, ""
    except ArchiveError as exc:
        attached, message = False, str(exc)

    volume = free = None
    if attached:
        volume = mount_point(root).name
        free = shutil.disk_usage(_existing(root)).free

    photos = archived = only_on_drive = 0
    changed: int | None = None
    if catalog is not None:
        connection = catalog.connection
        photos = connection.execute("SELECT count(*) FROM photo").fetchone()[0]
        archived = connection.execute(
            f"SELECT count(*) FROM photo p WHERE {ARCHIVED}"
        ).fetchone()[0]
        only_on_drive = connection.execute(
            "SELECT count(*) FROM photo WHERE on_mac = 0"
        ).fetchone()[0]
        changed = sum(1 for item in _sidecar_work(settings, catalog) if item.needed)
    return ArchiveStatus(
        configured=True, attached=attached, message=message, destination=root,
        volume=volume, free_bytes=free,
        last_verified=None if catalog is None else catalog.archive.last_verified(),
        photos=photos, photos_archived=archived, only_on_drive=only_on_drive,
        sidecars_changed=changed,
    )


# ----------------------------------------------------------------------
# What needs doing
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Work:
    #: Relative to both roots, forward-slashed.
    path: str
    source: Path
    #: What the catalog says the bytes must be: a photograph's content hash,
    #: or None for a RAF or sidecar, whose bytes the catalog does not hold.
    expected: str | None
    #: Photographs are never overwritten on the drive; sidecars are.
    replaceable: bool
    needed: bool = True


def _work(settings: Settings, catalog: Catalog) -> list[_Work]:
    """Every file on the Mac the archive does not already hold as it is now."""
    known = catalog.archive.all()
    work: list[_Work] = []
    rows = catalog.connection.execute(
        "SELECT library_path, raw_path, content_hash FROM photo "
        "WHERE on_mac = 1 ORDER BY captured_at, id"
    ).fetchall()
    for row in rows:
        library_path, raw_path, digest = row["library_path"], row["raw_path"], row["content_hash"]
        record = known.get(library_path)
        if record is None or record.sha256 != digest:
            work.append(_Work(library_path, settings.resolve(library_path), digest, False))
        if raw_path is not None and raw_path not in known:
            work.append(_Work(raw_path, settings.resolve(raw_path), None, False))
    work.extend(item for item in _sidecar_work(settings, catalog, known) if item.needed)
    return work


def _sidecar_work(
    settings: Settings, catalog: Catalog, known: dict | None = None
) -> Iterable[_Work]:
    """Each sidecar beside a photograph on the Mac, and whether the drive's
    copy is out of date. Hashed only when its mtime says it may have changed."""
    known = catalog.archive.all() if known is None else known
    rows = catalog.connection.execute(
        "SELECT library_path, raw_path FROM photo WHERE on_mac = 1"
    ).fetchall()
    for row in rows:
        for photo_path in (row["library_path"], row["raw_path"]):
            if photo_path is None:
                continue
            relative = sidecar_path(photo_path).as_posix()
            source = settings.resolve(relative)
            try:
                modified = dt.datetime.fromtimestamp(source.stat().st_mtime)
            except OSError:
                continue  # no sidecar, so nothing to archive
            record = known.get(relative)
            needed = (
                record is None
                or (modified > record.verified_at and content_hash(source) != record.sha256)
            )
            yield _Work(relative, source, None, True, needed)


# ----------------------------------------------------------------------
# Doing it
# ----------------------------------------------------------------------


def run_archive(
    settings: Settings,
    catalog: Catalog,
    *,
    progress: Progress | None = None,
    now: Callable[[], dt.datetime] = dt.datetime.now,
    snapshot: bool = True,
    private_dir: Path | None = None,
) -> ArchiveResult:
    """Copy everything the drive is missing, verify it, and record it.

    Refusals — no archive configured, drive not attached, not enough room —
    raise :class:`ArchiveError` before anything is written. A file that fails
    does not stop the others: it is reported, its ``.partial`` is left on the
    drive where it can be seen, and it gets no row, so it is still owed.

    Each verified file is committed to the catalog as it lands, so stopping
    half way loses nothing that was already done.

    With *snapshot*, the app's own state — the catalog, which now records
    what the drive holds, plus cull decisions and config — is snapshotted to
    ``backup_root`` afterwards, without the photographs.
    """
    started = time.perf_counter()
    root = check_archive(settings)
    work = _work(settings, catalog)
    seeds = _seeds(settings, root)

    needed = 0
    for item in work:
        try:
            if item.expected is None or item.expected not in seeds:
                needed += item.source.stat().st_size
        except OSError:
            pass  # reported as a failure when it is reached
    free = shutil.disk_usage(_existing(root)).free
    if free < needed * 1.05:
        raise ArchiveError(
            f"Not enough room: about {needed / 1e9:.2f} GB to write and "
            f"{free / 1e9:.2f} GB free on the archive drive."
        )

    result = ArchiveResult()
    for index, item in enumerate(work, start=1):
        if progress is not None:
            progress(index, len(work), item.path)
        _archive_one(item, root, seeds, result, catalog, now)
    result.files = result.copied + result.linked + result.unchanged

    if snapshot:
        _snapshot_state(settings, private_dir, result)
    result.seconds = time.perf_counter() - started
    return result


def _archive_one(
    item: _Work, root: Path, seeds: dict[str, Path], result: ArchiveResult,
    catalog: Catalog, now: Callable[[], dt.datetime],
) -> None:
    target = root.joinpath(*item.path.split("/"))
    partial = target.with_name(target.name + PARTIAL_SUFFIX)
    try:
        expected = content_hash(item.source)
        if item.expected is not None and expected != item.expected:
            result.failures.append(
                f"{item.path}: the Mac's copy no longer matches the catalog "
                f"({expected[:12]}, catalog {item.expected[:12]}), so it was not archived"
            )
            return

        if target.exists():
            existing = content_hash(target)
            if existing == expected:
                catalog.archive.record(item.path, existing, target.stat().st_size, now())
                result.unchanged += 1
                return
            if not item.replaceable:
                result.failures.append(
                    f"{item.path}: a different file is already at that path on the "
                    f"archive drive, and a photograph there is never overwritten"
                )
                return

        target.parent.mkdir(parents=True, exist_ok=True)
        if partial.exists():
            partial.unlink()  # left by an earlier failed run; never a finished file

        linked = False
        seed = seeds.get(expected)
        if seed is not None:
            try:
                os.link(seed, partial)
                linked = True
            except OSError:
                linked = False  # another volume, or no hard links: copy instead
        if not linked:
            copied = backup._copy_and_hash(item.source, partial)
            shutil.copystat(item.source, partial)
            if copied != expected:
                result.failures.append(f"{item.path}: changed while it was being copied")
                return

        landed = content_hash(partial)
        if landed != expected:
            result.failures.append(
                f"{item.path}: expected {expected[:12]} but read back {landed[:12]}; "
                f"left as {partial.name}"
            )
            return
        os.replace(partial, target)
        size = target.stat().st_size
        catalog.archive.record(item.path, landed, size, now())
    except OSError as exc:
        result.failures.append(f"{item.path}: {exc}")
        return

    if linked:
        result.linked += 1
        result.bytes_linked += size
    else:
        result.copied += 1
        result.bytes_copied += size


def _seeds(settings: Settings, root: Path) -> dict[str, Path]:
    """Photographs already on this drive in the newest dated snapshot that
    held the library, by content hash — so the first archive links them in
    rather than copying them again."""
    if settings.backup_root is None:
        return {}
    destination = Path(settings.backup_root).expanduser()
    if backup.device(destination) != backup.device(root):
        return {}
    container = backup._container(destination, Path(settings.library_root).resolve())
    for _, folder, manifest in reversed(backup._snapshots(container)):
        files = manifest.get("files", [])
        if files:
            return {
                entry["sha256"]: folder / "library" / Path(*entry["path"].split("/"))
                for entry in files
            }
    return {}


def _snapshot_state(settings: Settings, private_dir: Path | None, result: ArchiveResult) -> None:
    if settings.backup_root is None:
        return
    try:
        taken = backup.take_snapshot(
            settings, label="app state, beside the archive", include_library=False,
            private_dir=private_dir,
        )
    except backup.BackupError as exc:
        result.snapshot_problem = f"The app's own state was not snapshotted: {exc}"
        return
    result.snapshot = taken.snapshot
    if not taken.ok:
        result.snapshot_problem = (
            f"The app's state snapshot did not verify and was left as {taken.snapshot.name}. "
            f"First: {taken.failures[0]}"
        )


def _existing(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return path
