"""Copying the keepers into the library (REQUIREMENTS.md §6.6).

The first thing in this project that puts a photograph somewhere new. It is
deliberately shaped like :mod:`photo_manager.migration`, because a reader who
has understood one should recognise the other — plan first, check the whole
plan before touching anything, one transaction per photograph, verify by
re-reading. Four things differ, and each of them follows from **the card keeps
the original**.

**There is no reversal manifest.** The migration moved hundreds of irreplaceable files
and needed a record of where each one came from. An import copies: undoing one
is deleting the copies, and the card is still the source of truth. A manifest
would be a second thing to keep correct for no additional safety.

**One bad frame does not stop the other 1,133.** The migration stops at the
first failure, because pressing on through a half-understood library is how you
lose a photograph. Here the requirement says the opposite in as many words
(FR-1.3c), and it is right to: nothing has been consumed, so a frame that fails
can be retried while the other 924 land. Failures are collected and reported
(FR-6.6) rather than thrown.

**The plan does not hash.** Planning reads each frame's EXIF header — ~124 KB,
enough for the capture date and every catalog column — but not its 15 MB body.
The hash is computed *during the copy*, from the bytes already passing through,
because hashing at plan time would mean reading the whole card twice over a
20-37 MB/s reader (C-4) and the second read buys nothing.

**A duplicate is therefore discovered after the copy, not before.** The content
hash is the only thing that settles it (C-3) and the hash costs the read, so
the file is copied to a temporary, hashed, and discarded if the catalog already
has it. A wasted write to an SSD is far cheaper than a second pass over the
card — and the cull has usually excluded duplicates already, so it is rare.

**Every imported photograph gets an XMP sidecar** (D-1, FR-5.6), written inside
the same transaction as its row. The cull's rating lives nowhere else until
Phase 3's tagging arrives, and a rating that exists only in a database this
project calls rebuildable would be a rating that a rebuild loses.

Everything else is the standing rules: the card is opened read-only and nothing
here writes to it (§4.1), bytes are verified at the destination before a row is
committed (FR-6.4), and a failure part-way leaves no half-file, no row and no
orphaned sidecar.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import shutil
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .cards import CardPhoto, CardScan
from .catalog import Catalog
from .config import Settings
from .culling import FREE_SPACE_MARGIN, Decision, Verdict
from .hashing import DEFAULT_CHUNK_SIZE, content_hash
from .metadata import DateSource, MetadataError, PhotoMetadata, read_metadata, resolve_capture_date
from .models import Photo
from .paths import free_names, target_directory
from .sidecars import Sidecar, SidecarError, sidecar_path, write_sidecar

__all__ = [
    "ImportFailed",
    "PlannedImport",
    "ImportPlan",
    "Imported",
    "SkippedDuplicate",
    "Failed",
    "ImportOutcome",
    "plan_import",
    "execute_import",
    "target_filename",
    "summarise",
]


class ImportFailed(Exception):
    """One photograph could not be imported, or the import could not start."""


# ----------------------------------------------------------------------
# Naming (D-7, FR-6.3)
# ----------------------------------------------------------------------


def target_filename(
    captured_at: dt.datetime,
    original_name: str,
    *,
    location_slug: str | None = None,
    extension: str | None = None,
) -> str:
    """``2026-09-06_st-aldo_DSCF1024.jpg``, or without the slug (D-7, FR-6.3).

    The original shutter number survives in the middle of the name, which is
    what keeps a file in the library traceable back to the frame on the card.
    The extension is lower-cased: D-7 writes ``.jpg`` and the camera writes
    ``.JPG``, and one of them has to win for a folder listing to look ordered.

    **A missing location must never block an import** (FR-6.3), so the slug is
    simply left out rather than substituted or waited for. Nothing supplies one
    today — locations arrive with WU-3.2 — which makes the fallback the path
    every real import currently takes, not an edge case.
    """
    stem = Path(original_name).stem
    suffix = (extension if extension is not None else Path(original_name).suffix).lower()
    parts = [captured_at.strftime("%Y-%m-%d")]
    if location_slug:
        parts.append(location_slug)
    parts.append(stem)
    return "_".join(parts) + suffix


# ----------------------------------------------------------------------
# The plan
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedImport:
    """One keeper, where it is going, and everything its row will need."""

    #: Index into the scan the decision was made against. Carried so a caller
    #: can match a result back to the tile the user was looking at.
    index: int
    source: Path
    target: str
    captured_at: dt.datetime
    captured_at_inferred: bool
    metadata: PhotoMetadata
    original_name: str
    jpeg_bytes: int

    #: Present only when the RAF was flagged during culling (D-2). A photo with
    #: a RAF on the card that was not flagged has None here and the RAF is
    #: simply not copied.
    raw_source: Path | None = None
    raw_target: str | None = None
    raw_bytes: int = 0

    rating: int = 0
    #: True when the name had to be suffixed to avoid an existing file (FR-6.5).
    collided: bool = False

    @property
    def total_bytes(self) -> int:
        return self.jpeg_bytes + self.raw_bytes

    @property
    def files(self) -> list[tuple[Path, str]]:
        """Every (source, relative target) this photograph will write."""
        pairs = [(self.source, self.target)]
        if self.raw_source is not None and self.raw_target is not None:
            pairs.append((self.raw_source, self.raw_target))
        return pairs


@dataclass(frozen=True, slots=True)
class Unplannable:
    """A keeper that could not even be planned, and why."""

    index: int
    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class ImportPlan:
    """Everything the import will do, decided before it does any of it."""

    library_root: Path
    photos: tuple[PlannedImport, ...] = ()
    unplannable: tuple[Unplannable, ...] = ()
    #: Free space on the library volume when the plan was made (C-2, FR-4.5).
    free_bytes: int = 0
    generated_at: dt.datetime | None = None

    @property
    def total_bytes(self) -> int:
        return sum(photo.total_bytes for photo in self.photos)

    @property
    def raw_count(self) -> int:
        return sum(1 for photo in self.photos if photo.raw_source is not None)

    @property
    def collisions(self) -> tuple[PlannedImport, ...]:
        return tuple(photo for photo in self.photos if photo.collided)

    @property
    def target_directories(self) -> tuple[str, ...]:
        return tuple(sorted({photo.target.rsplit("/", 1)[0] for photo in self.photos}))

    @property
    def headroom_bytes(self) -> int:
        """What would be left over, keeping FR-4.5's margin in hand."""
        return self.free_bytes - self.total_bytes - FREE_SPACE_MARGIN

    @property
    def fits(self) -> bool:
        return self.headroom_bytes >= 0


