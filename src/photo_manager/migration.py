"""The one-off migration of the flat library into dated folders.

Two halves, and the line between them is the important thing about this file.

**Planning (WU-1.4) reads and never writes.** :func:`plan_migration` opens each
file, reads its EXIF, hashes its bytes and works out where it ought to live —
and stops there, producing a manifest for a human to review.

**Executing (WU-1.5) moves hundreds of irreplaceable files.** :func:`execute_migration`
is gated on that review, refuses to run against a library that has changed since
the plan was made, and writes a reversal manifest as it goes so
:func:`reverse_migration` can put everything back. Everything above the
"Executing the plan" banner is read-only; everything below it is not.

The library before it was a few hundred image files in one flat folder (REQUIREMENTS.md §3).
The target shape is D-4's ``YYYY/YYYY-MM-DD/``.

What the plan has to get right, and why each one is here:

* **Dates.** Two files carry no EXIF ``DateTimeOriginal`` and they are the
  hand-renamed, most meaningful ones. FR-8.2a's mtime fallback applies, and the
  photo is flagged so the manifest *shows* the guess rather than burying it.
* **Pairs.** A ``.RAF`` and its ``.JPG`` are one photograph (FR-1.2) and must
  land in the same folder keeping a shared basename, even if a collision forces
  a suffix. Matching is case-insensitive: the library holds
  ``Birthday.RAF`` beside ``birthday.JPG``.
* **Collisions.** Resolved deterministically with a numeric suffix (FR-6.5), so
  re-running the planner produces an identical manifest.
* **Derivatives.** Some library files are crops of others already in the
  library — different bytes, so a content hash will never catch them. The
  manifest is the only place they can be spotted, so this module looks for them
  explicitly and flags what it finds rather than leaving it to the eye.

Filenames are **preserved**, not rewritten to D-7's
``YYYY-MM-DD_location-slug_DSCF####.jpg``. D-7's slug needs a location, no
location is confirmed until this manifest has been reviewed, and slug
generation belongs to WU-3.2. Moving and renaming in one irreversible step also
makes the reversal manifest do two jobs at once. The rename is a later,
catalog-aware pass over a library that is by then already in the right shape.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import tomllib
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .catalog import Catalog
from .config import Settings
from .hashing import content_hash
from .metadata import (
    RAF_MAGIC,
    DateSource,
    MetadataError,
    PhotoMetadata,
    read_metadata,
    resolve_capture_date,
)
from .models import Photo
# Moved to paths.py once the card import needed the same answers, and
# re-exported here because plan_migration, run_migration, scan_card and three
# test modules all import them from this module by name.
from .paths import directory_snapshot, free_names, suffixed, target_directory

__all__ = [
    "MANIFEST_VERSION",
    "Override",
    "OverrideError",
    "load_overrides",
    "NoteKind",
    "Note",
    "NameHint",
    "PlannedPhoto",
    "UnreadableFile",
    "IgnoredEntry",
    "MigrationPlan",
    "parse_name_hint",
    "target_directory",
    "plan_migration",
    "directory_snapshot",
    "write_manifest",
    "write_report",
    "Move",
    "MigrationOutcome",
    "MigrationError",
    "execute_migration",
    "ReversalOutcome",
    "reverse_migration",
    "read_reversal_log",
    "REVERSAL_VERSION",
    "summarise",
]

#: Bumped when the manifest's shape changes, so WU-1.5 can refuse one it does
#: not understand rather than half-reading it.
MANIFEST_VERSION = 1

#: What the camera names its files. Anything else was named by a human, and
#: FR-8.2 says that name is a signal worth keeping.
_CAMERA_NAME = re.compile(r"^DSCF\d{4}$", re.IGNORECASE)

#: Trailing copy markers that other software adds: ``_1``, ``-2``, ``(3)``.
#: Stripping one is what links ``Birthday_1.jpg`` back to
#: ``birthday.JPG``.
_COPY_MARKER = re.compile(r"^(?P<stem>.+?)[ _-]*(?:\((?P<paren>\d+)\)|[_-](?P<plain>\d+))$")

_JPEG_MAGIC = b"\xff\xd8"


class NoteKind:
    """The reasons a planned photo might need a human's eye.

    Plain strings rather than an enum because they are written into the JSON
    manifest and read back by WU-1.5; a string survives that round trip
    unchanged.
    """

    INFERRED_DATE = "inferred-date"
    DATE_OVERRIDDEN = "date-overridden"
    DATE_FROM_RAW = "date-from-raw"
    HAND_NAMED = "hand-named"
    COLLISION = "collision"
    DUPLICATE_CONTENT = "duplicate-content"
    POSSIBLE_DERIVATIVE = "possible-derivative"
    UNPAIRED_RAW = "unpaired-raw"
    AMBIGUOUS_PAIR = "ambiguous-pair"
    RAW_DATE_DISAGREES = "raw-date-disagrees"

    #: The order the report prints them in: most alarming first.
    ORDER = (
        DUPLICATE_CONTENT,
        COLLISION,
        AMBIGUOUS_PAIR,
        UNPAIRED_RAW,
        RAW_DATE_DISAGREES,
        POSSIBLE_DERIVATIVE,
        DATE_OVERRIDDEN,
        INFERRED_DATE,
        DATE_FROM_RAW,
        HAND_NAMED,
    )

    #: One line per kind for the report, so a reader is never left guessing
    #: what a flag means or what is being asked of them.
    EXPLANATIONS = {
        DUPLICATE_CONTENT: "Byte-identical to another file. content_hash is UNIQUE, so "
                           "the migration can only keep one of them.",
        COLLISION: "Two files wanted the same target path. A numeric suffix was applied.",
        AMBIGUOUS_PAIR: "Several files share a basename, so RAW pairing is not obvious.",
        UNPAIRED_RAW: "A RAF with no JPEG beside it. It still moves, but what its catalog "
                      "row should look like is undecided — D-2 assumes JPEG plus optional "
                      "RAF.",
        RAW_DATE_DISAGREES: "A pair whose RAF and JPEG claim different capture times.",
        POSSIBLE_DERIVATIVE: "Looks like a crop or an edit of another file in the library. "
                             "Different bytes, so no hash will catch it.",
        INFERRED_DATE: "No EXIF capture date. Filed under the file's modification time "
                       "(FR-8.2a). The date is a guess and may be wrong.",
        DATE_OVERRIDDEN: "The date was set by hand at the manifest review, overriding "
                         "the mtime fallback. Still flagged as not-from-EXIF.",
        DATE_FROM_RAW: "No EXIF capture date on the JPEG, so it was taken from its "
                       "paired RAF — the same frame, so not a guess.",
        HAND_NAMED: "Named by hand, not by the camera. The name is a hint about location "
                    "or event (FR-8.2) and needs interpreting before Phase 3 tagging.",
    }


@dataclass(frozen=True, slots=True)
class Override:
    """A decision a human made at the manifest review (FR-8.3).

    Overrides live in a committed TOML file rather than in the generated
    manifest, because the manifest is rewritten on every run and a judgement
    should outlive the tool's output.
    """

    #: The file's original name in the flat library.
    name: str
    captured_at: dt.datetime | None = None
    reason: str = ""


class OverrideError(Exception):
    """An override file is malformed, or names a file that is not there."""


def load_overrides(path: str | Path) -> dict[str, Override]:
    """Read the review's decisions, keyed by casefolded original filename."""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise OverrideError(f"{path} is not valid TOML: {exc}") from exc

    overrides: dict[str, Override] = {}
    for name, body in (data.get("overrides") or {}).items():
        if not isinstance(body, dict):
            raise OverrideError(f"{path}: [overrides.{name!r}] must be a table")
        unknown = set(body) - {"captured_at", "reason"}
        if unknown:
            raise OverrideError(
                f"{path}: [overrides.{name!r}] has unknown key(s) "
                f"{', '.join(sorted(unknown))}"
            )
        captured_at = body.get("captured_at")
        if captured_at is not None and not isinstance(captured_at, dt.datetime):
            raise OverrideError(
                f"{path}: [overrides.{name!r}] captured_at must be a TOML date-time, "
                f"e.g. 2026-04-02T17:27:14"
            )
        overrides[name.casefold()] = Override(
            name=name,
            captured_at=captured_at,
            reason=str(body.get("reason", "")),
        )
    return overrides


