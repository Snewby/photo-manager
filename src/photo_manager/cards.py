"""Finding the memory card, and reading what is on it.

WU-2.1. Two jobs, and the first one exists because of a bug worth naming.

**Finding the card.** ``card_dcim_root`` used to default to ``D:/DCIM``, and
``Settings.card_image_folders()`` returned an empty list for two completely
different situations: *no card is inserted*, and *the card is on ``E:`` and we
are looking at ``D:``*. Both presented to the user as an empty grid. Drive
letters move between readers, ports and machines, so this was going to happen.
:func:`detect_card` therefore probes the volumes actually attached and returns a
:class:`CardLocation` carrying a **status**, never a bare list — every outcome
is a distinct, nameable thing with a sentence explaining it.

**Reading it.** Enumerate the ``1??_FUJI`` folders, pair ``.RAF`` with ``.JPG``
by basename (FR-1.2), and say which photos are already in the catalog (FR-1.4,
C-3).

That last part is where the measurements bite. This card reader runs at **20-33
MB/s**, so hashing a full card's thousand-odd JPEGs — some 16 GB — takes about **ten
minutes**. Reading FR-1.4's "hash each JPEG" as something the scan does up front
would mean ten minutes of nothing before culling could start, which is success
criterion 1 lost outright. So dedup is two-tier, exactly as thumbnailing is:

* :func:`scan_card` reads **no file contents at all**. It pairs and sizes from
  directory metadata and marks a photo :data:`Dedup.LIKELY_IMPORTED` when the
  catalog already holds its original filename. Whole card in well under a
  second, so the grid can paint immediately.
* :func:`confirm_imports` then hashes **only** those candidates and upgrades
  each to :data:`Dedup.IMPORTED` or :data:`Dedup.NOT_IMPORTED`.

The split is not a shortcut around correctness, and the type makes that hard to
forget: "likely" is its own state and is never spelled the same as "imported".
Nothing may be *discarded* on the cheap verdict — it is for greying a tile out
and skipping it by default. Anything that would drop a photo for good has to
wait for the hash.

**The card is read-only** (§4.1, NFR-3). Nothing in this module opens a file on
it for writing, creates a directory, or renames anything; the tests prove it
with a before-and-after snapshot rather than promising it.
"""

from __future__ import annotations

import os
import string
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .catalog import Catalog
from .config import Settings
from .hashing import content_hash

__all__ = [
    "CardStatus",
    "CardLocation",
    "Dedup",
    "CardPhoto",
    "CardScan",
    "removable_volumes",
    "candidate_volumes",
    "detect_card",
    "scan_card",
    "confirm_imports",
]


class CardStatus:
    """Why a scan can or cannot happen. Strings, so they survive a log or a
    JSON response unchanged."""

    #: A DCIM tree with at least one image folder. The only scannable status.
    FOUND = "found"
    #: Nothing removable is attached. A normal, everyday state.
    NO_MEDIA = "no-media"
    #: Something removable is attached, but nothing on it looks like a camera
    #: card — a USB stick, most likely.
    NO_PHOTOS_ON_MEDIA = "no-photos-on-media"
    #: ``card_dcim_root`` is set explicitly and is not there. Distinct from
    #: "no card", because the fix is different: edit the setting.
    CONFIGURED_PATH_MISSING = "configured-path-missing"
    #: ``card_dcim_root`` exists but holds no folder matching the glob.
    CONFIGURED_PATH_EMPTY = "configured-path-empty"


@dataclass(frozen=True, slots=True)
class CardLocation:
    """Where the card is, or a precise account of why it isn't anywhere."""

    status: str
    #: The DCIM directory, when one was found.
    dcim_root: Path | None = None
    #: Matching ``1??_FUJI`` folders, sorted. Only non-empty when found.
    image_folders: tuple[Path, ...] = ()
    #: Other places a card-looking tree was seen. The point of this field is
    #: the "you configured D: but the card is on E:" case, where knowing the
    #: alternative is the whole of the fix.
    elsewhere: tuple[Path, ...] = ()
    #: A sentence for a human. Always populated, including on success.
    message: str = ""

    @property
    def found(self) -> bool:
        return self.status == CardStatus.FOUND


