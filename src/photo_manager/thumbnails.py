"""The two-tier thumbnail strategy (REQUIREMENTS.md §6.2).

This is the unit the primary use case stands or falls on. Naively decoding
a thousand 14 MB JPEGs to fill a contact sheet means reading ~16 GB over a card
reader — minutes of staring at an empty grid before the first frame appears.
So there are two tiers:

**Tier 1 — the embedded EXIF thumbnail** (FR-2.1). Every camera-written JPEG
carries a 160 x 120 JPEG of itself inside its APP1 segment. That segment is
capped at 64 KB by the JPEG format itself and sits at the very front of the
file, so extracting it reads the header and nothing else: ~72 MB across a whole
card instead of 16 GB. This module walks the JPEG marker chain over a bounded
window and never reads past it — which is the entire basis of the claim in
NFR-1, and is asserted by measuring bytes read rather than by assertion.

**Tier 2 — a generated ~400 px thumbnail** (FR-2.2), made with Pillow's
``draft()``, which scales in the DCT domain during decode and so never
materialises 26 megapixels. Cached on disk by content hash (FR-2.3) and
generated on background threads (FR-2.4) so the grid keeps painting.

**Not every file has a tier 1** (FR-2.1a). Files that have been through other
software lose the embedded thumbnail — three in the existing library have none.
Absence returns ``None`` and the caller falls through to tier 2. It is never an
error and never a hole in the grid.
"""

from __future__ import annotations

import os
import struct
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError

from .config import Settings
from .hashing import hash_stream
from .metadata import RAF_MAGIC, MetadataError, raf_preview_offset

__all__ = [
    "ThumbnailError",
    "ThumbnailCache",
    "ThumbnailWorker",
    "embedded_thumbnail",
    "embedded_thumbnail_from",
    "generate_thumbnail",
    "render_and_hash",
    "DEFAULT_HEADER_WINDOW",
]

#: How much of a file tier 1 is allowed to read. A JPEG APP1 segment cannot
#: exceed 65,533 bytes — the format stores its length in 16 bits — so this is
#: roughly double the largest possible EXIF block, leaving room for a JFIF or
#: ICC segment to sit in front of it. On a 14 MB frame it is 0.9% of the file.
DEFAULT_HEADER_WINDOW = 128 * 1024

#: Quality for generated thumbnails. These are contact-sheet tiles, not
#: deliverables; the originals are untouched either way (§4.1).
JPEG_QUALITY = 85

_SOI = b"\xff\xd8"
_MARKER_APP1 = 0xE1
_MARKER_SOS = 0xDA
_EXIF_PREFIX = b"Exif\x00\x00"
_TAG_THUMBNAIL_OFFSET = 0x0201
_TAG_THUMBNAIL_LENGTH = 0x0202
#: The second IFD, which is where a JPEG keeps its embedded thumbnail.
#: Taken from Pillow's own enum rather than written out — its value is -1,
#: not 1, and guessing it is a silent 'no thumbnail anywhere' bug.
_IFD1 = ExifTags.IFD.IFD1


class ThumbnailError(Exception):
    """A thumbnail could not be produced for a file."""


# ----------------------------------------------------------------------
# Tier 1 — the embedded EXIF thumbnail
# ----------------------------------------------------------------------


def embedded_thumbnail(
    path: str | Path, *, window: int = DEFAULT_HEADER_WINDOW
) -> bytes | None:
    """The JPEG thumbnail the camera embedded, or None if there isn't one.

    Reads at most *window* bytes from the front of the file. Returns the
    thumbnail's raw JPEG bytes, ready to serve straight to a browser without
    re-encoding.
    """
    with Path(path).open("rb") as stream:
        return embedded_thumbnail_from(stream, window=window)