def plan_import(
    scan: CardScan,
    decisions: Mapping[int, Decision],
    settings: Settings,
    *,
    location_slug: str | None = None,
    now: dt.datetime | None = None,
) -> ImportPlan:
    """Work out every target path before a single byte is copied.

    Reads each keeper's EXIF header, which is where the capture date that
    decides its folder comes from, and resolves collisions across the whole
    batch so the answer does not depend on how far the import got. A frame
    whose metadata cannot be read is reported as unplannable rather than
    guessed at or silently dropped (FR-1.3c).
    """
    root = settings.library_root
    keepers = [
        (index, scan.photos[index])
        for index in sorted(decisions)
        if decisions[index].verdict == Verdict.KEEP and index < len(scan.photos)
    ]

    read: list[_Described] = []
    unplannable: list[Unplannable] = []

    for index, photo in keepers:
        try:
            captured_at, inferred, metadata = _describe(photo)
        except (MetadataError, OSError) as exc:
            unplannable.append(Unplannable(index, photo.primary.name, str(exc)))
            continue
        read.append(_Described(index, photo, captured_at, inferred, metadata))

    planned = _assign_targets(read, decisions, root, location_slug,
                              taken=_catalogued_paths(settings))

    return ImportPlan(
        library_root=root,
        photos=tuple(planned),
        unplannable=tuple(unplannable),
        free_bytes=shutil.disk_usage(root).free,
        generated_at=now or dt.datetime.now(),
    )


@dataclass(frozen=True, slots=True)
class _Described:
    """A keeper that has been read but not yet named."""

    index: int
    photo: CardPhoto
    captured_at: dt.datetime
    inferred: bool
    metadata: PhotoMetadata