class Dedup:
    """Whether a card photo is already in the library (FR-1.4, C-3)."""

    #: The catalog has never seen this filename. No hash needed to say so.
    NEW = "new"
    #: The catalog holds this filename. Cheap evidence, not proof — a hash has
    #: not been read. Grey the tile out and skip it by default; never drop it.
    LIKELY_IMPORTED = "likely-imported"
    #: Hash-confirmed: these exact bytes are in the library. Safe to skip.
    IMPORTED = "imported"
    #: The filename matched but the bytes did not. A different photograph that
    #: happens to share a name — offer it for culling.
    NOT_IMPORTED = "not-imported"

    #: States that still owe a hash before anything irreversible happens.
    UNCONFIRMED = frozenset({LIKELY_IMPORTED})


@dataclass(frozen=True, slots=True)
class CardPhoto:
    """One frame on the card: a JPEG, its RAF, or in odd cases just one of them.

    Both halves are optional because both absences happen. A JPEG with no RAF
    is ordinary — the camera can be set to JPEG only. A RAF with no JPEG is
    not, and :attr:`unpaired` surfaces either.
    """

    stem: str
    folder: Path
    jpeg: Path | None = None
    raw: Path | None = None
    jpeg_bytes: int = 0
    raw_bytes: int = 0

    dedup: str = Dedup.NEW
    #: The catalog row this was matched against, once something matched.
    matched_photo_id: int | None = None
    #: Set by :func:`confirm_imports`. Not read during the cheap scan.
    content_hash: str | None = None

    @property
    def primary(self) -> Path:
        """The file that represents this photo: the JPEG where there is one."""
        primary = self.jpeg or self.raw
        assert primary is not None  # a CardPhoto with neither is not built
        return primary

    @property
    def files(self) -> tuple[Path, ...]:
        return tuple(p for p in (self.jpeg, self.raw) if p is not None)

    @property
    def total_bytes(self) -> int:
        return self.jpeg_bytes + self.raw_bytes

    @property
    def has_raw(self) -> bool:
        return self.raw is not None

    @property
    def unpaired(self) -> bool:
        return self.jpeg is None or self.raw is None

    @property
    def needs_confirming(self) -> bool:
        return self.dedup in Dedup.UNCONFIRMED


@dataclass(frozen=True, slots=True)
class CardScan:
    """Everything on the card, paired and triaged."""

    location: CardLocation
    photos: tuple[CardPhoto, ...] = ()
    #: Files in an image folder that are neither JPEG nor RAF.
    ignored: tuple[Path, ...] = ()

    @property
    def photo_count(self) -> int:
        return len(self.photos)

    @property
    def pair_count(self) -> int:
        return sum(1 for p in self.photos if p.jpeg and p.raw)

    @property
    def unpaired(self) -> tuple[CardPhoto, ...]:
        return tuple(p for p in self.photos if p.unpaired)

    @property
    def jpeg_bytes(self) -> int:
        return sum(p.jpeg_bytes for p in self.photos)

    @property
    def total_bytes(self) -> int:
        return sum(p.total_bytes for p in self.photos)

    def by_dedup(self, state: str) -> tuple[CardPhoto, ...]:
        return tuple(p for p in self.photos if p.dedup == state)

    def dedup_counts(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for photo in self.photos:
            counts[photo.dedup] += 1
        return dict(sorted(counts.items()))

    @property
    def to_cull(self) -> tuple[CardPhoto, ...]:
        """What culling should actually offer: everything not known-imported.

        ``LIKELY_IMPORTED`` is deliberately *excluded* — that is the whole
        point of C-3 and success criterion 2 — but because it is only skipped
        and never discarded, an unconfirmed guess costs a photo nothing.
        """
        return tuple(
            p for p in self.photos
            if p.dedup in (Dedup.NEW, Dedup.NOT_IMPORTED)
        )


# ----------------------------------------------------------------------
# Finding it
# ----------------------------------------------------------------------

#: Windows ``GetDriveType`` results worth knowing about.
_DRIVE_REMOVABLE = 2
_DRIVE_FIXED = 3


def removable_volumes() -> tuple[Path, ...]:
    """Every removable volume currently attached, as drive roots.

    Windows only in any real sense; elsewhere this returns nothing and
    :func:`detect_card` falls back to the configured path. That is honest for a
    single-machine, Windows-only app (REQUIREMENTS.md §3) and keeps the tests
    running on any platform, since they inject their own volume list.
    """
    return tuple(root for root, kind in _volumes() if kind == _DRIVE_REMOVABLE)


def candidate_volumes() -> tuple[Path, ...]:
    """Volumes worth probing for a DCIM tree, best first.

    Removable volumes lead. Fixed volumes other than the system drive follow,
    because plenty of built-in SD readers present a card as a fixed disk — the
    exact "the reader moved" failure this function exists to survive. The
    system drive is never probed: the library lives there and a ``DCIM`` folder
    on it would not be a camera card.
    """
    system = Path(os.environ.get("SystemDrive", "C:") + "\\")
    removable = [root for root, kind in _volumes() if kind == _DRIVE_REMOVABLE]
    fixed = [
        root for root, kind in _volumes()
        if kind == _DRIVE_FIXED and root != system
    ]
    return tuple(removable + fixed)


def _volumes() -> list[tuple[Path, int]]:
    if sys.platform != "win32":
        return []
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    mask = kernel32.GetLogicalDrives()
    found: list[tuple[Path, int]] = []
    for index, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << index):
            continue
        root = f"{letter}:\\"
        found.append((Path(root), kernel32.GetDriveTypeW(root)))
    return found


