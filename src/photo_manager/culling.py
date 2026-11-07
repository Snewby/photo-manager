"""Cull decisions: what they are, where they live, and what they add up to.

Culling is the one part of this app where the user does a thousand pieces of work by
hand, so losing that work is the failure that matters (FR-4.4). This module is
the part that makes it survivable. The UI is WU-2.3's other half; this is the
half that is still true after the process is killed.

**Decisions are not in the catalog, deliberately.** The catalog is an index of
the *library*, rebuildable by rescanning it and its sidecars (NFR-2) — that
rebuildability is the safety net under the whole design, and the recovery path
for a bad schema migration. A cull decision is about a photograph that is not
in the library and may never be; nothing in the library could reconstruct it.
Putting it in the catalog would make "delete the database and rebuild" quietly
lossy, and would need migration 2 before the first real import. So decisions
live beside the catalog in ``state/cull/``, in their own file per card.

**One line per decision, appended and flushed as it is made.** Same shape as
the reversal log in FR-8.4, for the same reason: a document written at the end
is empty exactly when it is most needed. Replaying the file gives the current
state, last line winning per photo — which is also what makes FR-4.3's
revisitability free, since changing your mind is another line rather than a
rewrite.

**The identity problem, and the thing that protects a photograph.** A card
photo has no cheap identity: its content hash costs a 14 MB read over a
20-37 MB/s reader (C-4), and culling cannot pay that for every frame before it
starts. So a decision is keyed by **(image folder, filename, size in bytes)**,
inside a file named for the card's **volume serial number**.

The dangerous failure is a *stale* decision landing on a different photograph.
The camera's frame counter rolls over, so ``DSCF0007.JPG`` comes round again —
and a stale ``skip`` would mean a photograph the user meant to keep is quietly
never imported, with the UI showing it as already dealt with. Two things stand
in the way:

* **The volume serial.** Formatting a card, in camera or on a PC, writes a new
  filesystem and a new serial. A rolled-over counter therefore always arrives
  with a serial that has no decision file, so the whole card starts fresh.
* **The recorded size.** Within one card, a record whose size no longer matches
  the file on disk is treated as **stale and reported**, not silently applied
  and not silently dropped. A count the user can see beats a guess they cannot.

Neither is a hash, and neither is claimed to be. What they are is the same
asymmetry FR-1.4 uses: cheap evidence is allowed to make the UI wrong in a way
the user can see and undo, never in a way that loses a frame without saying so.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import string
import struct
import sys
import uuid
from collections.abc import Container, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .cards import CardPhoto, CardScan
from .config import Settings
from .macos import diskutil_info as _diskutil_info

__all__ = [
    "Verdict",
    "Decision",
    "PhotoKey",
    "CullStore",
    "Resume",
    "Tally",
    "card_key",
    "known_serials",
    "volume_serial",
    "tally",
    "FREE_SPACE_MARGIN",
]

#: How much of the library volume an import must leave behind. Not a capacity
#: figure — C-2 forbids those and this is measured against whatever is actually
#: free at the time — but a volume filled to literally zero bytes breaks every
#: other thing on the machine, so the warning fires before that rather than at
#: it.
FREE_SPACE_MARGIN = 1 << 30  # 1 GiB


class Verdict:
    """What has been decided about one frame. Strings, so they survive JSON."""

    #: Not looked at yet, or explicitly cleared. The resume point.
    UNDECIDED = "undecided"
    #: Import this one to the Mac.
    KEEP = "keep"
    #: **Reject**: do not import, and it is fine to lose when the card is
    #: formatted. Spelled "skip" because that is what every decision file
    #: already on disk says. Note that *nothing is deleted* — §2 forbids any
    #: destructive card operation — so a reject is "do not copy", not "discard".
    SKIP = "skip"
    #: Do not import, but **keep it on the card**: formatting the card would
    #: lose it, so the card is not safe to format while any frame says this.
    LEAVE = "leave"

    ALL = (UNDECIDED, KEEP, SKIP, LEAVE)


@dataclass(frozen=True, slots=True)
class Decision:
    """One frame's verdict, rating and RAW flag."""

    verdict: str = Verdict.UNDECIDED
    #: 0-5 (FR-4.2, FR-5.5). Only meaningful on a keeper.
    rating: int = 0
    #: Pull the ``.RAF`` too (D-2). Opt-in per photo, always.
    keep_raw: bool = False
    decided_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        if self.verdict not in Verdict.ALL:
            raise ValueError(
                f"verdict must be one of {', '.join(Verdict.ALL)}, got {self.verdict!r}"
            )
        if not 0 <= self.rating <= 5:
            raise ValueError(f"rating must be 0-5, got {self.rating}")

    @property
    def decided(self) -> bool:
        return self.verdict != Verdict.UNDECIDED

    @property
    def keeps(self) -> bool:
        return self.verdict == Verdict.KEEP