def _describe(photo: CardPhoto) -> tuple[dt.datetime, bool, PhotoMetadata]:
    """Capture time, whether it was inferred, and the metadata for the row.

    A header read, not a decode. The RAF is consulted only when the JPEG has no
    date of its own, which on a camera-written card is never — but the rule is
    FR-8.2a's and is applied identically here and in the migration.
    """
    assert photo.jpeg is not None or photo.raw is not None
    primary = read_metadata(photo.primary)

    raw_metadata: PhotoMetadata | None = None
    if primary.captured_at is None and photo.raw is not None and photo.jpeg is not None:
        raw_metadata = read_metadata(photo.raw)

    mtime = dt.datetime.fromtimestamp(photo.primary.stat().st_mtime).replace(microsecond=0)
    captured_at, source = resolve_capture_date(primary, mtime, raw_metadata)
    return captured_at, source == DateSource.MTIME, primary


def _catalogued_paths(settings: Settings) -> set[str]:
    """Every path the catalog already gives a photograph, casefolded.

    A name is not free just because the Mac has no file there: once *Free up
    Mac space* has run, a photograph can own a path whose only copy is on the
    archive drive. Handing that path to a new frame would put two different
    photographs at one path, and the archive never overwrites a photograph.
    """
    assert settings.database_path is not None  # filled in by Settings
    if not settings.database_path.exists():
        return set()
    with Catalog.open(settings) as catalog:
        rows = catalog.connection.execute("SELECT library_path, raw_path FROM photo")
        return {
            path.casefold() for row in rows for path in (row[0], row[1]) if path is not None
        }


def _assign_targets(
    read: Sequence[_Described],
    decisions: Mapping[int, Decision],
    root: Path,
    location_slug: str | None,
    *,
    taken: set[str] | None = None,
) -> list[PlannedImport]:
    """Name everything, resolving collisions deterministically (FR-6.5).

    Ordered by capture time then filename, exactly as the migration orders its
    own, so two runs over the same card produce the same names. A pair is
    suffixed as a unit, or its JPEG and RAF would stop sharing a basename and
    stop being pairable (FR-1.2).
    """
    ordered = sorted(read, key=lambda entry: (entry.captured_at, entry.photo.primary.name.casefold()))
    claimed: set[str] = set(taken or ())
    planned: list[PlannedImport] = []

    for entry in ordered:
        index, photo, captured_at = entry.index, entry.photo, entry.captured_at
        decision = decisions[index]
        directory = target_directory(captured_at)

        names = [target_filename(captured_at, photo.primary.name, location_slug=location_slug)]
        wants_raw = decision.keep_raw and photo.raw is not None
        if wants_raw:
            assert photo.raw is not None
            names.append(target_filename(
                captured_at, photo.primary.name,
                location_slug=location_slug, extension=photo.raw.suffix,
            ))

        suffix, resolved = free_names(directory, names, claimed, root)
        for name in resolved:
            claimed.add(f"{directory}/{name}".casefold())

        planned.append(PlannedImport(
            index=index,
            source=photo.primary,
            target=f"{directory}/{resolved[0]}",
            captured_at=captured_at,
            captured_at_inferred=entry.inferred,
            metadata=entry.metadata,
            original_name=photo.primary.name,
            jpeg_bytes=photo.jpeg_bytes or photo.raw_bytes,
            raw_source=photo.raw if wants_raw else None,
            raw_target=f"{directory}/{resolved[1]}" if wants_raw else None,
            raw_bytes=photo.raw_bytes if wants_raw else 0,
            rating=decision.rating,
            collided=suffix != 0,
        ))

    return planned


# ----------------------------------------------------------------------
# Doing it
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Imported:
    planned: PlannedImport
    content_hash: str
    bytes_written: int


@dataclass(frozen=True, slots=True)
class SkippedDuplicate:
    """These exact bytes are already in the library (C-3, FR-6.6)."""

    planned: PlannedImport
    content_hash: str
    existing_path: str


@dataclass(frozen=True, slots=True)
class Failed:
    planned: PlannedImport
    reason: str


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """FR-6.6's summary: what landed, what was already there, what did not."""

    imported: tuple[Imported, ...] = ()
    skipped: tuple[SkippedDuplicate, ...] = ()
    failed: tuple[Failed, ...] = ()
    unplannable: tuple[Unplannable, ...] = ()

    @property
    def bytes_written(self) -> int:
        return sum(entry.bytes_written for entry in self.imported)

    @property
    def complete(self) -> bool:
        return not self.failed and not self.unplannable