def detect_card(
    settings: Settings,
    *,
    volumes: Sequence[Path] | None = None,
) -> CardLocation:
    """Work out where the card is, and say precisely what happened if it isn't.

    An explicitly configured ``card_dcim_root`` is honoured and never silently
    overridden — but when it is wrong, the probe still runs so the message can
    say where the card actually is. Being told "configured D:\\DCIM is not
    there; a card looks to be at E:\\DCIM" is the difference between a fix and
    an afternoon.
    """
    probe = list(candidate_volumes() if volumes is None else volumes)
    discovered = tuple(_probe(probe, settings.card_folder_glob))

    configured = settings.card_dcim_root
    if configured is not None:
        return _from_configured(configured, settings.card_folder_glob, discovered)

    if discovered:
        first, rest = discovered[0], discovered[1:]
        folders = _image_folders(first, settings.card_folder_glob)
        return CardLocation(
            status=CardStatus.FOUND,
            dcim_root=first,
            image_folders=folders,
            elsewhere=rest,
            message=(
                f"Card found at {first} "
                f"({len(folders)} image folder{'' if len(folders) == 1 else 's'}: "
                f"{', '.join(f.name for f in folders)})."
                + (f" Also saw {', '.join(str(p) for p in rest)}." if rest else "")
            ),
        )

    if probe:
        return CardLocation(
            status=CardStatus.NO_PHOTOS_ON_MEDIA,
            message=(
                f"Nothing that looks like a camera card. Checked "
                f"{', '.join(str(p) for p in probe)} for "
                f"DCIM/{settings.card_folder_glob} and found none. "
                f"If the card is mounted somewhere else, set card_dcim_root."
            ),
        )

    return CardLocation(
        status=CardStatus.NO_MEDIA,
        message="No removable media attached. Insert the card.",
    )


def _from_configured(
    configured: Path, glob: str, discovered: tuple[Path, ...]
) -> CardLocation:
    elsewhere = tuple(p for p in discovered if p != configured)
    hint = (f" A card looks to be at {', '.join(str(p) for p in elsewhere)} instead."
            if elsewhere else "")

    if not configured.is_dir():
        return CardLocation(
            status=CardStatus.CONFIGURED_PATH_MISSING,
            elsewhere=elsewhere,
            message=(
                f"card_dcim_root is set to {configured}, which is not there."
                + (hint or " No removable media is attached either.")
            ),
        )

    folders = _image_folders(configured, glob)
    if not folders:
        return CardLocation(
            status=CardStatus.CONFIGURED_PATH_EMPTY,
            dcim_root=configured,
            elsewhere=elsewhere,
            message=(
                f"{configured} exists but holds no folder matching {glob}. "
                f"The card may be empty, or the folder pattern may be wrong."
                + hint
            ),
        )

    return CardLocation(
        status=CardStatus.FOUND,
        dcim_root=configured,
        image_folders=folders,
        elsewhere=elsewhere,
        message=(
            f"Card found at {configured} "
            f"({len(folders)} image folder{'' if len(folders) == 1 else 's'}: "
            f"{', '.join(f.name for f in folders)})."
        ),
    )


