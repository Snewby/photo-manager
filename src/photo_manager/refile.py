"""Re-filing the library into D-4's month folders.

D-4 was ``YYYY/YYYY-MM-DD/`` until 2026-09-23, when the owner asked for months:
``YYYY/YYYY-MM/``, with a day's photographs straight inside its month. New
imports follow :func:`~photo_manager.paths.target_directory` and need nothing
from here. This module moves what was filed **before** the change, and it
follows the migration's habits because it is the same kind of act — a mass move
of an irreplaceable library:

- **A plan first, and it writes nothing.** :func:`plan_refile` reads the catalog
  and the disk and says where every file would go.
- **Every file accounted for.** A file under the library root that no
  photograph owns is reported, and execution refuses while there is one: a
  re-file that leaves strays behind in the old folders has not re-filed the
  library.
- **A photograph moves as a unit** — its JPEG, its RAF and the sidecar beside
  each — inside one catalog transaction, each file hash-checked on arrival, and
  put back if anything about it fails.
- **Every move is logged as it happens** in the migration's reversal format, and
  :func:`reverse_refile` puts files *and* catalog paths back.

Filenames are kept. A clash inside a month is resolved the way every clash in
this app is (:func:`~photo_manager.paths.free_names`), the JPEG and RAF as one.

**What a day folder knew that a month folder does not.** The rebuild uses a
dated folder as the record of a capture date a person chose, for a photograph
whose EXIF has none (see ``rebuild._capture_time``). A month folder can only
say the month. The plan finds every photograph for which the day folder is the
*only* record — its date inferred, and its file's mtime on a different day — and
lists it under :attr:`RefilePlan.date_only_in_folder`, so that the loss is
decided rather than discovered by a rebuild months later.

the owner's decision for those (2026-09-23) was ``fix_timestamps``: set the file's
modified time to the catalog's capture time before it moves. The bytes are
untouched, the rebuild's mtime fallback then recovers the exact moment rather
than only the day, and the old mtime is written to the reversal log.
"""

from __future__ import annotations

import datetime as dt
import os
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from .catalog import Catalog
from .hashing import content_hash
from .models import Photo
from .paths import free_names, target_directory
from .sidecars import sidecar_path

# The reversal log and the move primitives are the migration's, on purpose: one
# format means one reader, and a second implementation of "move, verify, undo"
# would be a second chance to get it wrong.
from .migration import Move, _move, _move_back, _ReversalLog, read_reversal_log

__all__ = [
    "RefileError",
    "PlannedFile",
    "PlannedRefile",
    "RefilePlan",
    "RefileOutcome",
    "ReverseOutcome",
    "plan_refile",
    "execute_refile",
    "reverse_refile",
]


class RefileError(Exception):
    """The re-file could not proceed, and stopped rather than guessing."""


@dataclass(frozen=True, slots=True)
class PlannedFile:
    """One file: where it is, where it is going, and what its bytes hash to."""

    source: str
    target: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class PlannedRefile:
    """One photograph and every file that moves with it."""

    photo: Photo
    files: tuple[PlannedFile, ...]
    library_path: str
    raw_path: str | None


@dataclass(frozen=True, slots=True)
class RefilePlan:
    library_root: Path
    generated_at: dt.datetime
    photos: tuple[PlannedRefile, ...]
    #: Photographs already where the rule puts them.
    already_filed: int
    #: Files under the root that no catalogued photograph owns.
    strays: tuple[str, ...]
    #: Photographs whose capture day only their current folder records.
    date_only_in_folder: tuple[Photo, ...]

    @property
    def files(self) -> tuple[PlannedFile, ...]:
        return tuple(f for p in self.photos for f in p.files)

    def month_counts(self) -> dict[str, int]:
        """Photographs per target folder, oldest first."""
        counts = Counter(PurePosixPath(p.library_path).parent.as_posix() for p in self.photos)
        return dict(sorted(counts.items()))