def execute_import(
    plan: ImportPlan,
    catalog: Catalog,
    *,
    imported_at: dt.datetime | None = None,
    allow_overrun: bool = False,
    progress: Callable[[int, int, PlannedImport], None] | None = None,
) -> ImportOutcome:
    """Copy every keeper, verify it, and write its catalog row.

    Refuses to start an import the library volume cannot hold (FR-4.5), which
    is a check and not a guess — the free space is re-measured here rather than
    trusted from the plan, because a plan made ten minutes ago has had ten
    minutes for something else to fill the disk.
    """
    root = plan.library_root
    imported_at = imported_at or dt.datetime.now()

    free_now = shutil.disk_usage(root).free
    if not allow_overrun and free_now - plan.total_bytes - FREE_SPACE_MARGIN < 0:
        raise ImportFailed(
            f"These keepers need {plan.total_bytes / 1e9:.2f} GB and {root} has "
            f"{free_now / 1e9:.2f} GB free. That does not leave a gigabyte spare. "
            f"Cull further, flag fewer RAFs, or pass allow_overrun."
        )

    imported: list[Imported] = []
    skipped: list[SkippedDuplicate] = []
    failed: list[Failed] = []

    for position, planned in enumerate(plan.photos, start=1):
        if progress is not None:
            progress(position, len(plan.photos), planned)
        try:
            result = _import_one(planned, root, catalog, imported_at)
        except (ImportFailed, SidecarError, OSError, sqlite3.IntegrityError) as exc:
            # FR-1.3c: one frame that will not copy is one frame, not a card.
            failed.append(Failed(planned, f"{type(exc).__name__}: {exc}"))
            continue
        if isinstance(result, SkippedDuplicate):
            skipped.append(result)
        else:
            imported.append(result)

    return ImportOutcome(
        imported=tuple(imported),
        skipped=tuple(skipped),
        failed=tuple(failed),
        unplannable=plan.unplannable,
    )


class _AlreadyHere(Exception):
    """Raised inside the transaction so the row rolls back with everything else."""

    def __init__(self, digest: str, existing: str) -> None:
        super().__init__(existing)
        self.digest = digest
        self.existing = existing


def _import_one(
    planned: PlannedImport,
    root: Path,
    catalog: Catalog,
    imported_at: dt.datetime,
) -> Imported | SkippedDuplicate:
    """One photograph, all of it or none of it.

    The JPEG lands first and settles the duplicate question, so a photograph
    already in the library never costs its RAF's 30 MB as well.
    """
    created: list[Path] = []
    try:
        with catalog.transaction():
            destination = root / planned.target
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = _copy_verified(planned.source, destination)
            created.append(destination)

            existing = catalog.photos.by_hash(digest)
            if existing is not None:
                raise _AlreadyHere(digest, existing.library_path)

            raw_target: str | None = None
            if planned.raw_source is not None and planned.raw_target is not None:
                raw_destination = root / planned.raw_target
                _copy_verified(planned.raw_source, raw_destination)
                created.append(raw_destination)
                raw_target = planned.raw_target

            row = catalog.photos.add(_row(planned, digest, raw_target, imported_at))

            # Inside the transaction, so a sidecar that will not write takes
            # the whole photograph back out with it (FR-5.6). That is the
            # right way round: the cull's rating lives *only* here until
            # something else writes it, and a photograph in the library whose
            # rating exists nowhere but a database is exactly the loss D-1 is
            # about. The sidecar is a kilobyte in a folder we have just
            # successfully written 15 MB to, so failing here means something
            # is genuinely wrong rather than merely inconvenient.
            #
            # **The RAF gets one too.** §5.2 keeps the whole filename,
            # extension included, specifically so that a JPEG and its RAF do
            # not collide on one sidecar — which is only a problem worth
            # solving if both can have one. Someone who opens the RAF in
            # Lightroom should find the rating there, not discover it lives
            # beside a JPEG they are not looking at.
            # **But only when there is something to say.** A sidecar whose
            # entire content is "unrated" is a file that carries no
            # information, and writing one per keeper would double the number
            # of files in every folder in the library — against success
            # criterion 5, which is about Explorer becoming *more* useful.
            # An existing sidecar is still updated, because there the absence
            # of a rating is itself a change.
            tags = Sidecar(rating=row.rating)
            for _, relative in planned.files:
                destination = root / relative
                if tags.empty and not sidecar_path(destination).exists():
                    continue
                created.append(write_sidecar(destination, tags))

    except _AlreadyHere as duplicate:
        _remove(created)
        return SkippedDuplicate(planned, duplicate.digest, duplicate.existing)
    except BaseException:
        # The catalog transaction has rolled itself back. Take the files with
        # it, so a failed photograph leaves the library exactly as it was
        # (§4.1) — no half-file, no orphan beside a row that does not exist.
        _remove(created)
        raise

    return Imported(planned, digest, planned.total_bytes)