def _probe(volumes: Iterable[Path], glob: str) -> list[Path]:
    """DCIM roots on the given volumes that hold at least one image folder."""
    found: list[Path] = []
    for volume in volumes:
        dcim = volume / "DCIM"
        try:
            if dcim.is_dir() and _image_folders(dcim, glob):
                found.append(dcim)
        except OSError:
            # An unreadable or just-yanked volume is not an error worth
            # raising; it simply is not the card.
            continue
    return found


def _image_folders(dcim_root: Path, glob: str) -> tuple[Path, ...]:
    try:
        return tuple(sorted(p for p in dcim_root.glob(glob) if p.is_dir()))
    except OSError:
        return ()


# ----------------------------------------------------------------------
# Reading it
# ----------------------------------------------------------------------

_JPEG_SUFFIXES = {".jpg", ".jpeg"}
_RAW_SUFFIXES = {".raf"}


def scan_card(
    settings: Settings,
    *,
    catalog: Catalog | None = None,
    location: CardLocation | None = None,
) -> CardScan:
    """Enumerate and pair the card, without reading a single image byte.

    Suffixes decide what a file is here, unlike the library migration, which
    sniffs magic bytes (FR-1.3a). The reason is that the two populations differ:
    the library contains files a human renamed, where a suffix proves nothing,
    while every file on a card was written by the camera moments earlier. The
    cost of being strict about it is 2,268 file opens over a 25 MB/s reader
    before the grid can paint, which buys nothing real. Anything that is
    neither suffix is reported in :attr:`CardScan.ignored` rather than assumed
    away, and the import in WU-2.4 reads and hashes every byte regardless.
    """
    location = location or detect_card(settings)
    if not location.found:
        return CardScan(location=location)

    imported_names = _imported_names(catalog)

    photos: list[CardPhoto] = []
    ignored: list[Path] = []
    for folder in location.image_folders:
        grouped, folder_ignored = _group(folder)
        ignored.extend(folder_ignored)
        for stem in sorted(grouped):
            photos.append(_photo(stem, folder, grouped[stem], imported_names))

    return CardScan(
        location=location,
        photos=tuple(photos),
        ignored=tuple(ignored),
    )


def _imported_names(catalog: Catalog | None) -> dict[str, int]:
    """Original filename to photo id, for the cheap dedup tier.

    Filename rather than content hash because a hash costs 14 MB of card reads
    per frame and this costs nothing. It is evidence, not proof, and
    :func:`confirm_imports` is what turns it into proof.
    """
    if catalog is None:
        return {}
    return {
        name.casefold(): photo_id
        for photo_id, name in catalog.photos.original_names().items()
    }


def _group(folder: Path) -> tuple[dict[str, dict[str, _Half]], list[Path]]:
    """Enumerate one image folder, keeping the size the directory already knew.

    ``os.scandir`` rather than ``Path.iterdir``: on Windows the directory
    enumeration already carries each entry's size, so ``entry.stat()`` answers
    from that instead of reopening the file. Measured on the real card, that is
    the difference between **2.87 s and 0.06 s** for 2,268 entries — the whole
    budget for painting the grid, spent on data the OS had already handed us.
    """
    grouped: dict[str, dict[str, _Half]] = defaultdict(dict)
    ignored: list[Path] = []
    with os.scandir(folder) as entries:
        listing = sorted(entries, key=lambda e: e.name.casefold())
        for entry in listing:
            if not entry.is_file():
                continue
            path = Path(entry.path)
            suffix = path.suffix.casefold()
            half = _Half(path, _entry_size(entry))
            if suffix in _JPEG_SUFFIXES:
                grouped[path.stem.casefold()]["jpeg"] = half
            elif suffix in _RAW_SUFFIXES:
                grouped[path.stem.casefold()]["raw"] = half
            else:
                ignored.append(path)
    return grouped, ignored