def embedded_thumbnail_from(
    stream: BinaryIO, *, window: int = DEFAULT_HEADER_WINDOW
) -> bytes | None:
    """As :func:`embedded_thumbnail`, from an already-open stream."""
    head = stream.read(len(RAF_MAGIC))
    stream.seek(0)

    if head == RAF_MAGIC:
        # A RAF's preview is a whole JPEG with its own embedded thumbnail, so
        # tier 1 stays a header read here too: the RAF header, then the front
        # of the preview. The 5 MB preview body is never touched.
        try:
            _, offset, _ = raf_preview_offset(stream)
        except MetadataError:
            return None
        stream.seek(offset)

    return _thumbnail_from_jpeg_header(stream.read(window))


def _thumbnail_from_jpeg_header(head: bytes) -> bytes | None:
    """Pull the IFD1 thumbnail out of a JPEG's header bytes."""
    payload = _find_app1(head)
    if payload is None:
        return None

    exif = Image.Exif()
    try:
        exif.load(payload)
        ifd1 = exif.get_ifd(_IFD1)
    except (OSError, ValueError, SyntaxError, struct.error, KeyError):
        return None

    offset = ifd1.get(_TAG_THUMBNAIL_OFFSET)
    length = ifd1.get(_TAG_THUMBNAIL_LENGTH)
    if not isinstance(offset, int) or not isinstance(length, int) or length <= 0:
        return None

    # Thumbnail offsets are relative to the start of the TIFF header, which is
    # immediately after the 'Exif\0\0' prefix.
    tiff = payload[len(_EXIF_PREFIX):]
    blob = tiff[offset:offset + length]
    if len(blob) != length or not blob.startswith(_SOI):
        # Truncated, or the tag points somewhere that isn't a JPEG. Fall
        # through to tier 2 rather than serving nonsense (FR-2.1a).
        return None
    return blob


def _find_app1(head: bytes) -> bytes | None:
    """Walk the JPEG marker chain for the Exif APP1 segment.

    Stops at start-of-scan: past that point there is only entropy-coded image
    data, and continuing would mean reading the whole file.
    """
    if not head.startswith(_SOI):
        return None
    position = 2
    while position + 4 <= len(head):
        if head[position] != 0xFF:
            return None
        marker = head[position + 1]
        if marker == 0xFF:  # fill byte, allowed between markers
            position += 1
            continue
        if marker == _MARKER_SOS:
            return None
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            position += 2  # standalone marker, no length
            continue
        (length,) = struct.unpack(">H", head[position + 2:position + 4])
        if length < 2:
            return None
        payload = head[position + 4:position + 2 + length]
        if marker == _MARKER_APP1 and payload.startswith(_EXIF_PREFIX):
            if len(payload) < length - 2:
                return None  # segment runs past our window
            return payload
        position += 2 + length
    return None


# ----------------------------------------------------------------------
# Tier 2 — a generated thumbnail
# ----------------------------------------------------------------------


def generate_thumbnail(path: str | Path, *, max_px: int) -> bytes:
    """A ~*max_px* JPEG thumbnail, as bytes.

    Uses ``draft()``, which tells the JPEG decoder to downscale by a power of
    two while decoding. A 6240 x 4160 frame is decoded at 780 x 520 and never
    exists in memory at full size — the difference between this being viable
    for a thousand frames and not.

    Raises :class:`ThumbnailError` for anything unreadable, so one bad file
    leaves a gap of one rather than failing a whole card.
    """
    if max_px <= 0:
        raise ValueError(f"max_px must be positive, got {max_px}")

    path = Path(path)
    try:
        source: Path | BytesIO = path
        with path.open("rb") as probe:
            if probe.read(len(RAF_MAGIC)) == RAF_MAGIC:
                probe.seek(0)
                _, offset, length = raf_preview_offset(probe)
                probe.seek(offset)
                source = BytesIO(probe.read(length))
        return _encode(source, max_px)
    except MetadataError as exc:
        raise ThumbnailError(f"{path}: {exc}") from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise ThumbnailError(
            f"{path}: cannot generate a thumbnail ({type(exc).__name__}: {exc})"
        ) from exc