@dataclass(frozen=True, slots=True)
class PhotoKey:
    """How a decision finds its photograph again.

    Not a hash, and not a path. See the module docstring for why it is these
    three fields and what stops a stale one being applied.
    """

    folder: str
    name: str
    size: int

    @classmethod
    def of(cls, photo: CardPhoto) -> PhotoKey:
        return cls(
            folder=photo.folder.name,
            name=photo.primary.name,
            size=photo.jpeg_bytes or photo.raw_bytes,
        )

    @property
    def loose(self) -> tuple[str, str]:
        """Folder and name, without the size — for spotting a *stale* record
        rather than an absent one."""
        return self.folder.casefold(), self.name.casefold()


# ----------------------------------------------------------------------
# Which card is this?
# ----------------------------------------------------------------------


def volume_serial(path: str | Path, *, known: Iterable[str] = ()) -> str | None:
    """The volume serial number of the volume *path* sits on, as hex.

    Returns None anywhere it cannot be determined, and :func:`card_key` falls
    back rather than failing — a weaker key is better than refusing to
    remember anything.

    On macOS the serial is never reported, only a UUID derived from it — see
    :func:`_mac_volume_identity`, which is why this takes the serials already
    *known*: one of those can be proved, where an unknown one cannot be read.
    """
    if sys.platform == "darwin":
        return _mac_volume_identity(path, known)
    if sys.platform != "win32":
        return None
    import ctypes

    root = Path(path).resolve().anchor
    if not root:
        return None
    serial = ctypes.c_ulong(0)
    try:
        ok = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
            ctypes.c_wchar_p(root), None, 0,
            ctypes.byref(serial), None, None, None, 0,
        )
    except OSError:
        return None
    return f"{serial.value:08x}" if ok else None


#: The namespace macOS's msdosfs and exfat derive a volume's UUID in.
_APPLE_VOLUME_NAMESPACE = uuid.UUID("b3e20f39-f292-11d6-97a4-00306543ecac")

#: How far below the partition's size a FAT boot sector's sector count is
#: looked for. A formatter may leave a few sectors unused (64 MiB FAT32: two).
_FAT_SECTOR_SLACK = 1 << 16


def _mac_volume_identity(path: str | Path, known: Iterable[str]) -> str | None:
    """A FAT or exFAT card's identity on macOS: its serial if provable, else its UUID.

    macOS never reports a FAT or exFAT volume serial. It reports ``VolumeUUID``,
    a version-3 UUID it derives from the serial — MD5 over a fixed namespace and
    the boot sector's four serial bytes, plus for FAT the sector count — so the
    serial cannot be read back out, and the raw device that holds it needs root.
    Checked on 2026-11-02 against FAT32 and exFAT images formatted on macOS,
    whose boot sectors could be read directly.

    What *can* be done is to check a serial already known. Every card culled on
    Windows is known by its serial — it is in the decision file's name — so such
    a card comes back as the same eight digits and finds its decisions. A card
    known by nothing is named by the UUID itself, which changes on a reformat
    exactly as the serial does, and that is the property §5.3 rests on.

    Anything that is not FAT or exFAT, or that ``diskutil`` cannot answer for,
    is None: the path fallback, as before.
    """
    info = _diskutil_info(path)
    if not info or info.get("FilesystemType") not in ("msdos", "exfat"):
        return None
    try:
        volume_uuid = uuid.UUID(str(info.get("VolumeUUID")))
    except ValueError:
        return None
    try:
        sectors = int(info.get("Size", 0)) // int(info.get("DeviceBlockSize") or 512)
    except (TypeError, ValueError):
        sectors = 0
    for serial in known:
        if _proves(serial, volume_uuid, info["FilesystemType"], sectors):
            return serial.casefold()
    return str(volume_uuid)


