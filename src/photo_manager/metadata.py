"""Reading a photo's metadata without decoding it.

WU-1.2's EXIF reader. Two file types matter here:

* **JPEG**, which is every frame the camera writes and every file the culling
  and import paths will ever touch.
* **RAF**, Fujifilm's raw format, which Pillow cannot open at all. A RAF carries
  a full JPEG preview with the camera's complete EXIF inside it, so this module
  parses the RAF container far enough to find that preview and reads its EXIF —
  then corrects the dimensions, because the preview is smaller than the frame.

Dispatch is on the file's **magic bytes**, not its extension. The existing
library already contains ``birthday.JPG`` beside ``Birthday.RAF``,
so trusting case or suffix is asking to be wrong at the worst moment.

Three deliberate non-behaviours, each with a reason:

* **No capture-date fallback.** Only EXIF ``DateTimeOriginal`` (0x9003) counts,
  exactly as REQUIREMENTS.md §5.1 says. When it is missing this returns None and
  WU-1.4's migration applies FR-8.2a's mtime fallback and *flags the row*. Quietly
  falling back to EXIF ``DateTime`` here would file a photo under the date some
  other software last touched it, and nothing downstream would know.
* **No GPS coordinate parsing.** The camera records none (C-1) and location is a
  manual-entry problem by design (D-5). :attr:`PhotoMetadata.has_gps` reports
  whether a GPS IFD was present at all, so the assumption stays measured rather
  than assumed.
* **No orientation applied.** ``width``/``height`` are the stored pixel
  dimensions and ``orientation`` is reported alongside, matching what §5.1 asks
  for. Rotating is a display concern.
"""

from __future__ import annotations

import datetime as dt
import io
import struct
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO

from PIL import Image, UnidentifiedImageError

__all__ = [
    "DateSource",
    "resolve_capture_date",
    "PhotoMetadata",
    "MetadataError",
    "read_metadata",
    "read_metadata_from",
    "RAF_MAGIC",
    "raf_preview_offset",
]

# EXIF tag numbers. Spelled out rather than imported so the mapping to
# REQUIREMENTS.md §5.1 is readable without a second window open.
_TAG_ORIENTATION = 0x0112
_TAG_MODEL = 0x0110
_TAG_EXIF_IFD = 0x8769
_TAG_GPS_IFD = 0x8825
_TAG_DATE_TIME_ORIGINAL = 0x9003
_TAG_EXPOSURE_TIME = 0x829A
_TAG_F_NUMBER = 0x829D
_TAG_ISO = 0x8827
_TAG_FOCAL_LENGTH = 0x920A
_TAG_LENS_MODEL = 0xA434

_EXIF_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"

#: A RAF announces itself in its first 16 bytes. Public because the thumbnail
#: cache dispatches on it too.
RAF_MAGIC = b"FUJIFILMCCD-RAW "
_RAF_MAGIC = RAF_MAGIC
#: Offsets into the RAF header: where the embedded JPEG lives, and where the
#: CFA header's tagged directory lives. Fixed by the format.
_RAF_JPEG_POINTER = 0x54
_RAF_CFA_HEADER_POINTER = 0x5C
#: Tag 0x0111 in the CFA header directory is the output image size, stored as
#: (height, width). Verified against DSCF0807.RAF, which reports 4160 x 6240 —
#: the frame, not the 4416 x 2944 preview whose EXIF everything else comes from.
_RAF_TAG_IMAGE_SIZE = 0x0111


class DateSource:
    """Where a photograph's ``captured_at`` actually came from (FR-8.2a).

    A string rather than a bool, because the three cases are genuinely three
    and only one of them is a guess. Collapsing them into "inferred or not"
    was available and is what the requirement spends a paragraph rejecting.
    """

    #: The file's own EXIF ``DateTimeOriginal`` (0x9003). The real answer.
    EXIF = "exif"
    #: The paired RAF's EXIF. **Not** a guess: a pair is one frame, so this is
    #: that frame's real capture time and the inferred flag stays clear.
    PAIRED_RAW = "paired-raw"
    #: The file's modification time. The only actual guess, and the only one
    #: that sets ``captured_at_inferred``.
    MTIME = "mtime"


def resolve_capture_date(
    primary: PhotoMetadata,
    mtime: dt.datetime,
    raw: PhotoMetadata | None = None,
) -> tuple[dt.datetime, str]:
    """A photograph's capture time, and which of the three it came from.

    The rule in one place, because the library migration (FR-8.1) and the card
    import (FR-6.1) both need it and two copies would be two chances to
    disagree about the distinction FR-8.2a makes on purpose: only mtime is a
    guess, and the deliberate absence of any fallback to EXIF ``DateTime``
    (0x0132) — which would file a photograph under the date some other
    software last touched it, with nothing flagged.
    """
    if primary.captured_at is not None:
        return primary.captured_at, DateSource.EXIF
    if raw is not None and raw.captured_at is not None:
        return raw.captured_at, DateSource.PAIRED_RAW
    return mtime, DateSource.MTIME