def render_and_hash(path: str | Path, *, max_px: int) -> tuple[bytes, str]:
    """A thumbnail **and** the file's content hash, from a single read.

    For a photo already in the library this would be pointless — the catalog
    holds the hash. It exists for the card, where the cache key (FR-2.3) is a
    hash nobody has paid for yet, and where paying for it separately means
    reading 14 MB twice over a 20-37 MB/s reader (C-4). Reading once and
    answering both questions halves the only cost that matters on the
    interactive path.

    The whole file is held in memory for the duration, which is the trade: ~15
    MB per frame in flight, against ~0.5 s of card reads saved per frame.

    Raises :class:`ThumbnailError` exactly as :func:`generate_thumbnail` does,
    so one bad frame is still one gap.
    """
    if max_px <= 0:
        raise ValueError(f"max_px must be positive, got {max_px}")

    path = Path(path)
    try:
        data = path.read_bytes()
        digest = hash_stream(BytesIO(data))

        source: Path | BytesIO = BytesIO(data)
        if data.startswith(RAF_MAGIC):
            probe = BytesIO(data)
            _, offset, length = raf_preview_offset(probe)
            source = BytesIO(data[offset:offset + length])
        return _encode(source, max_px), digest
    except MetadataError as exc:
        raise ThumbnailError(f"{path}: {exc}") from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise ThumbnailError(
            f"{path}: cannot generate a thumbnail ({type(exc).__name__}: {exc})"
        ) from exc


def _encode(source: Path | BytesIO, max_px: int) -> bytes:
    """Decode as small as the format allows, orient, fit, and re-encode."""
    with Image.open(source) as image:
        # draft() must come before anything that decodes, and it must be asked
        # for the *actual* target box rather than a square one. It accepts a
        # power-of-two reduction only while both dimensions stay at or above
        # what was requested, so asking for (max_px, max_px) on a 3:2 frame
        # measures the reduction against the short edge and settles for one
        # step less than it could. Measured on a real 6240 x 4160 frame at
        # 1560 px: 956 ms for the square request against **453 ms** for this
        # one, for a pixel-identical result — the square request decodes
        # 3120 x 2080 and resamples down, this one decodes 1560 x 1040 and is
        # already there.
        image.draft("RGB", _target_box(image.size, max_px))
        oriented = ImageOps.exif_transpose(image) or image
        oriented.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
        if oriented.mode != "RGB":
            oriented = oriented.convert("RGB")
        buffer = BytesIO()
        oriented.save(buffer, "JPEG", quality=JPEG_QUALITY, optimize=True)
        return buffer.getvalue()


def _target_box(size: tuple[int, int], max_px: int) -> tuple[int, int]:
    """The size ``thumbnail(max_px)`` will produce, so ``draft`` can aim at it."""
    width, height = size
    longest = max(width, height)
    if longest <= max_px:
        return width, height
    scale = max_px / longest
    return max(1, round(width * scale)), max(1, round(height * scale))


# ----------------------------------------------------------------------
# The disk cache
# ----------------------------------------------------------------------


