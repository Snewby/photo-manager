"""Verified snapshots of the library on another drive — and of what it cannot rebuild.

WU-1.5's gate asked for "a verified backup on separate physical media", and
both halves still matter: a copy nobody checked is a belief rather than a
backup, and a copy on the same disk does not survive the thing it exists to
survive. This module is what ``tools/backup_library.py`` and the app's Backup
button both call, so there is one definition of "backed up".

**A snapshot is a dated folder that looks like a full copy**::

    <destination>/<library>/<YYYY-MM-DD>/
        library/                the photographs and their .xmp sidecars
        state/  private/  config.toml
        backup-manifest.json    SHA-256 and size of every file in it
        READ-ME-FIRST.txt

Restoring the library is copying ``library/`` back. ``state/`` holds what the
library cannot give back — the cull decisions, the undo logs, the catalog
itself (rebuildable, but rebuilt is not the same as kept) — and ``private/``
the project documents, which have no other copy. The thumbnail cache is left
out: it is regenerated from the photographs.

**Snapshots are incremental, and do not look it.** A file whose content is
identical to the same path in the previous snapshot is *hard-linked* to it
rather than copied, so each snapshot is complete on its own while a photograph
that never changes is stored once. Deleting any snapshot therefore never loses
a file another snapshot still has. Photographs here are never modified (§4.1),
so after the first snapshot each one costs only what was imported or tagged
since. A drive that cannot hard-link gets a copy, and the manifest is the same.

**Verification is end to end, links included.** Every file is hashed from the
library, then re-read from the drive and hashed again — for a link that is a
read of bytes written by an earlier snapshot, which is also how decay in an old
copy would be caught. The live catalog is copied with SQLite's own backup API,
because copying a database file that is in use can produce one that is not
consistent, and the copy is integrity-checked.

**Nothing here deletes or overwrites.** A snapshot is built as
``<name>.partial`` and renamed only once every file is verified, so a failure
leaves an obviously unfinished folder rather than one that looks complete; a
second snapshot on the same day gets the time in its name instead of replacing
the first. Removing old snapshots is a decision for a human with a full drive.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .hashing import DEFAULT_CHUNK_SIZE, content_hash
from .macos import mount_point

__all__ = [
    "MANIFEST_NAME",
    "READ_ME_NAME",
    "BackupError",
    "BackupResult",
    "BackupStatus",
    "check_destination",
    "device",
    "latest_snapshot",
    "status",
    "take_snapshot",
    "verify_snapshot",
]

MANIFEST_NAME = "backup-manifest.json"
READ_ME_NAME = "READ-ME-FIRST.txt"
PARTIAL_SUFFIX = ".partial"

#: Beside the catalog: what the last snapshot held, so the app can say what is
#: not yet backed up while the drive is in a drawer.
_RECORD_DIR = "backup"

Progress = Callable[[int, int, str], None]

READ_ME = """\
Photo library backup - {label}

Source   : {source}
Taken    : {taken}
Files    : {count} ({library} in library/)
Bytes    : {size:,}
Verified : every file was re-read from this drive and its SHA-256 matched the
           source it was copied from.

library/ is a plain copy of the photo library with its directory structure
intact. To restore, copy the contents of library/ back over the library root.
Nothing here is compressed, encrypted or in a proprietary format - the files
are the photographs. state/, private/ and config.toml are the app's own files
that the library cannot rebuild.

Files that had not changed since the previous snapshot are hard links to it:
each snapshot is complete on its own, and deleting one never removes a file
another snapshot still holds.

{manifest} lists every file with its SHA-256, so this backup can be re-verified
at any time:

    python tools/backup_library.py --verify-only "<this folder>"