class MetadataError(Exception):
    """A file could not be read far enough to get metadata out of it.

    Raised for truncated, corrupt or unrecognised files. A *missing* EXIF block
    is not this — that is normal, and comes back as a :class:`PhotoMetadata`
    full of None.
    """


@dataclass(frozen=True, slots=True)
class PhotoMetadata:
    """Everything the catalog wants from a file, per REQUIREMENTS.md §5.1.

    Every field is optional. The library already contains files that have been
    through other software and lost most of their EXIF, so "absent" is a normal
    case throughout, not an error.
    """

    #: EXIF DateTimeOriginal. None when absent or unparseable — WU-1.4 then
    #: applies FR-8.2a's mtime fallback and flags the row as inferred.
    captured_at: dt.datetime | None = None

    #: Actual stored pixel dimensions, unrotated.
    width: int | None = None
    height: int | None = None

    #: 'X-T30 III' on this camera — the model string, not the family (§3).
    camera_model: str | None = None
    lens: str | None = None

    #: Shutter speed, formatted the way a photographer reads it: '1/160'.
    exposure: str | None = None
    f_number: float | None = None
    iso: int | None = None
    focal_length: float | None = None
    orientation: int | None = None

    #: Whether a GPS IFD was present. False on every file this camera writes
    #: (C-1); here so that stays a measured fact rather than an assumption.
    has_gps: bool = False


def read_metadata(path: str | Path) -> PhotoMetadata:
    """Read metadata from a file on disk.

    Raises :class:`MetadataError` if the file is unreadable as an image.
    """
    path = Path(path)
    try:
        with path.open("rb") as stream:
            return read_metadata_from(stream)
    except MetadataError as exc:
        # Re-raise with the filename attached; an import of a thousand frames needs
        # to say *which* one it could not read.
        raise MetadataError(f"{path}: {exc}") from exc.__cause__


def read_metadata_from(stream: BinaryIO) -> PhotoMetadata:
    """Read metadata from an open binary stream, dispatching on magic bytes."""
    magic = stream.read(len(_RAF_MAGIC))
    stream.seek(0)
    if magic == _RAF_MAGIC:
        return _from_raf(stream)
    return _from_jpeg(stream)


# ----------------------------------------------------------------------
# JPEG
# ----------------------------------------------------------------------


def _from_jpeg(stream: BinaryIO) -> PhotoMetadata:
    """The common path: a camera JPEG, read header-only.

    Pillow parses to the start-of-scan marker and stops; no pixel data is
    decoded, which is what FR-1.3 asks for and what makes this affordable
    a thousand times over a card reader.
    """
    try:
        with Image.open(stream) as image:
            width, height = image.size
            exif = image.getexif()
            return _from_exif(exif, width=width, height=height)
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise MetadataError(f"not a readable image ({type(exc).__name__}: {exc})") from exc


def _from_exif(exif: Image.Exif, *, width: int | None, height: int | None) -> PhotoMetadata:
    """Pull §5.1's fields out of a parsed EXIF block."""
    try:
        detail = exif.get_ifd(_TAG_EXIF_IFD)
    except (OSError, ValueError, SyntaxError):
        # A damaged sub-IFD should cost the exposure settings, not the file.
        detail = {}

    return PhotoMetadata(
        captured_at=_parse_datetime(detail.get(_TAG_DATE_TIME_ORIGINAL)),
        width=width,
        height=height,
        camera_model=_clean_text(exif.get(_TAG_MODEL)),
        lens=_clean_text(detail.get(_TAG_LENS_MODEL)),
        exposure=_format_exposure(detail.get(_TAG_EXPOSURE_TIME)),
        f_number=_as_float(detail.get(_TAG_F_NUMBER)),
        iso=_as_int(detail.get(_TAG_ISO)),
        focal_length=_as_float(detail.get(_TAG_FOCAL_LENGTH)),
        orientation=_as_int(exif.get(_TAG_ORIENTATION)),
        has_gps=bool(_gps_ifd(exif)),
    )


def _gps_ifd(exif: Image.Exif) -> dict[int, object]:
    try:
        return dict(exif.get_ifd(_TAG_GPS_IFD))
    except (OSError, ValueError, SyntaxError):
        return {}


# ----------------------------------------------------------------------
# RAF
# ----------------------------------------------------------------------


def _from_raf(stream: BinaryIO) -> PhotoMetadata:
    """A Fujifilm raw file.

    Pillow cannot open a RAF, but one contains a full JPEG preview complete with
    the camera's EXIF. The whole preview is read — about 5 MB — because Pillow
    refuses to open a JPEG it cannot scan to the end of, so a bounded read of
    just the EXIF segment is not an option. That cost is fine: under D-2 a RAF
    is opt-in and rare, and card ingest reads metadata from the paired JPEG.
    """
    header, jpeg_offset, jpeg_length = raf_preview_offset(stream)

    stream.seek(jpeg_offset)
    preview = stream.read(jpeg_length)
    if len(preview) < jpeg_length:
        raise MetadataError("RAF is truncated before the end of its JPEG preview")

    try:
        with Image.open(io.BytesIO(preview)) as image:
            exif = image.getexif()
            metadata = _from_exif(exif, width=None, height=None)
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise MetadataError(
            f"RAF preview is not readable ({type(exc).__name__}: {exc})"
        ) from exc

    # The preview's own dimensions describe the preview, so the real frame size
    # comes from the RAF's CFA header instead. Failing to find it costs the
    # dimensions and nothing else.
    width, height = _raf_dimensions(stream, header)
    return _replace_dimensions(metadata, width, height)