def _proves(serial: str, volume_uuid: uuid.UUID, kind: str, sectors: int) -> bool:
    """Whether macOS would have derived *volume_uuid* from *serial*."""
    try:
        packed = struct.pack("<I", int(serial, 16))
    except (ValueError, struct.error):
        return False
    if kind == "exfat":
        return uuid.uuid3(_APPLE_VOLUME_NAMESPACE, packed) == volume_uuid
    # FAT hashes the boot sector's total-sector count too, which nothing
    # unprivileged reports — but it is the partition's size or a little under.
    return any(
        uuid.uuid3(_APPLE_VOLUME_NAMESPACE, packed + struct.pack("<I", total)) == volume_uuid
        for total in range(sectors, max(sectors - _FAT_SECTOR_SLACK, 0), -1)
    )


def known_serials(settings: Settings) -> tuple[str, ...]:
    """The serials of every card with a decision file — what macOS can check a
    card against, because it cannot read one (:func:`_mac_volume_identity`)."""
    names = (path.name for path in (settings.state_dir / "cull").glob("volume-*.jsonl"))
    return tuple(sorted(
        match.group(1) for name in names
        if (match := re.fullmatch(r"volume-([0-9a-f]{8})\.jsonl", name))
    ))


def card_key(
    dcim_root: str | Path, *, serial: str | None = None, known: Iterable[str] = ()
) -> str:
    """A filename-safe identity for the card at *dcim_root*.

    The volume serial where there is one, because it survives the card moving
    between readers and drive letters and — crucially — *changes* when the card
    is reformatted, which is the event that makes old decisions dangerous.

    Where there isn't one, this falls back to the path, which is weaker in
    exactly the way WU-2.1 spent a unit fixing: the same card on a different
    letter reads as a different card and starts again. That is the safe
    direction to be wrong in (work repeated, never work misapplied), and the
    name says which one you got.

    On macOS a card arrives as its eight-digit serial when that serial is one of
    *known* (pass :func:`known_serials`), and otherwise as its volume UUID.
    """
    resolved = serial if serial is not None else volume_serial(dcim_root, known=known)
    if resolved:
        return f"volume-{resolved}"
    safe = "".join(
        character if character in _SAFE_CHARACTERS else "-"
        for character in str(Path(dcim_root)).casefold()
    )
    return f"path-{safe.strip('-')}"


_SAFE_CHARACTERS = set(string.ascii_lowercase + string.digits + "-_")


# ----------------------------------------------------------------------
# The store
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Resume:
    """What a stored file had to say about the card actually in the reader."""

    #: Decisions by index into ``scan.photos``.
    decisions: dict[int, Decision]
    #: Records whose folder and filename matched but whose size did not. The
    #: dangerous ones, so they are counted and named rather than dropped quietly.
    stale: tuple[str, ...] = ()
    #: Records naming a file that is not on this card at all. Ordinary — frames
    #: get deleted in camera — and not the same thing as stale.
    absent: int = 0
    #: Lines that were not valid JSON. A truncated final line is the expected
    #: shape of this after a hard kill, and it costs one decision, not the file.
    unreadable: int = 0

    @property
    def resumed(self) -> int:
        return len(self.decisions)


class CullStore:
    """Cull decisions for one card, on disk.

    Opened per card rather than per session, so closing the app and reopening
    it lands on the same file (FR-4.4).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @classmethod
    def for_card(cls, settings: Settings, key: str) -> CullStore:
        return cls(settings.state_dir / "cull" / f"{key}.jsonl")

    # -- writing -------------------------------------------------------

    def record(self, key: PhotoKey, decision: Decision) -> None:
        """Append one decision and get it onto the disk before returning.

        Flushed *and* fsynced per line. A keypress is cheap and this is the
        work the user cannot be asked to redo; the whole point of the file is
        to be complete at an arbitrary moment, not at a tidy one.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "folder": key.folder,
            "name": key.name,
            "size": key.size,
            "verdict": decision.verdict,
            "rating": decision.rating,
            "keep_raw": decision.keep_raw,
            "at": (decision.decided_at or dt.datetime.now()).isoformat(timespec="seconds"),
        }, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    # -- reading -------------------------------------------------------

    def replay(self) -> tuple[dict[PhotoKey, Decision], int]:
        """Every decision the file holds, last line winning, plus a bad-line count."""
        decisions: dict[PhotoKey, Decision] = {}
        unreadable = 0
        if not self.path.is_file():
            return decisions, unreadable

        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                key = PhotoKey(
                    folder=str(record["folder"]),
                    name=str(record["name"]),
                    size=int(record["size"]),
                )
                decisions[key] = Decision(
                    verdict=str(record["verdict"]),
                    rating=int(record.get("rating", 0)),
                    keep_raw=bool(record.get("keep_raw", False)),
                    decided_at=_parse_time(record.get("at")),
                )
            except (ValueError, TypeError, KeyError):
                # A half-written final line is what a kill mid-write looks
                # like. It costs that one decision and nothing else.
                unreadable += 1
        return decisions, unreadable

    def resume(self, scan: CardScan) -> Resume:
        """Match what is stored against what is actually in the reader."""
        stored, unreadable = self.replay()
        by_loose: dict[tuple[str, str], list[PhotoKey]] = {}
        for key in stored:
            by_loose.setdefault(key.loose, []).append(key)

        decisions: dict[int, Decision] = {}
        matched: set[PhotoKey] = set()
        stale: list[str] = []

        for index, photo in enumerate(scan.photos):
            key = PhotoKey.of(photo)
            decision = stored.get(key)
            if decision is not None:
                decisions[index] = decision
                matched.add(key)
                continue
            # Same folder and filename, different size: this is not that
            # photograph. Say so rather than applying it or hiding it.
            for candidate in by_loose.get(key.loose, ()):
                stale.append(photo.primary.name)
                matched.add(candidate)

        absent = len(set(stored) - matched)
        return Resume(
            decisions=decisions,
            stale=tuple(sorted(set(stale))),
            absent=absent,
            unreadable=unreadable,
        )