"""


class BackupError(ValueError):
    """A backup that was refused before anything was written, with the reason."""


@dataclass
class BackupResult:
    snapshot: Path
    files: int = 0
    copied: int = 0
    linked: int = 0
    bytes_copied: int = 0
    bytes_linked: int = 0
    incremental_from: str | None = None
    seconds: float = 0.0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class BackupStatus:
    """What the app says about backups, with or without the drive attached."""

    configured: bool
    attached: bool
    #: A sentence for a human — always populated.
    message: str
    destination: str | None = None
    volume: str | None = None
    free_bytes: int | None = None
    last_taken: str | None = None
    last_snapshot: str | None = None
    #: Catalogued photographs whose bytes are in no snapshot the app knows of.
    photos_not_backed_up: int | None = None
    #: Sidecars written since the last snapshot — tags not yet backed up.
    sidecars_changed: int | None = None


# ----------------------------------------------------------------------
# Where
# ----------------------------------------------------------------------


def device(path: Path) -> int | None:
    """The device number of the volume *path* is on, or would be created on.

    By ``st_dev`` rather than ``Path.drive``: the drive letter answered this on
    Windows, but it is ``''`` for every path on macOS, so comparing drives
    refused every destination there — the external drive included. ``st_dev``
    is the volume on both (Python fills it from the volume serial on Windows).
    The destination usually does not exist yet, so its nearest existing
    ancestor is what gets asked; None means nothing on that path exists at all.
    """
    for candidate in (path, *path.parents):
        try:
            return candidate.stat().st_dev
        except OSError:
            # Absent, or a Windows drive letter with nothing in the reader.
            continue
    return None


def check_destination(library: Path, destination: Path) -> None:
    """Refuse a destination that would not be a backup. Creates nothing.

    An unplugged drive and the library's own disk are the same refusal on
    macOS, where ``/Volumes/<drive>`` simply does not exist without the drive
    and its nearest ancestor is the startup disk — so that case is named as
    the drive not being attached, which is what it almost always is.
    """
    library = library.resolve()
    if destination.exists() and not destination.is_dir():
        raise BackupError(f"{destination} is not a folder.")
    if destination == library or library in destination.parents:
        raise BackupError(
            f"{destination} is inside the library. A copy of something, filed "
            f"inside that same something, is not a backup of it."
        )
    destination_device = device(destination)
    if destination_device is None:
        raise BackupError(f"No part of {destination} exists. Is the drive attached?")
    if destination_device == device(library):
        if not destination.exists():
            raise BackupError(
                f"{destination} is not there, so the backup drive is not attached."
            )
        raise BackupError(
            f"{destination} is on the same disk as the library. A backup has to "
            f"survive that disk failing, so it needs another drive."
        )


def _container(destination: Path, library: Path) -> Path:
    return destination / library.name


def _snapshots(container: Path) -> list[tuple[str, Path, dict]]:
    """Finished snapshots under *container*, oldest first, with their manifests."""
    found: list[tuple[str, Path, dict]] = []
    try:
        candidates = [p for p in container.iterdir() if p.is_dir()]
    except OSError:
        return []
    for folder in candidates:
        if folder.name.endswith(PARTIAL_SUFFIX):
            continue
        manifest = _read_manifest(folder)
        if manifest is not None:
            found.append((str(manifest.get("taken", "")), folder, manifest))
    return sorted(found, key=lambda item: (item[0], item[1].name))


def latest_snapshot(destination: Path, library: Path) -> Path | None:
    snapshots = _snapshots(_container(destination, library))
    return snapshots[-1][1] if snapshots else None


def _read_manifest(snapshot: Path) -> dict | None:
    try:
        return json.loads((snapshot / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _entries(manifest: dict) -> dict[str, dict]:
    """Every file a manifest lists, keyed by its path inside the snapshot."""
    entries = {f"library/{e['path']}": e for e in manifest.get("files", [])}
    entries.update({e["path"]: e for e in manifest.get("app_files", [])})
    return entries


def _snapshot_name(container: Path, now: dt.datetime) -> str:
    """The date, or the date and time when the date is taken."""
    for name in (now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d-%H%M%S")):
        if not (container / name).exists() and not (container / f"{name}{PARTIAL_SUFFIX}").exists():
            return name
    raise BackupError(f"A snapshot named {name} already exists. Try again in a second.")


# ----------------------------------------------------------------------
# What
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Item:
    source: Path
    #: Where it lands inside the snapshot, with forward slashes.
    path: str
    #: The live catalog, which is copied through SQLite rather than as bytes.
    database: bool = False


def _items(
    settings: Settings, library: Path, include_app_state: bool, private: Path
) -> list[_Item]:
    items = [
        _Item(path, f"library/{path.relative_to(library).as_posix()}")
        for path in _files_under(library)
    ]
    if not include_app_state:
        return items

    state = Path(settings.state_dir)
    live = Path(settings.database_path).resolve() if settings.database_path else None
    thumbnails = Path(settings.thumbnail_cache_dir).resolve() if settings.thumbnail_cache_dir else None
    for path in _files_under(state):
        if thumbnails is not None and thumbnails in path.resolve().parents:
            continue
        if path.name.endswith(("-wal", "-shm")):
            continue  # part of a live database, which the backup API covers
        items.append(_Item(
            path, f"state/{path.relative_to(state).as_posix()}",
            database=path.resolve() == live,
        ))

    for path in _files_under(private):
        items.append(_Item(path, f"private/{path.relative_to(private).as_posix()}"))

    config = settings.source_path
    if config is not None and Path(config).is_file():
        items.append(_Item(Path(config), "config.toml"))
    return items


#: The project's own documents: a local-only git repository beside the code.
PRIVATE_DIR = Path(__file__).resolve().parents[2] / "private"


def _files_under(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        (Path(folder) / name for folder, _, names in os.walk(root) for name in names),
        key=lambda p: p.as_posix().casefold(),
    )


# ----------------------------------------------------------------------
# Taking one
# ----------------------------------------------------------------------


def take_snapshot(
    settings: Settings,
    destination: Path | None = None,
    *,
    label: str = "",
    incremental: bool = True,
    include_app_state: bool = True,
    private_dir: Path | None = None,
    progress: Progress | None = None,
    now: dt.datetime | None = None,
) -> BackupResult:
    """Write a verified snapshot to *destination* (``backup_root`` by default).

    Refusals — no destination set, the drive not attached, the library's own
    disk, not enough room — raise :class:`BackupError` before anything is
    written. A file that fails verification does not stop the others; it is
    reported, and the snapshot is left as ``.partial`` so it is never mistaken
    for a good one.
    """
    started = time.perf_counter()
    library = settings.verify_library_root()
    destination = destination if destination is not None else settings.backup_root
    if destination is None:
        raise BackupError("No backup_root is set in config.toml, so there is nowhere to back up to.")
    destination = Path(destination).expanduser()
    check_destination(library, destination)

    container = _container(destination, library)
    previous_snapshots = _snapshots(container) if incremental else []
    previous = previous_snapshots[-1] if previous_snapshots else None
    previous_entries = _entries(previous[2]) if previous else {}

    items = _items(settings, library, include_app_state, private_dir or PRIVATE_DIR)
    needed = sum(
        item.source.stat().st_size for item in items
        if not _probably_linkable(item, previous_entries)
    )
    free = shutil.disk_usage(_existing(destination)).free
    if free < needed * 1.05:
        raise BackupError(
            f"Not enough room: about {needed / 1e9:.2f} GB to write and "
            f"{free / 1e9:.2f} GB free on the backup drive."
        )

    now = now or dt.datetime.now()
    name = _snapshot_name(container, now)
    partial = container / f"{name}{PARTIAL_SUFFIX}"
    partial.mkdir(parents=True)
    result = BackupResult(
        snapshot=container / name,
        incremental_from=previous[1].name if previous else None,
    )

    library_entries: list[dict] = []
    app_entries: list[dict] = []
    for index, item in enumerate(items, start=1):
        if progress is not None:
            progress(index, len(items), item.path)
        entry = _store(item, partial, previous, previous_entries, result)
        if entry is None:
            continue
        if item.path.startswith("library/"):
            library_entries.append({**entry, "path": item.path.removeprefix("library/")})
        else:
            app_entries.append(entry)

    result.files = len(library_entries) + len(app_entries)
    result.seconds = time.perf_counter() - started
    if result.failures:
        result.snapshot = partial
        return result

    taken = now.isoformat(timespec="seconds")
    manifest = {
        "source": str(library),
        "taken": taken,
        "label": label,
        "incremental_from": result.incremental_from,
        "files": library_entries,
        "app_files": app_entries,
    }
    (partial / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (partial / READ_ME_NAME).write_text(
        READ_ME.format(
            label=label or "routine", source=library, taken=taken,
            count=result.files, library=len(library_entries),
            size=sum(int(e["bytes"]) for e in library_entries + app_entries),
            manifest=MANIFEST_NAME,
        ),
        encoding="utf-8",
    )
    partial.rename(result.snapshot)
    _record(settings, result.snapshot, manifest)
    result.seconds = time.perf_counter() - started
    return result


def _probably_linkable(item: _Item, previous: dict[str, dict]) -> bool:
    entry = previous.get(item.path)
    return entry is not None and not item.database and entry["bytes"] == item.source.stat().st_size


def _store(
    item: _Item, partial: Path, previous: tuple[str, Path, dict] | None,
    previous_entries: dict[str, dict], result: BackupResult,
) -> dict | None:
    target = partial / item.path
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if item.database:
            _copy_database(item.source, target)
            expected = content_hash(target)
            linked = False
        else:
            expected = content_hash(item.source)
            linked = _link_if_unchanged(item, target, expected, previous, previous_entries)
            if not linked:
                copied = _copy_and_hash(item.source, target)
                shutil.copystat(item.source, target)
                if copied != expected:
                    result.failures.append(f"{item.path}: changed while it was being copied")
                    return None
        landed = content_hash(target)
    except (OSError, sqlite3.Error) as exc:
        result.failures.append(f"{item.path}: {exc}")
        return None

    if landed != expected:
        result.failures.append(
            f"{item.path}: expected {expected[:12]} but read back {landed[:12]}"
        )
        return None
    size = target.stat().st_size
    if linked:
        result.linked += 1
        result.bytes_linked += size
    else:
        result.copied += 1
        result.bytes_copied += size
    return {"path": item.path, "bytes": size, "sha256": landed}


def _link_if_unchanged(
    item: _Item, target: Path, expected: str,
    previous: tuple[str, Path, dict] | None, previous_entries: dict[str, dict],
) -> bool:
    entry = previous_entries.get(item.path)
    if previous is None or entry is None or entry["sha256"] != expected:
        return False
    try:
        os.link(previous[1] / item.path, target)
    except OSError:
        # Gone from the old snapshot, or a filesystem with no hard links
        # (exFAT): a copy is the same backup at the cost of the space.
        return False
    return True


def _copy_and_hash(source: Path, target: Path) -> str:
    """Copy, hashing the bytes on the way through."""
    digest = hashlib.sha256()
    with source.open("rb") as reader, target.open("wb") as writer:
        while chunk := reader.read(DEFAULT_CHUNK_SIZE):
            digest.update(chunk)
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    return digest.hexdigest()


def _copy_database(source: Path, target: Path) -> None:
    """A consistent copy of a database that may be in use, then checked.

    SQLite's backup API reads it page by page under its own locking, which a
    byte copy of a WAL-mode file cannot promise. The copy is switched to a
    rollback journal so that opening it — to check it, or after a restore —
    leaves no ``-wal`` file beside it on the backup drive.
    """
    reader = sqlite3.connect(source)
    try:
        writer = sqlite3.connect(target)
        try:
            reader.backup(writer)
            writer.execute("PRAGMA journal_mode=DELETE")
        finally:
            writer.close()
    finally:
        reader.close()
    check = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    try:
        verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if verdict != "ok":
        raise sqlite3.DatabaseError(f"the copied catalog failed its integrity check: {verdict}")


def _existing(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return path


def _record(settings: Settings, snapshot: Path, manifest: dict) -> None:
    """Keep what the last snapshot held beside the catalog."""
    folder = Path(settings.state_dir) / _RECORD_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "latest-manifest.json").write_text(
        json.dumps({**manifest, "snapshot": str(snapshot)}), encoding="utf-8"
    )
    with (folder / "history.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps({
            "taken": manifest["taken"], "snapshot": str(snapshot),
            "files": len(manifest["files"]) + len(manifest["app_files"]),
            "incremental_from": manifest["incremental_from"],
        }) + "\n")


# ----------------------------------------------------------------------
# Checking one
# ----------------------------------------------------------------------


def verify_snapshot(snapshot: Path, progress: Progress | None = None) -> tuple[int, list[str]]:
    """Re-read every file a snapshot's manifest lists. Returns (checked, problems)."""
    manifest = _read_manifest(snapshot)
    if manifest is None:
        return 0, [f"no readable {MANIFEST_NAME} in {snapshot}"]
    entries = _entries(manifest)
    problems: list[str] = []
    for index, (path, entry) in enumerate(entries.items(), start=1):
        if progress is not None:
            progress(index, len(entries), path)
        file = snapshot / path
        if not file.is_file():
            problems.append(f"{path}: missing")
        elif content_hash(file) != entry["sha256"]:
            problems.append(f"{path}: does not match the manifest")
    return len(entries), problems