def _copy_verified(source: Path, destination: Path) -> str:
    """Copy one file and prove the copy, returning the content hash.

    Written to a ``.partial`` beside the destination and renamed only once the
    bytes are on the disk and have been read back and checked, so nothing ever
    observes a half-written photograph at its real name — not another process,
    not Explorer, and not a later run of this import.

    The source is hashed **as it streams**, and the destination is hashed by
    re-reading it. Comparing those two is FR-6.4: it proves what landed is what
    was read, rather than proving the same buffer equals itself.
    """
    if destination.exists():
        raise ImportFailed(f"{destination.name} already exists; it appeared since planning")

    digest = hashlib.sha256()
    partial = destination.with_name(destination.name + ".partial")
    try:
        with source.open("rb") as reader, partial.open("wb") as writer:
            while chunk := reader.read(DEFAULT_CHUNK_SIZE):
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())

        expected = digest.hexdigest()
        landed = content_hash(partial)
        if landed != expected:
            raise ImportFailed(
                f"{destination.name} hashes to {landed[:12]} on disk, but the bytes read "
                f"from the card hashed to {expected[:12]}. The copy is not the original."
            )
        os.replace(partial, destination)
        return expected
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _remove(paths: list[Path]) -> None:
    for path in reversed(paths):
        path.unlink(missing_ok=True)


def _row(
    planned: PlannedImport, digest: str, raw_target: str | None, imported_at: dt.datetime
) -> Photo:
    return Photo(
        content_hash=digest,
        library_path=planned.target,
        original_name=planned.original_name,
        captured_at=planned.captured_at,
        imported_at=imported_at,
        captured_at_inferred=planned.captured_at_inferred,
        raw_path=raw_target,
        width=planned.metadata.width,
        height=planned.metadata.height,
        camera_model=planned.metadata.camera_model,
        lens=planned.metadata.lens,
        exposure=planned.metadata.exposure,
        f_number=planned.metadata.f_number,
        iso=planned.metadata.iso,
        focal_length=planned.metadata.focal_length,
        orientation=planned.metadata.orientation,
        rating=planned.rating,
    )


# ----------------------------------------------------------------------
# Saying what happened (FR-6.6)
# ----------------------------------------------------------------------


def summarise(outcome: ImportOutcome) -> str:
    """One paragraph for a terminal. ASCII, because a Windows console is
    cp1252 more often than not."""
    lines = [
        f"Imported {len(outcome.imported)} photographs "
        f"({outcome.bytes_written / 1e9:.2f} GB)."
    ]
    if outcome.skipped:
        lines.append(
            f"Skipped {len(outcome.skipped)} already in the library: "
            + ", ".join(entry.planned.original_name for entry in outcome.skipped[:8])
        )
    if outcome.failed:
        lines.append(f"FAILED {len(outcome.failed)}:")
        lines.extend(
            f"  {entry.planned.original_name}: {entry.reason}"
            for entry in outcome.failed[:10]
        )
    if outcome.unplannable:
        lines.append(f"Could not even be planned ({len(outcome.unplannable)}):")
        lines.extend(
            f"  {entry.name}: {entry.reason}" for entry in outcome.unplannable[:10]
        )
    return "\n".join(lines)