def plan_refile(catalog: Catalog, *, now: dt.datetime | None = None) -> RefilePlan:
    """Where every catalogued file would go under the current D-4. Writes nothing."""
    root = catalog.settings.library_root
    photos: list[PlannedRefile] = []
    owned: set[str] = set()
    claimed: set[str] = set()
    already = 0
    at_risk: list[Photo] = []

    for photo in catalog.photos.all(newest_first=False):
        current = [photo.library_path] + ([photo.raw_path] if photo.raw_path else [])
        for relative in current:
            owned.add(relative.casefold())
            owned.add(_sidecar(relative).casefold())

        directory = target_directory(photo.captured_at)
        if PurePosixPath(photo.library_path).parent.as_posix() == directory:
            already += 1
            for relative in current:
                claimed.add(relative.casefold())
            continue

        if _date_only_in_folder(photo, root):
            at_risk.append(photo)

        names = [PurePosixPath(relative).name for relative in current]
        _, targets = free_names(directory, names, claimed, root)
        planned: list[PlannedFile] = []
        new_paths: list[str] = []
        for relative, name in zip(current, targets):
            target = f"{directory}/{name}"
            claimed.add(target.casefold())
            new_paths.append(target)
            expected = photo.content_hash if relative == photo.library_path else None
            planned.append(_planned(root, relative, target, expected))
            if (root / _sidecar(relative)).is_file():
                planned.append(_planned(root, _sidecar(relative), _sidecar(target), None))

        photos.append(PlannedRefile(
            photo=photo,
            files=tuple(planned),
            library_path=new_paths[0],
            raw_path=new_paths[1] if len(new_paths) > 1 else None,
        ))

    strays = tuple(sorted(
        relative for relative in _files_under(root) if relative.casefold() not in owned
    ))
    return RefilePlan(
        library_root=root,
        generated_at=now or dt.datetime.now(),
        photos=tuple(photos),
        already_filed=already,
        strays=strays,
        date_only_in_folder=tuple(at_risk),
    )


def _planned(root: Path, source: str, target: str, expected: str | None) -> PlannedFile:
    """The JPEG's hash is the catalog's; anything else is hashed now, so the
    preflight can prove nothing changed between the plan and the move."""
    path = root / source
    digest = expected if expected is not None else (content_hash(path) if path.is_file() else "")
    return PlannedFile(source, target, digest)


def _sidecar(relative: str) -> str:
    return sidecar_path(PurePosixPath(relative)).as_posix()


def _date_only_in_folder(photo: Photo, root: Path) -> bool:
    """True when a rebuild could only recover this capture day from the folder."""
    if not photo.captured_at_inferred:
        return False
    try:
        mtime = dt.datetime.fromtimestamp((root / photo.library_path).stat().st_mtime)
    except OSError:
        return False
    return mtime.date() != photo.captured_at.date()


def _files_under(root: Path) -> list[str]:
    found: list[str] = []
    for current, _, files in os.walk(root):
        for name in files:
            found.append((Path(current) / name).relative_to(root).as_posix())
    return found


# ----------------------------------------------------------------------
# Doing it
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefileOutcome:
    reversal_path: Path
    moves: tuple[Move, ...] = ()
    photos_refiled: int = 0
    failure: str | None = None

    @property
    def complete(self) -> bool:
        return self.failure is None


def execute_refile(
    plan: RefilePlan,
    catalog: Catalog,
    *,
    reversal_path: Path,
    fix_timestamps: bool = False,
    progress: Callable[[int, int, PlannedRefile], None] | None = None,
) -> RefileOutcome:
    """Move every planned photograph and point its catalog row at the new paths.

    Refuses to start while anything is unaccounted for, and checks the whole plan
    against the disk before the first move. Stops at the first failure, with the
    photograph that failed put back exactly as it was.

    With *fix_timestamps*, each photograph in ``plan.date_only_in_folder`` first
    has its mtime set to its catalog capture time, so that the day its folder
    recorded survives the folder.
    """
    if plan.strays:
        raise RefileError(
            f"{len(plan.strays)} file(s) under the library belong to no catalogued "
            f"photograph, e.g. {plan.strays[0]}. Account for them first; a re-file "
            f"that leaves files behind in the old folders has not re-filed the library."
        )
    _preflight(plan)

    root = plan.library_root
    moves: list[Move] = []
    done = 0
    failure: str | None = None
    with _ReversalLog(reversal_path, root, plan.generated_at) as log:
        if fix_timestamps:
            for photo in plan.date_only_in_folder:
                _set_mtime(root / photo.library_path, photo.captured_at, log)
        for index, planned in enumerate(plan.photos, start=1):
            if progress is not None:
                progress(index, len(plan.photos), planned)
            try:
                moves.extend(_refile_one(planned, root, catalog, log))
            except (RefileError, OSError) as exc:
                failure = f"{planned.photo.library_path}: {exc}"
                break
            done += 1

    _prune_emptied(root, {PurePosixPath(f.source).parent for f in plan.files})
    return RefileOutcome(reversal_path, tuple(moves), done, failure)