# ----------------------------------------------------------------------
# Saying where things stand
# ----------------------------------------------------------------------


def status(settings: Settings, catalog_hashes: Iterable[str] = ()) -> BackupStatus:
    """Whether a backup can be taken now, and what the last one does not hold.

    With the drive attached the newest snapshot on it is the reference; without
    it, the record kept beside the catalog when the last one was taken. A
    photograph counts as backed up when its *bytes* are in that snapshot —
    the same content hash the catalog dedups by — and a sidecar written since
    is tags that are not.
    """
    destination = settings.backup_root
    if destination is None:
        return BackupStatus(
            configured=False, attached=False,
            message="Backups are not set up: add backup_root to config.toml.",
        )
    library = Path(settings.library_root).resolve()
    destination = Path(destination).expanduser()
    try:
        check_destination(library, destination)
        attached, reason = True, ""
    except BackupError as exc:
        attached, reason = False, str(exc)

    manifest = _reference_manifest(settings, destination, library, attached)

    volume = mount_point(destination).name if attached else None
    free = shutil.disk_usage(_existing(destination)).free if attached else None
    if manifest is None:
        return BackupStatus(
            configured=True, attached=attached, destination=str(destination),
            volume=volume, free_bytes=free,
            message="No backup has been taken yet." if attached else reason,
        )

    backed_up = {entry["sha256"] for entry in manifest.get("files", [])}
    missing = sum(1 for digest in set(catalog_hashes) if digest not in backed_up)
    sidecars = _sidecars_since(library, manifest)
    return BackupStatus(
        configured=True, attached=attached, destination=str(destination),
        volume=volume, free_bytes=free,
        last_taken=manifest.get("taken"),
        last_snapshot=Path(manifest["snapshot"]).name if manifest.get("snapshot") else None,
        photos_not_backed_up=missing, sidecars_changed=sidecars,
        message=reason if not attached else "",
    )