class ThumbnailCache:
    """Generated thumbnails on disk, keyed by content hash (FR-2.3).

    Keying by hash rather than by path is what lets the card view and the
    library view share one cache: the same photo is the same bytes whether it
    is still on the card or already imported, so it is thumbnailed once and
    the second view is free.

    Tier 1 is deliberately *not* cached. Extracting it is a 128 KB read that
    costs less than the round trip to find out whether a cache entry exists.
    """

    def __init__(self, cache_dir: str | Path, *, max_px: int = 400) -> None:
        if max_px <= 0:
            raise ValueError(f"max_px must be positive, got {max_px}")
        self.cache_dir = Path(cache_dir)
        self.max_px = max_px

    @classmethod
    def from_settings(cls, settings: Settings) -> ThumbnailCache:
        assert settings.thumbnail_cache_dir is not None  # filled in by Settings
        return cls(settings.thumbnail_cache_dir, max_px=settings.thumbnail_max_px)

    def path_for(self, content_hash: str) -> Path:
        """Where a thumbnail lives. Sharded, and stamped with its size.

        The size is in the filename because ``thumbnail_max_px`` is a setting:
        raising it must regenerate, not silently keep serving smaller tiles
        from before the change.
        """
        if not content_hash:
            raise ValueError("content_hash must not be empty")
        return self.cache_dir / content_hash[:2] / f"{content_hash}-{self.max_px}.jpg"

    def cached(self, content_hash: str) -> Path | None:
        """The cached thumbnail, or None. Never generates."""
        path = self.path_for(content_hash)
        return path if path.is_file() else None

    def ensure(self, source: str | Path, content_hash: str) -> Path:
        """Return the cached thumbnail, generating it first if it is missing."""
        existing = self.cached(content_hash)
        if existing is not None:
            return existing
        return self.store(content_hash, generate_thumbnail(source, max_px=self.max_px))

    def store(self, content_hash: str, data: bytes) -> Path:
        """Write bytes into the cache atomically.

        Via a temporary file and ``os.replace`` so that a crash or two workers
        racing on the same photo can never leave a half-written thumbnail that
        every later run would happily serve.

        The temporary name carries the **thread** id as well as the process id.
        It used to carry only the process id, which was fine while the only
        caller was ``ThumbnailWorker`` — that dedups by content hash, so two of
        its threads never held the same one. WU-2.3's preview prefetch does not
        know a hash until it has read the file, so three threads really can
        arrive here with the same one; sharing a temporary name then means each
        thread's ``finally`` deletes whichever sibling got there first, and the
        losers fail with a FileNotFoundError on a thumbnail that was perfectly
        good. Found by two identical frames on one card.
        """
        destination = self.path_for(content_hash)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f"{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        except OSError:
            # Windows refuses a replace while another thread is replacing the
            # same destination, with an access-denied that says nothing about
            # what actually happened. It does not matter which writer won: the
            # cache is content-addressed, so both of them were writing the same
            # bytes under the same name. A lost race is a cache hit. Only an
            # empty destination means the failure was real.
            if not destination.is_file():
                raise
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def clear(self) -> int:
        """Delete every cached thumbnail. Returns how many went.

        Safe by construction: the cache holds only regenerable derivatives, and
        nothing here touches an original.
        """
        if not self.cache_dir.is_dir():
            return 0
        removed = 0
        for entry in self.cache_dir.rglob("*.jpg"):
            entry.unlink()
            removed += 1
        return removed


# ----------------------------------------------------------------------
# Background generation
# ----------------------------------------------------------------------


class ThumbnailWorker:
    """Generates tier 2 thumbnails off the request thread (FR-2.2, FR-2.4).

    Threads rather than processes: the work is JPEG decoding inside Pillow,
    which releases the GIL, so threads genuinely run in parallel here and cost
    nothing to hand a file path to.
    """

    def __init__(self, cache: ThumbnailCache, *, max_workers: int | None = None) -> None:
        self.cache = cache
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers or min(8, (os.cpu_count() or 4)),
            thread_name_prefix="thumbnail",
        )
        self._lock = threading.Lock()
        self._pending: dict[str, Future[Path]] = {}

    def submit(self, source: str | Path, content_hash: str) -> Future[Path]:
        """Queue a thumbnail. Returns immediately.

        Asking twice for the same photo returns the same future rather than
        decoding it twice — a grid that scrolls back over itself would
        otherwise re-queue everything it has already passed.
        """
        with self._lock:
            existing = self._pending.get(content_hash)
            if existing is not None and not existing.done():
                return existing
            future = self._executor.submit(self.cache.ensure, source, content_hash)
            self._pending[content_hash] = future
            return future

    def shutdown(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)

    def __enter__(self) -> ThumbnailWorker:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.shutdown()