@dataclass(frozen=True, slots=True)
class _Half:
    path: Path
    size: int


def _entry_size(entry: os.DirEntry[str]) -> int:
    try:
        return entry.stat().st_size
    except OSError:
        return 0


def _photo(
    stem: str, folder: Path, halves: dict[str, _Half], imported_names: dict[str, int]
) -> CardPhoto:
    jpeg = halves.get("jpeg")
    raw = halves.get("raw")
    primary = jpeg or raw
    assert primary is not None

    matched = imported_names.get(primary.path.name.casefold())
    return CardPhoto(
        stem=stem,
        folder=folder,
        jpeg=None if jpeg is None else jpeg.path,
        raw=None if raw is None else raw.path,
        jpeg_bytes=0 if jpeg is None else jpeg.size,
        raw_bytes=0 if raw is None else raw.size,
        dedup=Dedup.NEW if matched is None else Dedup.LIKELY_IMPORTED,
        matched_photo_id=matched,
    )


def confirm_imports(
    scan: CardScan,
    catalog: Catalog,
    *,
    progress: Callable[[int, int, CardPhoto], None] | None = None,
) -> CardScan:
    """Hash the cheap tier's candidates and settle each one.

    Only ``LIKELY_IMPORTED`` photos are read, so the cost is proportional to
    how much of the card is already in the library rather than to the card.
    Slow enough on this reader — about half a second per frame — that callers
    should run it in the background; fast enough that it is finished long
    before anyone reaches the import step, which is the only place the answer
    has to be certain.
    """
    candidates = [p for p in scan.photos if p.needs_confirming]
    if not candidates:
        return scan

    known = catalog.photos.hashes()
    settled: dict[int, CardPhoto] = {}
    for done, photo in enumerate(candidates, start=1):
        if progress is not None:
            progress(done, len(candidates), photo)
        try:
            digest = content_hash(photo.primary)
        except OSError:
            # An unreadable frame is not a duplicate; leave it to be culled and
            # let the import raise about it properly (FR-1.3c).
            settled[id(photo)] = _with(photo, dedup=Dedup.NOT_IMPORTED)
            continue
        settled[id(photo)] = _with(
            photo,
            dedup=Dedup.IMPORTED if digest in known else Dedup.NOT_IMPORTED,
            content_hash=digest,
        )

    return CardScan(
        location=scan.location,
        photos=tuple(settled.get(id(p), p) for p in scan.photos),
        ignored=scan.ignored,
    )


def _with(photo: CardPhoto, **changes: object) -> CardPhoto:
    values: dict[str, object] = {
        "stem": photo.stem,
        "folder": photo.folder,
        "jpeg": photo.jpeg,
        "raw": photo.raw,
        "jpeg_bytes": photo.jpeg_bytes,
        "raw_bytes": photo.raw_bytes,
        "dedup": photo.dedup,
        "matched_photo_id": photo.matched_photo_id,
        "content_hash": photo.content_hash,
    }
    values.update(changes)
    return CardPhoto(**values)  # type: ignore[arg-type]


def summarise(scan: CardScan) -> str:
    """One paragraph for a terminal. ASCII, because a Windows console is
    cp1252 more often than not."""
    if not scan.location.found:
        return f"[{scan.location.status}] {scan.location.message}"

    counts = scan.dedup_counts()
    flagged = ", ".join(f"{count} {state}" for state, count in counts.items())
    return (
        f"{scan.location.message}\n"
        f"{scan.photo_count} photographs | {scan.pair_count} RAW pairs | "
        f"{len(scan.unpaired)} unpaired | {scan.total_bytes / 1e9:.2f} GB "
        f"({scan.jpeg_bytes / 1e9:.2f} GB of JPEG)\n"
        f"Dedup: {flagged}"
        + (f"\nIgnored: {len(scan.ignored)} non-image files" if scan.ignored else "")
    )