def _preflight(plan: RefilePlan) -> None:
    root = plan.library_root
    problems: list[str] = []
    seen: set[str] = set()
    for planned in plan.files:
        source = root / planned.source
        if not source.is_file():
            problems.append(f"{planned.source}: gone since the plan")
        elif content_hash(source) != planned.content_hash:
            problems.append(f"{planned.source}: changed since the plan")
        if (root / planned.target).exists():
            problems.append(f"{planned.target}: something is already there")
        if planned.target.casefold() in seen:
            problems.append(f"{planned.target}: two files both want this path")
        seen.add(planned.target.casefold())
    if problems:
        raise RefileError(
            "The plan no longer matches the library. Re-run the dry run:\n  "
            + "\n  ".join(problems[:20])
            + (f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else "")
        )


def _refile_one(
    planned: PlannedRefile, root: Path, catalog: Catalog, log: _ReversalLog
) -> list[Move]:
    done: list[Move] = []
    try:
        with catalog.transaction():
            for item in planned.files:
                destination = root / item.target
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise RefileError(f"{item.target} appeared while the re-file ran")
                _move(root / item.source, destination)
                done.append(Move(item.source, item.target, item.content_hash))
                if content_hash(destination) != item.content_hash:
                    raise RefileError(f"{item.target} does not hash as it did before the move")
                log.record(done[-1])
            catalog.photos.update(replace(
                planned.photo, library_path=planned.library_path, raw_path=planned.raw_path
            ))
    except BaseException:
        for move in reversed(done):
            _move_back(root / move.target, root / move.source)
            log.record_undo(move)
        raise
    return done


def _set_mtime(path: Path, when: dt.datetime, log: _ReversalLog) -> None:
    """Set *path*'s modified time, keeping its access time, and log the old one.

    ``read_reversal_log`` ignores records it does not know, so an ``mtime``
    record is an account of what happened without being something a reversal
    replays."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, int(when.timestamp() * 1_000_000_000)))
    log._write({
        "kind": "mtime",
        "path": path.relative_to(log._root).as_posix(),
        "from_ns": stat.st_mtime_ns,
        "to": when.isoformat(),
    })


def _prune_emptied(root: Path, directories: set[PurePosixPath]) -> None:
    """Remove the old day folders this emptied — those, and nothing else."""
    for directory in sorted(directories, key=lambda d: len(d.parts), reverse=True):
        path = root / directory
        while path != root and path.is_dir() and not any(path.iterdir()):
            path.rmdir()
            path = path.parent


# ----------------------------------------------------------------------
# Putting it back
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReverseOutcome:
    restored: int = 0
    skipped: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.failures


def reverse_refile(path: Path, catalog: Catalog) -> ReverseOutcome:
    """Undo a re-file from its log: files back to their old folders, and the
    catalog's paths with them. Running it twice is safe."""
    _, moves = read_reversal_log(path)
    root = catalog.settings.library_root
    restored = 0
    skipped: list[str] = []
    failures: list[str] = []
    back: dict[str, str] = {}

    for move in reversed(moves):
        current, original = root / move.target, root / move.source
        if not current.exists():
            if original.exists():
                back[move.target] = move.source
            skipped.append(f"{move.target}: not there (already reversed?)")
            continue
        if original.exists():
            failures.append(f"{move.source}: something is already at the original path")
            continue
        original.parent.mkdir(parents=True, exist_ok=True)
        try:
            _move(current, original)
        except OSError as exc:
            failures.append(f"{move.target}: {exc}")
            continue
        if content_hash(original) != move.content_hash:
            failures.append(f"{move.source}: hash does not match what was moved")
            continue
        back[move.target] = move.source
        restored += 1

    with catalog.transaction():
        for photo in catalog.photos.all():
            library_path = back.get(photo.library_path, photo.library_path)
            raw_path = back.get(photo.raw_path, photo.raw_path) if photo.raw_path else None
            if (library_path, raw_path) != (photo.library_path, photo.raw_path):
                catalog.photos.update(replace(photo, library_path=library_path, raw_path=raw_path))

    _prune_emptied(root, {PurePosixPath(m.target).parent for m in moves})
    return ReverseOutcome(restored, tuple(skipped), tuple(failures))