@dataclass(frozen=True, slots=True)
class Note:
    """One thing about a planned photo that a human should look at."""

    kind: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class NameHint:
    """What a hand-given filename appears to be saying (FR-8.2).

    This deliberately stops at extracting the phrase. It does **not** decide
    whether the phrase is a location, an event or a person: nothing in a
    filename separates "St Aldo" (a place) from "Birthday" (a pub)
    without knowledge the machine does not have, and a wrong guess written into
    the catalog is worse than a blank a reviewer fills in. The manifest carries
    empty ``suggested_location`` and ``suggested_event`` fields for exactly
    that.
    """

    #: The filename stem as it appears on disk.
    stem: str
    #: Readable form: 'birthday' becomes 'Birthday'.
    phrase: str
    tokens: tuple[str, ...]
    #: The number stripped off a copy marker, if there was one.
    copy_marker: str | None = None
    #: The stem with any copy marker removed, casefolded. This is what links a
    #: derivative back to its source.
    base: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "stem": self.stem,
            "phrase": self.phrase,
            "tokens": list(self.tokens),
            "copy_marker": self.copy_marker,
            "suggested_location": "",
            "suggested_event": "",
        }


@dataclass(frozen=True, slots=True)
class PlannedPhoto:
    """One photograph and where it is going. None of this has happened yet."""

    #: The JPEG — or the RAF itself, when a RAF has no JPEG beside it.
    source: Path
    #: Target path relative to the library root, forward-slashed: the same form
    #: ``photo.library_path`` stores.
    target: str
    content_hash: str
    metadata: PhotoMetadata

    captured_at: dt.datetime
    #: True when :attr:`captured_at` came from mtime rather than EXIF (FR-8.2a).
    captured_at_inferred: bool

    raw_source: Path | None = None
    raw_target: str | None = None
    raw_content_hash: str | None = None

    name_hint: NameHint | None = None
    notes: tuple[Note, ...] = ()

    @property
    def sources(self) -> tuple[Path, ...]:
        return (self.source,) if self.raw_source is None else (self.source, self.raw_source)

    @property
    def has_raw(self) -> bool:
        return self.raw_source is not None

    def note_kinds(self) -> frozenset[str]:
        return frozenset(note.kind for note in self.notes)

    def as_dict(self, library_root: Path) -> dict[str, object]:
        return {
            "source": _relative_source(self.source, library_root),
            "target": self.target,
            "content_hash": self.content_hash,
            "raw_source": (None if self.raw_source is None
                           else _relative_source(self.raw_source, library_root)),
            "raw_target": self.raw_target,
            "raw_content_hash": self.raw_content_hash,
            "captured_at": self.captured_at.isoformat(),
            "captured_at_inferred": self.captured_at_inferred,
            "original_name": self.source.name,
            "width": self.metadata.width,
            "height": self.metadata.height,
            "camera_model": self.metadata.camera_model,
            "lens": self.metadata.lens,
            "exposure": self.metadata.exposure,
            "f_number": self.metadata.f_number,
            "iso": self.metadata.iso,
            "focal_length": self.metadata.focal_length,
            "orientation": self.metadata.orientation,
            "name_hint": None if self.name_hint is None else self.name_hint.as_dict(),
            "notes": [note.as_dict() for note in self.notes],
        }