def backed_up_hashes(settings: Settings) -> frozenset[str] | None:
    """The content hashes the last backup holds, or None when there is none.

    The same reference as :func:`status` — the newest snapshot on the drive,
    else the record kept beside the catalog — so a photograph a page marks as
    backed up is one the strip counts as backed up.
    """
    if settings.backup_root is None:
        return None
    library = Path(settings.library_root).resolve()
    destination = Path(settings.backup_root).expanduser()
    try:
        check_destination(library, destination)
        attached = True
    except BackupError:
        attached = False
    manifest = _reference_manifest(settings, destination, library, attached)
    if manifest is None:
        return None
    return frozenset(entry["sha256"] for entry in manifest.get("files", []))


def _reference_manifest(
    settings: Settings, destination: Path, library: Path, attached: bool
) -> dict | None:
    """The manifest "backed up" is measured against: the newest snapshot on
    the drive when it is attached, else the copy recorded when it was taken."""
    if attached:
        found = _snapshots(_container(destination, library))
        if found:
            return {**found[-1][2], "snapshot": str(found[-1][1])}
    try:
        recorded = Path(settings.state_dir) / _RECORD_DIR / "latest-manifest.json"
        return json.loads(recorded.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _sidecars_since(library: Path, manifest: dict) -> int:
    """Sidecars whose content the last snapshot does not have.

    By hash where the snapshot lists the path, so a sidecar rewritten with the
    same tags does not count; one it does not list at all is new.
    """
    known = {entry["path"]: entry for entry in manifest.get("files", [])}
    taken = manifest.get("taken")
    try:
        cutoff = dt.datetime.fromisoformat(taken).timestamp() if taken else 0.0
    except ValueError:
        cutoff = 0.0
    changed = 0
    for path in library.rglob("*.xmp"):
        relative = path.relative_to(library).as_posix()
        entry = known.get(relative)
        if entry is None:
            changed += 1
        elif path.stat().st_mtime > cutoff and content_hash(path) != entry["sha256"]:
            changed += 1
    return changed