def _parse_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


# ----------------------------------------------------------------------
# What it all adds up to (FR-4.5)
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tally:
    """Keepers, what they will cost, and whether the volume can take them."""

    keepers: int
    #: Rejected: fine to lose when the card is formatted.
    skipped: int
    undecided: int
    #: How many of the keepers are also pulling their RAF (D-2).
    raw_keepers: int
    #: Bytes the keepers' JPEGs will occupy.
    jpeg_bytes: int
    #: Bytes the RAFs of RAW-flagged keepers will occupy (D-2).
    raw_bytes: int
    #: Free space on the library volume, **measured now** (C-2).
    free_bytes: int
    #: Chosen to stay on the card: not imported, and not to be lost either.
    left: int = 0

    @property
    def total_bytes(self) -> int:
        return self.jpeg_bytes + self.raw_bytes

    @property
    def headroom_bytes(self) -> int:
        """What would be left, keeping the margin in hand."""
        return self.free_bytes - self.total_bytes - FREE_SPACE_MARGIN

    @property
    def over_capacity(self) -> bool:
        """True when importing these keepers would not leave the margin free."""
        return self.headroom_bytes < 0


def tally(
    photos: Iterable[CardPhoto],
    decisions: Mapping[int, Decision],
    settings: Settings,
    *,
    offered: Container[int] | None = None,
    free_bytes: int | None = None,
) -> Tally:
    """Add up the decisions against the space actually free right now.

    *offered* is the set of indices actually being culled, and leaving it out
    means all of them. It matters for one number: an already-imported frame is
    on the card but is deliberately not something anyone will decide about
    (C-3), so counting it as *undecided* would tell the user a thousand frames are
    left when the real answer is 925 — and that number is the one they are
    working down.

    *free_bytes* is measured on the configured library volume unless it is
    passed in. Nothing here knows how big any disk is: C-2 says storage is
    being expanded separately, so a hardcoded capacity would be wrong within
    the month and wrong silently.
    """
    keepers = skipped = left = undecided = raw_keepers = 0
    jpeg_bytes = raw_bytes = 0

    for index, photo in enumerate(photos):
        if offered is not None and index not in offered:
            continue
        decision = decisions.get(index, _UNDECIDED)
        if decision.verdict == Verdict.SKIP:
            skipped += 1
            continue
        if decision.verdict == Verdict.LEAVE:
            left += 1
            continue
        if not decision.decided:
            undecided += 1
            continue
        keepers += 1
        jpeg_bytes += photo.jpeg_bytes or photo.raw_bytes
        if decision.keep_raw and photo.has_raw:
            raw_keepers += 1
            raw_bytes += photo.raw_bytes

    if free_bytes is None:
        free_bytes = shutil.disk_usage(settings.library_root).free

    return Tally(
        keepers=keepers,
        skipped=skipped,
        undecided=undecided,
        raw_keepers=raw_keepers,
        jpeg_bytes=jpeg_bytes,
        raw_bytes=raw_bytes,
        free_bytes=free_bytes,
        left=left,
    )


_UNDECIDED = Decision()