def raf_preview_offset(stream: BinaryIO) -> tuple[bytes, int, int]:
    """Locate the JPEG preview inside a RAF.

    Returns the RAF header bytes and the preview's (offset, length). Reads only
    the 108-byte header, so a caller that wants just the preview's *own* header
    — the thumbnail cache does — never touches the 5 MB body.
    """
    stream.seek(0)
    header = stream.read(0x6C)
    if not header.startswith(RAF_MAGIC):
        raise MetadataError("not a Fujifilm RAF file")
    if len(header) < 0x6C:
        raise MetadataError("RAF is truncated inside its own header")

    jpeg_offset, jpeg_length = struct.unpack(
        ">II", header[_RAF_JPEG_POINTER:_RAF_JPEG_POINTER + 8]
    )
    if jpeg_length <= 0:
        raise MetadataError("RAF contains no embedded JPEG preview")
    return header, jpeg_offset, jpeg_length


def _raf_dimensions(
    stream: BinaryIO, header: bytes
) -> tuple[int | None, int | None]:
    """The frame's real size, from tag 0x0111 of the CFA header directory."""
    offset, length = struct.unpack(
        ">II", header[_RAF_CFA_HEADER_POINTER:_RAF_CFA_HEADER_POINTER + 8]
    )
    if length <= 4:
        return None, None

    try:
        stream.seek(offset)
        directory = stream.read(length)
        if len(directory) < 4:
            return None, None
        (entries,) = struct.unpack(">I", directory[:4])
        position = 4
        for _ in range(entries):
            if position + 4 > len(directory):
                break
            tag, size = struct.unpack(">HH", directory[position:position + 4])
            position += 4
            value = directory[position:position + size]
            position += size
            if tag == _RAF_TAG_IMAGE_SIZE and size == 4:
                stored_height, stored_width = struct.unpack(">HH", value)
                return stored_width, stored_height
    except (OSError, struct.error):
        return None, None
    return None, None


def _replace_dimensions(
    metadata: PhotoMetadata, width: int | None, height: int | None
) -> PhotoMetadata:
    from dataclasses import replace

    return replace(metadata, width=width, height=height)


# ----------------------------------------------------------------------
# Coercion
# ----------------------------------------------------------------------
#
# EXIF values arrive as rationals, NUL-padded strings and occasional nonsense.
# Everything below turns them into something the catalog can store, or None.


def _clean_text(value: object) -> str | None:
    """Trim a fixed-width EXIF string.

    Camera-written ``LensModel`` comes back as 'XF18-55mmF2.8-4 R LM OIS'
    followed by 39 NUL bytes; the same tag on a file that has been through other
    software comes back clean. Both must end up as the same string, because one
    of them is going into a filename.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - decode with errors= cannot raise
            return None
    if not isinstance(value, str):
        return None
    cleaned = value.replace("\x00", "").strip()
    return cleaned or None


def _parse_datetime(value: object) -> dt.datetime | None:
    """EXIF's 'YYYY:MM:DD HH:MM:SS'.

    Naive local time — EXIF has no timezone, and the catalog stores it that way
    so string comparison sorts correctly. An unparseable value is treated as
    absent rather than raising: FR-8.2a's inferred-date flag is the mechanism
    that makes it visible, and one bad date must not fail an import of a thousand.
    """
    text = _clean_text(value)
    if text is None:
        return None
    try:
        return dt.datetime.strptime(text, _EXIF_DATE_FORMAT)
    except ValueError:
        return None


def _format_exposure(value: object) -> str | None:
    """'1/160' — a shutter speed as a photographer reads it.

    EXIF stores it as a rational, and the camera's is unreduced: 10/1600.
    """
    if value is None:
        return None
    numerator = getattr(value, "numerator", None)
    denominator = getattr(value, "denominator", None)
    try:
        if numerator is not None and denominator:
            fraction = Fraction(int(numerator), int(denominator))
        else:
            fraction = Fraction(float(value)).limit_denominator(100000)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None
    if fraction <= 0:
        return None
    if fraction >= 1:
        # A long exposure reads as seconds: '2', '1.6'.
        return f"{float(fraction):g}"
    return f"{fraction.numerator}/{fraction.denominator}"


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    # ISO is occasionally a tuple of two values on some bodies; take the first.
    if isinstance(value, (tuple, list)):
        value = value[0] if value else None
        if value is None:
            return None
    try:
        return int(value)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None