@dataclass(frozen=True, slots=True)
class UnreadableFile:
    """A file that looked like a photo and could not be read (FR-1.3c).

    One bad frame must never take the migration down with it, so these are
    collected and reported rather than raised.
    """

    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class IgnoredEntry:
    """Something in the library root that is not a top-level image file."""

    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """The whole dry run: every intended move, and everything in the way."""

    library_root: Path
    generated_at: dt.datetime
    photos: tuple[PlannedPhoto, ...] = ()
    unreadable: tuple[UnreadableFile, ...] = ()
    ignored: tuple[IgnoredEntry, ...] = ()

    #: Total bytes across every file that would move. Informational.
    total_bytes: int = 0

    #: Built in __post_init__; here so the dataclass can stay frozen.
    _by_kind: dict[str, list[PlannedPhoto]] = field(
        default_factory=dict, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        index: dict[str, list[PlannedPhoto]] = defaultdict(list)
        for photo in self.photos:
            for kind in sorted(photo.note_kinds()):
                index[kind].append(photo)
        object.__setattr__(self, "_by_kind", dict(index))

    # -- counts ------------------------------------------------------------

    @property
    def file_count(self) -> int:
        """Files that would move. Should equal the library's image count."""
        return sum(len(photo.sources) for photo in self.photos)

    @property
    def raw_pair_count(self) -> int:
        return sum(1 for photo in self.photos if photo.has_raw)

    @property
    def target_directories(self) -> tuple[str, ...]:
        return tuple(sorted({str(PurePosixPath(p.target).parent) for p in self.photos}))

    def with_note(self, kind: str) -> tuple[PlannedPhoto, ...]:
        return tuple(self._by_kind.get(kind, ()))

    def note_counts(self) -> dict[str, int]:
        return {kind: len(photos) for kind, photos in sorted(self._by_kind.items())}

    @property
    def needs_review(self) -> bool:
        """True when anything at all was flagged. WU-1.5's gate cares."""
        return bool(self._by_kind) or bool(self.unreadable)

    # -- output ------------------------------------------------------------

    def to_manifest(self) -> dict[str, object]:
        """The machine-readable manifest — FR-8.4's old-to-new mapping.

        This is what WU-1.5 consumes, and what a reviewer edits to fill in the
        suggested location and event for hand-named files.
        """
        return {
            "manifest_version": MANIFEST_VERSION,
            "generated_at": self.generated_at.isoformat(),
            "library_root": str(self.library_root),
            "dry_run": True,
            "summary": {
                "photos": len(self.photos),
                "files": self.file_count,
                "raw_pairs": self.raw_pair_count,
                "total_bytes": self.total_bytes,
                "target_directories": len(self.target_directories),
                "notes": self.note_counts(),
                "unreadable": len(self.unreadable),
                "ignored": len(self.ignored),
            },
            "photos": [p.as_dict(self.library_root) for p in self.photos],
            "unreadable": [
                {"source": _relative_source(u.path, self.library_root), "reason": u.reason}
                for u in self.unreadable
            ],
            "ignored": [
                {"source": _relative_source(i.path, self.library_root), "reason": i.reason}
                for i in self.ignored
            ],
        }

    def render_report(self) -> str:
        """The human-readable form. This is the thing the owner actually reads."""
        return _render_report(self)


# ----------------------------------------------------------------------
# Names
# ----------------------------------------------------------------------


def parse_name_hint(stem: str) -> NameHint | None:
    """Read meaning out of a filename stem, or None if the camera wrote it.

    ``DSCF1024`` carries nothing a human put there. ``birthday`` does,
    and FR-8.2 says that is the signal this whole project started from.
    """
    if not stem or _CAMERA_NAME.match(stem):
        return None

    base, copy_marker = _strip_copy_marker(stem)
    tokens = tuple(t for t in re.split(r"[\s_\-.]+", base) if t)
    if not tokens:
        return None
    phrase = " ".join(token[:1].upper() + token[1:] for token in tokens)
    return NameHint(
        stem=stem,
        phrase=phrase,
        tokens=tokens,
        copy_marker=copy_marker,
        base=base.casefold(),
    )


def _strip_copy_marker(stem: str) -> tuple[str, str | None]:
    match = _COPY_MARKER.match(stem)
    if match is None:
        return stem, None
    marker = match.group("paren") or match.group("plain")
    return match.group("stem"), marker


def plan_migration(
    settings: Settings,
    *,
    now: dt.datetime | None = None,
    overrides: dict[str, Override] | None = None,
    progress: Callable[[int, int, Path], None] | None = None,
) -> MigrationPlan:
    """Work out where every file in the flat library should go.

    Reads only. Opens each file to read its EXIF and hash its bytes, and writes
    nothing anywhere — the manifest is written by the caller, outside the
    library root.
    """
    root = settings.verify_library_root()
    generated_at = now or dt.datetime.now()
    overrides = dict(overrides or {})

    groups, ignored = _scan(root)
    total = sum(len(paths) for paths in groups.values())

    photos: list[PlannedPhoto] = []
    unreadable: list[UnreadableFile] = []
    done = 0
    for stem in sorted(groups):
        read: list[_Read] = []
        for path in groups[stem]:
            done += 1
            if progress is not None:
                progress(done, total, path)
            try:
                read.append(_read_file(path))
            except (MetadataError, OSError) as exc:
                unreadable.append(UnreadableFile(path, str(exc)))
        photos.extend(_plan_group(read, overrides))

    _check_overrides_were_used(overrides, photos, unreadable)

    photos = _assign_targets(photos, root)
    _annotate(photos)

    total_bytes = sum(source.stat().st_size for p in photos for source in p.sources)
    return MigrationPlan(
        library_root=root,
        generated_at=generated_at,
        photos=tuple(photos),
        unreadable=tuple(unreadable),
        ignored=tuple(ignored),
        total_bytes=total_bytes,
    )


@dataclass(frozen=True, slots=True)
class _Read:
    """A file that has been opened, identified, hashed and parsed."""

    path: Path
    is_raw: bool
    content_hash: str
    metadata: PhotoMetadata
    mtime: dt.datetime


def _scan(root: Path) -> tuple[dict[str, list[Path]], list[IgnoredEntry]]:
    """Group the library root's top-level image files by casefolded basename.

    Case-insensitive because the library holds ``Birthday.RAF`` next to
    ``birthday.JPG`` and they are one photograph (FR-1.2).
    """
    groups: dict[str, list[Path]] = defaultdict(list)
    ignored: list[IgnoredEntry] = []

    for entry in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
        if entry.is_dir():
            # A YYYY folder means a previous run already moved things. The
            # planner only looks at the flat top level, so say so out loud
            # rather than silently planning half a library.
            ignored.append(IgnoredEntry(
                entry, "directory — not scanned; the plan covers the flat top level only"
            ))
            continue
        if not entry.is_file():
            ignored.append(IgnoredEntry(entry, "not a regular file"))
            continue
        if _file_kind(entry) is None:
            ignored.append(IgnoredEntry(entry, "not a JPEG or RAF (checked by magic bytes)"))
            continue
        groups[entry.stem.casefold()].append(entry)

    return groups, ignored


def _file_kind(path: Path) -> str | None:
    """'jpeg', 'raf', or None — decided by magic bytes, never by suffix.

    FR-1.3a. The library contains ``birthday.JPG`` beside
    ``Birthday.RAF``; no suffix on this machine is load bearing.
    """
    try:
        with path.open("rb") as stream:
            magic = stream.read(len(RAF_MAGIC))
    except OSError:
        return None
    if magic.startswith(RAF_MAGIC):
        return "raf"
    if magic.startswith(_JPEG_MAGIC):
        return "jpeg"
    return None


def _read_file(path: Path) -> _Read:
    stat = path.stat()
    return _Read(
        path=path,
        is_raw=_file_kind(path) == "raf",
        content_hash=content_hash(path),
        metadata=read_metadata(path),
        # Truncated to whole seconds to match EXIF's resolution. An inferred
        # date is already a guess; giving it microsecond precision the EXIF
        # dates do not have would make catalog timestamps inconsistent and
        # imply an accuracy that is not there.
        mtime=dt.datetime.fromtimestamp(stat.st_mtime).replace(microsecond=0),
    )


def _check_overrides_were_used(
    overrides: dict[str, Override],
    photos: Sequence[PlannedPhoto],
    unreadable: Sequence[UnreadableFile],
) -> None:
    """An override naming a file that is not there is an error.

    A typo that silently does nothing is worse than one that stops the run: the
    reviewer would believe a decision had been applied when it had not, and
    would find out after hundreds of irreplaceable files had moved.
    """
    seen = {p.source.name.casefold() for p in photos}
    seen |= {u.path.name.casefold() for u in unreadable}
    missing = sorted(o.name for key, o in overrides.items() if key not in seen)
    if missing:
        raise OverrideError(
            "Override(s) name files that are not in the library: "
            + ", ".join(missing)
            + ". Check the spelling, or remove the entry."
        )


def _plan_group(
    read: Sequence[_Read], overrides: dict[str, Override]
) -> list[PlannedPhoto]:
    """Turn one basename group into planned photos, pairing RAF with JPEG."""
    if not read:
        return []

    jpegs = [r for r in read if not r.is_raw]
    raws = [r for r in read if r.is_raw]

    if len(jpegs) == 1 and len(raws) <= 1:
        return [_plan_photo(jpegs[0], raws[0] if raws else None, overrides=overrides)]

    if not jpegs and len(raws) == 1:
        # A RAF on its own. It still has to move or the library is left half
        # flat, but D-2's model is a JPEG with an optional RAF, so what its
        # catalog row looks like is a question for WU-1.5's review.
        return [_plan_photo(raws[0], None, overrides=overrides, extra=(
            Note(NoteKind.UNPAIRED_RAW, "No JPEG beside this RAF."),
        ))]

    # More than one file of a kind sharing a basename. Rather than guess which
    # RAF belongs to which JPEG, plan every file on its own and say so.
    detail = "Basename shared by: " + ", ".join(sorted(r.path.name for r in read))
    return [
        _plan_photo(item, None, overrides=overrides,
                    extra=(Note(NoteKind.AMBIGUOUS_PAIR, detail),))
        for item in sorted(read, key=lambda r: r.path.name.casefold())
    ]


def _plan_photo(
    primary: _Read,
    raw: _Read | None,
    *,
    overrides: dict[str, Override] | None = None,
    extra: tuple[Note, ...] = (),
) -> PlannedPhoto:
    notes = list(extra)
    captured_at, inferred, date_notes = _resolve_capture_date(primary, raw)
    notes.extend(date_notes)

    override = (overrides or {}).get(primary.path.name.casefold())
    if override is not None and override.captured_at is not None:
        # The flag stays set. It means "this date did not come from this file's
        # EXIF", which a reviewed decision does not change — and Phase 4's
        # "photos with an uncertain date" view should still surface it.
        notes.append(Note(
            NoteKind.DATE_OVERRIDDEN,
            f"Filed under {override.captured_at.isoformat()} by decision at the "
            f"manifest review, not {captured_at.isoformat()}."
            + (f" {override.reason}" if override.reason else ""),
        ))
        captured_at, inferred = override.captured_at, True

    hint = parse_name_hint(primary.path.stem)
    if hint is not None:
        notes.append(Note(NoteKind.HAND_NAMED, f"Reads as {hint.phrase!r}."))

    return PlannedPhoto(
        source=primary.path,
        target="",  # assigned once every photo's date is known
        content_hash=primary.content_hash,
        metadata=primary.metadata,
        captured_at=captured_at,
        captured_at_inferred=inferred,
        raw_source=None if raw is None else raw.path,
        raw_content_hash=None if raw is None else raw.content_hash,
        name_hint=hint,
        notes=tuple(notes),
    )


def _resolve_capture_date(
    primary: _Read, raw: _Read | None
) -> tuple[dt.datetime, bool, list[Note]]:
    """EXIF first, then the paired RAF's EXIF, then mtime (FR-8.2a).

    The middle step is not a fallback chain in the sense FR-8.2a rules out: a
    pair is one frame, so the RAF's ``DateTimeOriginal`` *is* that frame's real
    capture time, not a guess about it. Only mtime is a guess, and only mtime
    sets the inferred flag.
    """
    notes: list[Note] = []
    primary_date = primary.metadata.captured_at
    raw_date = None if raw is None else raw.metadata.captured_at

    if primary_date is not None and raw_date is not None and primary_date != raw_date:
        assert raw is not None
        notes.append(Note(
            NoteKind.RAW_DATE_DISAGREES,
            f"{primary.path.name} says {primary_date.isoformat()}, "
            f"{raw.path.name} says {raw_date.isoformat()}. Using the JPEG's.",
        ))

    # The rule itself lives in metadata.py, so the card import cannot come to a
    # different conclusion about it. What stays here is the explaining.
    resolved, source = resolve_capture_date(
        primary.metadata, primary.mtime, None if raw is None else raw.metadata
    )

    if source == DateSource.PAIRED_RAW:
        assert raw is not None
        notes.append(Note(
            NoteKind.DATE_FROM_RAW,
            f"No EXIF date on {primary.path.name}; took {resolved.isoformat()} "
            f"from its pair {raw.path.name}.",
        ))
    elif source == DateSource.MTIME:
        notes.append(Note(
            NoteKind.INFERRED_DATE,
            f"No EXIF DateTimeOriginal. Filed under the file's modification time, "
            f"{primary.mtime.isoformat(timespec='seconds')} — a guess, not a fact.",
        ))

    return resolved, source == DateSource.MTIME, notes


# ----------------------------------------------------------------------
# Targets and collisions
# ----------------------------------------------------------------------


def _assign_targets(photos: list[PlannedPhoto], root: Path) -> list[PlannedPhoto]:
    """Give every photo its ``YYYY/YYYY-MM-DD/name`` and resolve collisions.

    Deterministic in two senses that both matter. The order photos are
    considered in is fixed, so re-running produces an identical manifest. And a
    pair's JPEG and RAF are suffixed together, so they never stop sharing a
    basename and stop being pairable (FR-1.2).
    """
    ordered = sorted(photos, key=lambda p: (p.captured_at, p.source.name.casefold()))

    #: Targets already claimed, casefolded — Windows will not distinguish
    #: ``Wall.JPG`` from ``wall.JPG`` in one folder even though Python would.
    claimed: set[str] = set()
    planned: list[PlannedPhoto] = []

    for photo in ordered:
        directory = target_directory(photo.captured_at)
        names = [photo.source.name]
        if photo.raw_source is not None:
            names.append(photo.raw_source.name)

        suffix, resolved = free_names(directory, names, claimed, root)
        for name in resolved:
            claimed.add(f"{directory}/{name}".casefold())

        notes = photo.notes
        if suffix:
            notes = notes + (Note(
                NoteKind.COLLISION,
                f"{directory}/{names[0]} was already claimed; "
                f"filed as {resolved[0]} instead.",
            ),)

        planned.append(_replace(
            photo,
            target=f"{directory}/{resolved[0]}",
            raw_target=None if photo.raw_source is None else f"{directory}/{resolved[1]}",
            notes=notes,
        ))

    return planned


# ----------------------------------------------------------------------
# Things a human has to look at
# ----------------------------------------------------------------------


def _annotate(photos: list[PlannedPhoto]) -> None:
    """Add the cross-file notes: duplicates and suspected derivatives.

    Mutates the list in place, replacing entries with annotated copies. Runs
    after targets are assigned so a note can name a file by its final path.
    """
    added: dict[int, list[Note]] = defaultdict(list)

    _flag_duplicate_content(photos, added)
    _flag_shared_capture_time(photos, added)
    _flag_matching_exposure(photos, added)
    _flag_related_names(photos, added)

    for index, notes in added.items():
        photos[index] = _replace(photos[index], notes=photos[index].notes + tuple(notes))


def _flag_duplicate_content(
    photos: Sequence[PlannedPhoto], added: dict[int, list[Note]]
) -> None:
    """Byte-identical files. ``content_hash`` is UNIQUE, so WU-1.5 cannot
    import both and needs to know before it tries."""
    by_hash: dict[str, list[int]] = defaultdict(list)
    for index, photo in enumerate(photos):
        by_hash[photo.content_hash].append(index)

    for indexes in by_hash.values():
        if len(indexes) < 2:
            continue
        for index in indexes:
            others = sorted(photos[i].source.name for i in indexes if i != index)
            added[index].append(Note(
                NoteKind.DUPLICATE_CONTENT,
                "Byte-identical to " + ", ".join(others) + ".",
            ))


def _flag_shared_capture_time(
    photos: Sequence[PlannedPhoto], added: dict[int, list[Note]]
) -> None:
    """Two files claiming the same EXIF capture instant are the same frame.

    The strongest derivative signal there is, and it finds both of the real
    cases in the library: ``wall.JPG`` is a crop of ``DSCF0953.JPG`` and
    ``family.jpg`` of ``DSCF1133.JPG``. A RAF and its JPEG share an instant
    too, of course — which is why they are one :class:`PlannedPhoto` and never
    compared against each other here.
    """
    by_time: dict[dt.datetime, list[int]] = defaultdict(list)
    for index, photo in enumerate(photos):
        if not photo.captured_at_inferred:
            by_time[photo.captured_at].append(index)

    for instant, indexes in by_time.items():
        if len(indexes) < 2:
            continue
        for index in indexes:
            others = sorted(photos[i].source.name for i in indexes if i != index)
            added[index].append(Note(
                NoteKind.POSSIBLE_DERIVATIVE,
                f"Same capture instant ({instant.isoformat()}) as "
                + ", ".join(others)
                + " — the same frame twice, so one is a crop or an edit.",
            ))


def _flag_matching_exposure(
    photos: Sequence[PlannedPhoto], added: dict[int, list[Note]]
) -> None:
    """The fallback signal, for a derivative whose EXIF date was stripped.

    ``seafront.JPG`` has no capture date at all, so the timestamp check cannot
    see it — but it still carries the camera's exposure settings, and that
    exact combination belongs to exactly one frame in the library. Reported
    only when the match is unique and every field is present: bursts share
    settings constantly, so anything looser is noise rather than signal.
    """
    signatures: dict[tuple[object, ...], list[int]] = defaultdict(list)
    for index, photo in enumerate(photos):
        signature = _exposure_signature(photo.metadata)
        if signature is not None:
            signatures[signature].append(index)

    for index, photo in enumerate(photos):
        if not photo.captured_at_inferred:
            continue
        signature = _exposure_signature(photo.metadata)
        if signature is None:
            continue
        others = [i for i in signatures[signature] if i != index]
        if len(others) != 1:
            continue
        other = photos[others[0]]
        added[index].append(Note(
            NoteKind.POSSIBLE_DERIVATIVE,
            f"No capture date, but its exposure settings "
            f"({_describe_signature(photo.metadata)}) match exactly one frame in the "
            f"library: {other.source.name}." + _folder_mismatch(photo, other),
        ))


def _exposure_signature(metadata: PhotoMetadata) -> tuple[object, ...] | None:
    values = (
        metadata.camera_model, metadata.exposure, metadata.f_number,
        metadata.iso, metadata.focal_length,
    )
    return None if any(value is None for value in values) else values


def _describe_signature(metadata: PhotoMetadata) -> str:
    return (f"{metadata.exposure}s f/{metadata.f_number} ISO {metadata.iso} "
            f"{metadata.focal_length}mm")


def _flag_related_names(
    photos: Sequence[PlannedPhoto], added: dict[int, list[Note]]
) -> None:
    """A hand-named file whose stem, minus its copy marker, is another file's.

    ``Birthday_1.jpg`` has no EXIF at all — no date, no lens, no exposure
    — so neither of the other two signals can reach it. Its name is the only
    thing left connecting it to ``birthday.JPG``.
    """
    by_stem: dict[str, list[int]] = defaultdict(list)
    for index, photo in enumerate(photos):
        by_stem[photo.source.stem.casefold()].append(index)

    for index, photo in enumerate(photos):
        hint = photo.name_hint
        if hint is None or hint.copy_marker is None:
            continue
        for other_index in by_stem.get(hint.base, ()):
            if other_index == index:
                continue
            other = photos[other_index]
            added[index].append(Note(
                NoteKind.POSSIBLE_DERIVATIVE,
                f"Name looks like a copy of {other.source.name}."
                + _folder_mismatch(photo, other),
            ))


def _folder_mismatch(photo: PlannedPhoto, other: PlannedPhoto) -> str:
    """Say so when a suspected derivative is filed away from its source.

    This is the real ``Birthday_1.jpg`` case: no EXIF at all, so it falls
    back to mtime and lands on 2026-04-03 while the frame it came from sits in
    2026-04-02. Both facts are already in the report separately; a reader
    should not have to join them up to notice.
    """
    here = PurePosixPath(photo.target).parent
    there = PurePosixPath(other.target).parent
    if here == there:
        return ""
    return (f" They are filed a folder apart — this one under `{here}`, "
            f"that one under `{there}`.")


def _replace(photo: PlannedPhoto, **changes: object) -> PlannedPhoto:
    """``dataclasses.replace`` for a frozen slotted class, spelled out.

    ``replace()`` itself works here, but it silently picks up any field added
    later; listing them keeps a new field from arriving unnoticed.
    """
    values: dict[str, object] = {
        "source": photo.source,
        "target": photo.target,
        "content_hash": photo.content_hash,
        "metadata": photo.metadata,
        "captured_at": photo.captured_at,
        "captured_at_inferred": photo.captured_at_inferred,
        "raw_source": photo.raw_source,
        "raw_target": photo.raw_target,
        "raw_content_hash": photo.raw_content_hash,
        "name_hint": photo.name_hint,
        "notes": photo.notes,
    }
    values.update(changes)
    return PlannedPhoto(**values)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Proving nothing moved
# ----------------------------------------------------------------------


def _relative_source(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _render_report(plan: MigrationPlan) -> str:
    lines: list[str] = []
    out = lines.append

    out("# Library migration — dry run")
    out("")
    out(f"Generated {plan.generated_at.isoformat(timespec='seconds')} from "
        f"`{plan.library_root}`.")
    out("")
    out("**Nothing has been moved.** This is a plan. Executing it is WU-1.5, which is")
    out("gated on a verified backup and on your approval of this document.")
    out("")

    out("## Summary")
    out("")
    out("| | |")
    out("|---|---|")
    out(f"| Photographs | {len(plan.photos)} |")
    out(f"| Files to move | {plan.file_count} |")
    out(f"| RAW pairs | {plan.raw_pair_count} |")
    out(f"| Date folders | {len(plan.target_directories)} |")
    out(f"| Total size | {plan.total_bytes / 1e9:.2f} GB |")
    out(f"| Unreadable | {len(plan.unreadable)} |")
    out(f"| Ignored (not top-level images) | {len(plan.ignored)} |")
    out("")

    counts = plan.note_counts()
    if counts:
        out("## Flagged for review")
        out("")
        out("| Flag | Photos | What it means |")
        out("|---|---|---|")
        for kind in NoteKind.ORDER:
            if kind in counts:
                out(f"| `{kind}` | {counts[kind]} | {NoteKind.EXPLANATIONS[kind]} |")
        out("")
    else:
        out("## Flagged for review")
        out("")
        out("Nothing flagged.")
        out("")

    for kind in NoteKind.ORDER:
        flagged = plan.with_note(kind)
        if not flagged:
            continue
        out(f"### {kind}")
        out("")
        for photo in sorted(flagged, key=lambda p: p.source.name.casefold()):
            out(f"- **{photo.source.name}** → `{photo.target}`")
            for note in photo.notes:
                if note.kind == kind:
                    out(f"  - {note.detail}")
        out("")

    if plan.with_note(NoteKind.HAND_NAMED):
        out("### Names to interpret")
        out("")
        out("FR-8.2 keeps these names as a hint. Nothing here guesses whether a name is a")
        out("location, an event or a person — fill the blanks in the JSON manifest and")
        out("Phase 3's tagging has somewhere to start.")
        out("")
        out("| File | Reads as | Filed under | Location? | Event? |")
        out("|---|---|---|---|---|")
        for photo in sorted(plan.with_note(NoteKind.HAND_NAMED),
                            key=lambda p: p.source.name.casefold()):
            assert photo.name_hint is not None
            folder = str(PurePosixPath(photo.target).parent)
            out(f"| `{photo.source.name}` | {photo.name_hint.phrase} | `{folder}` | | |")
        out("")

    if plan.unreadable:
        out("### Unreadable")
        out("")
        for item in plan.unreadable:
            out(f"- `{item.path.name}` — {item.reason}")
        out("")

    if plan.ignored:
        out("### Ignored")
        out("")
        for item in plan.ignored:
            out(f"- `{item.path.name}` — {item.reason}")
        out("")

    out("## Every move")
    out("")
    by_directory: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for photo in plan.photos:
        directory = str(PurePosixPath(photo.target).parent)
        by_directory[directory].append(
            (photo.source.name, PurePosixPath(photo.target).name)
        )
        if photo.raw_source is not None and photo.raw_target is not None:
            by_directory[directory].append(
                (photo.raw_source.name, PurePosixPath(photo.raw_target).name)
            )

    for directory in sorted(by_directory):
        moves = sorted(by_directory[directory], key=lambda move: move[0].casefold())
        plural = "file" if len(moves) == 1 else "files"
        out(f"### `{directory}/` — {len(moves)} {plural}")
        out("")
        for old, new in moves:
            out(f"- {f'`{old}`' if old == new else f'`{old}` → `{new}`'}")
        out("")

    return "\n".join(lines).rstrip() + "\n"


def write_manifest(plan: MigrationPlan, path: Path) -> Path:
    """Write the JSON manifest, refusing to put it inside the library."""
    path = _outside_library(plan, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan.to_manifest(), indent=2), encoding="utf-8")
    return path


def write_report(plan: MigrationPlan, path: Path) -> Path:
    """Write the human-readable report, refusing to put it inside the library."""
    path = _outside_library(plan, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.render_report(), encoding="utf-8")
    return path


def _outside_library(plan: MigrationPlan, path: Path) -> Path:
    """The library root holds photos and nothing else (REQUIREMENTS.md §4.2).

    A dry run that wrote a file into the folder it is promising not to touch
    would also break the before/after snapshot that proves it was dry.
    """
    resolved = Path(path).expanduser().resolve()
    root = plan.library_root.resolve()
    if resolved == root or root in resolved.parents:
        raise ValueError(
            f"Refusing to write {resolved} inside the library root {root}. "
            f"The library holds photos and nothing else."
        )
    return resolved


def summarise(plan: MigrationPlan) -> str:
    """One paragraph for a terminal, when the whole report is too much.

    Kept to ASCII. The written report is UTF-8 and keeps its typography, but a
    Windows console is cp1252 more often than not and an em dash arrives there
    as a replacement character.
    """
    parts = [
        f"{len(plan.photos)} photographs / {plan.file_count} files",
        f"{plan.raw_pair_count} RAW pairs",
        f"{len(plan.target_directories)} date folders",
        f"{plan.total_bytes / 1e9:.2f} GB",
    ]
    flagged = ", ".join(f"{count} {kind}" for kind, count in sorted(plan.note_counts().items()))
    return " | ".join(parts) + (f"\nFlagged: {flagged}" if flagged else "\nNothing flagged.")


# ----------------------------------------------------------------------
# Executing the plan — WU-1.5
# ----------------------------------------------------------------------
#
# Everything above this line reads. Everything below it moves hundreds of irreplaceable
# files, so the shape of it is dictated by what has to remain true if it stops
# halfway through, at any line, for any reason:
#
# * **No photo is ever in no place.** Files move by rename within one volume,
#   which is atomic — a frame is at its old path or its new one, never between
#   them and never half-written. Renaming also needs no second copy of 3 GB and
#   cannot run the disk out of space mid-run.
# * **The record is written before it is needed.** Each move is appended to the
#   reversal manifest and fsynced *as it happens*, so a power cut leaves a
#   complete account of exactly what moved.
# * **A crash leaves a consistent catalog, not an empty one.** Each photograph
#   is its own transaction: files moved, hashes verified, row committed.
#   Stopping at photo 100 leaves 99 moved with 99 rows, which is resumable. One
#   transaction around all of them would roll back every row while leaving every
#   file moved — the worst of both.

REVERSAL_VERSION = 1


@dataclass(frozen=True, slots=True)
class Move:
    """One file that actually moved, and the proof it arrived intact."""

    source: str
    target: str
    content_hash: str

    def as_dict(self) -> dict[str, str]:
        return {"from": self.source, "to": self.target, "sha256": self.content_hash}


@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    """What the execution did. Returned whether or not it finished."""

    library_root: Path
    reversal_path: Path
    moves: tuple[Move, ...] = ()
    photos_catalogued: int = 0
    failure: str | None = None

    @property
    def complete(self) -> bool:
        return self.failure is None

    @property
    def files_moved(self) -> int:
        return len(self.moves)


class MigrationError(Exception):
    """The migration could not proceed, and stopped rather than guessing."""


def execute_migration(
    plan: MigrationPlan,
    catalog: Catalog,
    *,
    reversal_path: Path,
    imported_at: dt.datetime | None = None,
    progress: Callable[[int, int, PlannedPhoto], None] | None = None,
) -> MigrationOutcome:
    """Move every planned file and write a catalog row for each photograph.

    Refuses to start unless the plan still describes the library on disk: every
    source present, every hash unchanged since it was planned, every target
    free. A plan reviewed yesterday against a library edited since is not a plan
    anyone approved.

    Stops at the first failure rather than pressing on, and a move that fails
    verification is undone before stopping — the library is never left holding a
    file whose bytes are in doubt.
    """
    root = plan.library_root
    imported_at = imported_at or dt.datetime.now()

    if plan.unreadable:
        raise MigrationError(
            f"{len(plan.unreadable)} file(s) could not be read when the plan was made. "
            f"Fix or remove them and re-plan; this will not move a library it cannot "
            f"fully account for."
        )

    _preflight(plan, root)

    moves: list[Move] = []
    catalogued = 0
    failure: str | None = None

    with _ReversalLog(reversal_path, root, plan.generated_at) as log:
        for index, photo in enumerate(plan.photos, start=1):
            if progress is not None:
                progress(index, len(plan.photos), photo)
            try:
                done = _migrate_one(photo, root, catalog, log, imported_at)
            except (MigrationError, OSError) as exc:
                failure = f"{photo.source.name}: {exc}"
                break
            moves.extend(done)
            catalogued += 1

    return MigrationOutcome(
        library_root=root,
        reversal_path=reversal_path,
        moves=tuple(moves),
        photos_catalogued=catalogued,
        failure=failure,
    )


def _preflight(plan: MigrationPlan, root: Path) -> None:
    """Check the whole plan against the disk before touching anything.

    All of it, up front. Discovering the twelfth file missing after eleven have
    already moved is a far worse position than discovering it before any have.
    """
    problems: list[str] = []
    claimed: set[str] = set()

    for photo in plan.photos:
        for source, target, expected in _file_moves(photo):
            if not source.is_file():
                problems.append(f"{source.name}: gone from the library since the plan")
                continue
            actual = content_hash(source)
            if actual != expected:
                problems.append(
                    f"{source.name}: changed since the plan "
                    f"(was {expected[:12]}, now {actual[:12]})"
                )
            if (root / target).exists():
                problems.append(f"{target}: something is already there")
            if target.casefold() in claimed:
                problems.append(f"{target}: two photos both want this path")
            claimed.add(target.casefold())

    if problems:
        raise MigrationError(
            "The plan no longer matches the library. Re-run the dry run and review it "
            "again:\n  " + "\n  ".join(problems[:20])
            + (f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else "")
        )


def _file_moves(photo: PlannedPhoto) -> list[tuple[Path, str, str]]:
    """Every (source, relative target, expected hash) this photograph involves."""
    moves = [(photo.source, photo.target, photo.content_hash)]
    if photo.raw_source is not None and photo.raw_target is not None:
        assert photo.raw_content_hash is not None
        moves.append((photo.raw_source, photo.raw_target, photo.raw_content_hash))
    return moves


def _migrate_one(
    photo: PlannedPhoto,
    root: Path,
    catalog: Catalog,
    log: "_ReversalLog",
    imported_at: dt.datetime,
) -> list[Move]:
    """Move one photograph's files and write its row, or leave nothing behind."""
    done: list[Move] = []
    try:
        with catalog.transaction():
            for source, target, expected in _file_moves(photo):
                destination = root / target
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise MigrationError(f"{target} appeared while the migration ran")

                _move(source, destination)
                done.append(Move(source.name, target, expected))

                landed = content_hash(destination)
                if landed != expected:
                    raise MigrationError(
                        f"{target} hashes to {landed[:12]} after moving, expected "
                        f"{expected[:12]}"
                    )
                # Recorded only once the bytes at the new path are proven, so the
                # reversal manifest never describes a move that did not fully happen.
                log.record(done[-1])

            catalog.photos.add(_row(photo, imported_at))
    except BaseException:
        # The catalog transaction has rolled itself back; put the files back too,
        # so the library is exactly as it was before this photograph was touched.
        for move in reversed(done):
            _move_back(root / move.target, root / move.source)
            log.record_undo(move)
        raise

    return done


def _row(photo: PlannedPhoto, imported_at: dt.datetime) -> Photo:
    return Photo(
        content_hash=photo.content_hash,
        library_path=photo.target,
        original_name=photo.source.name,
        captured_at=photo.captured_at,
        imported_at=imported_at,
        captured_at_inferred=photo.captured_at_inferred,
        raw_path=photo.raw_target,
        width=photo.metadata.width,
        height=photo.metadata.height,
        camera_model=photo.metadata.camera_model,
        lens=photo.metadata.lens,
        exposure=photo.metadata.exposure,
        f_number=photo.metadata.f_number,
        iso=photo.metadata.iso,
        focal_length=photo.metadata.focal_length,
        orientation=photo.metadata.orientation,
    )


def _move(source: Path, destination: Path) -> None:
    """Rename where possible; copy-and-delete only across volumes.

    A rename is atomic and needs no second copy of the bytes. ``shutil.move`` is
    the fallback for a library root whose subfolders somehow span volumes, which
    a junction or a symlink can arrange.
    """
    try:
        source.rename(destination)
    except OSError:
        shutil.move(str(source), str(destination))


def _move_back(destination: Path, source: Path) -> None:
    if destination.exists() and not source.exists():
        _move(destination, source)


class _ReversalLog:
    """FR-8.4's manifest, written as it happens rather than at the end.

    JSON Lines, flushed and fsynced after every record. A single JSON document
    written at the end would be empty exactly when it is most needed: after a
    crash halfway through.
    """

    def __init__(self, path: Path, root: Path, planned_at: dt.datetime) -> None:
        self._path = path
        self._root = root
        self._planned_at = planned_at
        self._handle: object | None = None

    def __enter__(self) -> "_ReversalLog":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("w", encoding="utf-8", newline="\n")
        self._write({
            "kind": "header",
            "reversal_version": REVERSAL_VERSION,
            "library_root": str(self._root),
            "planned_at": self._planned_at.isoformat(),
            "started_at": dt.datetime.now().isoformat(),
        })
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._handle is not None:
            self._write({"kind": "finished", "at": dt.datetime.now().isoformat()})
            self._handle.close()  # type: ignore[attr-defined]
            self._handle = None

    def record(self, move: Move) -> None:
        self._write({"kind": "move", **move.as_dict()})

    def record_undo(self, move: Move) -> None:
        """A move that was rolled back. Recorded so the log is a true account of
        what happened, not only of what stuck."""
        self._write({"kind": "undone", **move.as_dict()})

    def _write(self, record: dict[str, object]) -> None:
        assert self._handle is not None
        handle = self._handle
        handle.write(json.dumps(record) + "\n")  # type: ignore[attr-defined]
        handle.flush()  # type: ignore[attr-defined]
        os.fsync(handle.fileno())  # type: ignore[attr-defined]


# ----------------------------------------------------------------------
# Putting it back
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReversalOutcome:
    restored: int = 0
    skipped: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.failures


def read_reversal_log(path: Path) -> tuple[dict[str, object], list[Move]]:
    """Read a reversal manifest back: its header, and the moves that stuck."""
    header: dict[str, object] = {}
    moves: list[Move] = []
    undone: set[tuple[str, str]] = set()

    lines = path.read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise MigrationError(
                f"{path}: line {number} is not valid JSON ({exc}). A crash can truncate "
                f"the final line; earlier lines are still a usable account."
            ) from exc
        kind = record.get("kind")
        if kind == "header":
            header = record
        elif kind == "move":
            moves.append(Move(record["from"], record["to"], record["sha256"]))
        elif kind == "undone":
            undone.add((record["from"], record["to"]))

    return header, [m for m in moves if (m.source, m.target) not in undone]


def reverse_migration(
    path: Path,
    *,
    library_root: Path | None = None,
    verify: bool = True,
    progress: Callable[[int, int, Move], None] | None = None,
) -> ReversalOutcome:
    """Undo a migration, putting every file back at its original flat path.

    Reverses in the opposite order to the moves, verifies each file by hash on
    arrival, and treats a file already back where it belongs as done rather than
    as an error — so running it twice is safe.

    It does **not** touch the catalog. A catalog whose files have moved out from
    under it is rebuildable from the library and its sidecars (NFR-2), and
    deleting rows here would give this function two jobs and the ability to
    half-do either.
    """
    header, moves = read_reversal_log(path)
    root = library_root or Path(str(header.get("library_root", "")))
    if not str(root) or not root.is_dir():
        raise MigrationError(f"Library root from {path} is not a directory: {root}")

    restored = 0
    skipped: list[str] = []
    failures: list[str] = []

    for index, move in enumerate(reversed(moves), start=1):
        if progress is not None:
            progress(index, len(moves), move)
        current = root / move.target
        original = root / move.source

        if not current.exists():
            skipped.append(f"{move.target}: not there (already reversed?)")
            continue
        if original.exists():
            failures.append(f"{move.source}: something is already at the original path")
            continue

        try:
            _move(current, original)
        except OSError as exc:
            failures.append(f"{move.target}: {exc}")
            continue

        if verify and content_hash(original) != move.content_hash:
            failures.append(f"{move.source}: hash does not match what was moved")
            continue
        restored += 1

    _prune_empty_directories(root)
    return ReversalOutcome(
        restored=restored, skipped=tuple(skipped), failures=tuple(failures)
    )


def _prune_empty_directories(root: Path) -> None:
    """Remove dated folders a reversal has emptied, and nothing else.

    Only directories, only empty ones, never the root. A reversal that left 24
    empty folders behind would not have put the library back.
    """
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            try:
                path.rmdir()
            except OSError:
                pass
