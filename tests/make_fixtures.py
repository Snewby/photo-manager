"""Generate the fixture photographs the suite runs against.

The fixtures are **synthetic**: pixels of noise and gradient, wrapped in EXIF
written byte by byte to look the way this camera writes it. Nothing in them
came from anyone's camera, so the repository can be public and carry no
photographs at all — the files are made on first use (``conftest.py``) into
``tests/fixtures/photos/``, which git ignores.

What they must reproduce is not the picture but the awkwardness that makes
each one worth having, all of it observed in a real X-T30 library first:

- camera-written EXIF with an **embedded thumbnail** in IFD1 (FR-2.1's fast
  path) and a ``LensModel`` padded with **39 trailing NULs**;
- **downscaled** files whose EXIF still claims the full 6240 x 4160;
- one **full-size** original over 14 MB, for byte-counting and ``draft()``;
- a **RAF** — Fujifilm's container, which Pillow cannot open — with the
  camera's EXIF inside its JPEG preview and the frame size in its CFA header;
- a hand-renamed file that has been **through other software**: no capture
  date, no embedded thumbnail, no orientation, and an unpadded lens string;
- a **truncated** JPEG.

``tests/fixtures.py`` names each file by the case it covers.

    python tests/make_fixtures.py [--force]
"""

from __future__ import annotations

import datetime as dt
import io
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

PHOTO_DIR = Path(__file__).resolve().parent / "fixtures" / "photos"

#: Bump when the generated files change, so stale copies are rebuilt.
VERSION = "3"
_STAMP = PHOTO_DIR / ".generated"

MODEL = "X-T30 III"
LENS = "XF18-55mmF2.8-4 R LM OIS"
FULL = (6240, 4160)
DOWNSCALED = (1600, 1067)


@dataclass(frozen=True)
class Exif:
    captured_at: dt.datetime | None
    exposure: tuple[int, int] = (1, 250)
    f_number: tuple[int, int] = (56, 10)
    iso: int = 160
    focal_length: tuple[int, int] = (230, 10)
    orientation: int | None = 1
    padded_lens: bool = True
    thumbnail: bool = True
    claimed_size: tuple[int, int] = FULL


# ----------------------------------------------------------------------
# A little TIFF writer — Pillow cannot write an IFD1 thumbnail
# ----------------------------------------------------------------------

_ASCII, _SHORT, _LONG, _RATIONAL = 2, 3, 4, 5


def _ifd(entries: list[tuple[int, int, object]], start: int, next_ifd: int = 0
         ) -> tuple[bytes, bytes]:
    """One little-endian IFD at offset *start*: (directory, out-of-line data)."""
    directory = struct.pack("<H", len(entries))
    data_start = start + 2 + 12 * len(entries) + 4
    data = b""
    for tag, kind, value in sorted(entries, key=lambda e: e[0]):
        if kind == _ASCII:
            raw = value if isinstance(value, bytes) else value.encode() + b"\0"
            count = len(raw)
        elif kind == _SHORT:
            raw, count = struct.pack("<H", value) + b"\0\0", 1
        elif kind == _LONG:
            raw, count = struct.pack("<I", value), 1
        else:  # RATIONAL
            raw, count = struct.pack("<II", *value), 1
        if len(raw) <= 4:
            field = raw.ljust(4, b"\0")
        else:
            field = struct.pack("<I", data_start + len(data))
            data += raw + (b"\0" if len(raw) % 2 else b"")
        directory += struct.pack("<HHI", tag, kind, count) + field
    directory += struct.pack("<I", next_ifd)
    return directory, data


def exif_bytes(exif: Exif, thumbnail: bytes | None) -> bytes:
    """The APP1 payload: ``Exif\\0\\0`` and a TIFF with IFD0, Exif and IFD1."""
    lens = LENS.encode() + (b"\0" * 39 if exif.padded_lens else b"\0")
    exif_entries: list[tuple[int, int, object]] = [
        (0x829A, _RATIONAL, exif.exposure),
        (0x829D, _RATIONAL, exif.f_number),
        (0x8827, _SHORT, exif.iso),
        (0x920A, _RATIONAL, exif.focal_length),
        (0xA002, _LONG, exif.claimed_size[0]),
        (0xA003, _LONG, exif.claimed_size[1]),
        (0xA434, _ASCII, lens),
    ]
    if exif.captured_at is not None:
        exif_entries.append((0x9003, _ASCII, exif.captured_at.strftime("%Y:%m:%d %H:%M:%S")))

    ifd0_entries: list[tuple[int, int, object]] = [
        (0x010F, _ASCII, "FUJIFILM"),
        (0x0110, _ASCII, MODEL),
        (0x8769, _LONG, 0),  # patched below
    ]
    if exif.orientation is not None:
        ifd0_entries.append((0x0112, _SHORT, exif.orientation))

    # Two passes: sizes first, then real offsets.
    ifd0_start = 8
    ifd0, ifd0_data = _ifd(ifd0_entries, ifd0_start)
    exif_start = ifd0_start + len(ifd0) + len(ifd0_data)
    ifd0_entries = [(t, k, exif_start if t == 0x8769 else v) for t, k, v in ifd0_entries]
    exif_ifd, exif_data = _ifd(exif_entries, exif_start)
    ifd1_start = exif_start + len(exif_ifd) + len(exif_data)

    next_ifd = ifd1_start if thumbnail else 0
    ifd0, ifd0_data = _ifd(ifd0_entries, ifd0_start, next_ifd)
    tiff = b"II*\0" + struct.pack("<I", ifd0_start) + ifd0 + ifd0_data + exif_ifd + exif_data
    if thumbnail:
        ifd1_entries: list[tuple[int, int, object]] = [
            (0x0103, _SHORT, 6),
            (0x0201, _LONG, 0),
            (0x0202, _LONG, len(thumbnail)),
        ]
        probe, _ = _ifd(ifd1_entries, ifd1_start)
        thumb_at = ifd1_start + len(probe)
        ifd1_entries[1] = (0x0201, _LONG, thumb_at)
        ifd1, ifd1_data = _ifd(ifd1_entries, ifd1_start)
        tiff += ifd1 + ifd1_data + thumbnail
    return b"Exif\0\0" + tiff


# ----------------------------------------------------------------------
# Pixels
# ----------------------------------------------------------------------


def _picture(size: tuple[int, int], seed: int, noise: float) -> Image.Image:
    """A gradient with shapes and noise: decodable, compressible to a known-ish
    size, and different per seed so no two fixtures share a content hash."""
    width, height = size
    base = Image.linear_gradient("L").resize(size).convert("RGB")
    tint = Image.new("RGB", size, ((seed * 70) % 256, (seed * 130) % 256, (seed * 30) % 256))
    image = Image.blend(base, tint, 0.4)
    draw = ImageDraw.Draw(image)
    for i in range(6):
        x = (seed * 997 + i * 331) % width
        y = (seed * 577 + i * 211) % height
        r = max(width, height) // (8 + i)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=((i * 40) % 256, 90, (seed * 50) % 256))
    if noise:
        grain = Image.merge("RGB", [Image.effect_noise(size, noise) for _ in range(3)])
        image = Image.blend(image, grain, 0.5)
    return image


def _jpeg(image: Image.Image, exif: Exif, quality: int) -> bytes:
    thumbnail = None
    if exif.thumbnail:
        # The camera's thumbnail is always 160 x 120, letterboxed in black.
        small = image.copy()
        small.thumbnail((160, 120))
        framed = Image.new("RGB", (160, 120))
        framed.paste(small, ((160 - small.width) // 2, (120 - small.height) // 2))
        buffer = io.BytesIO()
        framed.save(buffer, "JPEG", quality=90)
        thumbnail = buffer.getvalue()
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality, exif=exif_bytes(exif, thumbnail))
    return buffer.getvalue()


def _after_exif(jpeg: bytes) -> bytes:
    """Insert a 64 KB application segment straight after the EXIF.

    A camera JPEG carries a large second preview there, so its first 64 KB end
    *inside* a segment: the EXIF and its thumbnail are whole, and the picture
    is unreachable. APP11 is one no reader interprets, so this is inert filler.
    """
    app1 = jpeg.index(b"\xff\xe1")
    (length,) = struct.unpack(">H", jpeg[app1 + 2:app1 + 4])
    end = app1 + 2 + length
    filler = b"\xff\xeb" + struct.pack(">H", 65533) + bytes(65531)
    return jpeg[:end] + filler + jpeg[end:]


def _raf(preview: bytes, frame: tuple[int, int]) -> bytes:
    """A RAF container: header, the JPEG preview, a CFA header directory
    carrying the frame size as tag 0x0111 (height, width), and 32 MB standing
    in for the sensor data — a real RAF's size, so "reads only the headers" is
    measured against a file the size of the real thing."""
    header_size = 0x94
    cfa = struct.pack(">I", 1) + struct.pack(">HH", 0x0111, 4) + struct.pack(">HH", frame[1], frame[0])
    preview_at = header_size
    cfa_at = preview_at + len(preview)
    header = bytearray(header_size)
    header[0:16] = b"FUJIFILMCCD-RAW "
    header[16:20] = b"0201"
    header[20:28] = b"FF383501"
    header[28:28 + len(MODEL)] = MODEL.encode()
    header[0x54:0x5C] = struct.pack(">II", preview_at, len(preview))
    header[0x5C:0x64] = struct.pack(">II", cfa_at, len(cfa))
    return bytes(header) + preview + cfa + bytes(32_000_000)


# ----------------------------------------------------------------------
# The set
# ----------------------------------------------------------------------


def build(directory: Path = PHOTO_DIR) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    split_a = dt.datetime(2026, 7, 13, 12, 50, 56)
    full = Exif(split_a, exposure=(1, 160), f_number=(140, 10), focal_length=(505, 10),
                claimed_size=FULL)

    # Full size, over 14 MB: enough grain that JPEG cannot squeeze it smaller.
    original = _after_exif(_jpeg(_picture(FULL, 1, noise=48), full, quality=92))
    (directory / "DSCF0807.JPG").write_bytes(original)
    # The first 64 KB: the EXIF, and with it the embedded thumbnail, survives
    # whole; the file cannot be decoded, or even identified, past it.
    (directory / "truncated.JPG").write_bytes(original[:65536])

    preview = _jpeg(_picture((1920, 1280), 1, noise=10), full, quality=85)
    (directory / "DSCF0807.RAF").write_bytes(_raf(preview, FULL))

    def downscaled(name: str, seed: int, exif: Exif) -> None:
        (directory / name).write_bytes(_jpeg(_picture(DOWNSCALED, seed, noise=12), exif, 85))

    downscaled("DSCF0818.JPG", 2, Exif(dt.datetime(2026, 7, 13, 19, 42, 1)))
    downscaled("DSCF1029.JPG", 3, Exif(dt.datetime(2026, 9, 6, 11, 6, 51)))
    downscaled("DSCF1030.JPG", 4, Exif(dt.datetime(2026, 9, 6, 11, 6, 53)))
    downscaled("birthday.JPG", 5, Exif(dt.datetime(2026, 4, 2, 17, 27, 14), exposure=(1, 320)))
    # A crop of DSCF0807 that has been through other software: it kept the
    # exposure settings, which is how a dateless file is matched to its source.
    downscaled("seafront.JPG", 6, Exif(None, exposure=(1, 160), f_number=(140, 10),
                                       focal_length=(505, 10), orientation=None,
                                       padded_lens=False, thumbnail=False,
                                       claimed_size=DOWNSCALED))
    _STAMP.write_text(VERSION, encoding="utf-8")


def ensure(directory: Path = PHOTO_DIR) -> None:
    """Build the set unless this version of it is already there."""
    try:
        if _STAMP.read_text(encoding="utf-8") == VERSION:
            return
    except OSError:
        pass
    build(directory)


if __name__ == "__main__":
    build() if "--force" in sys.argv else ensure()
    for path in sorted(PHOTO_DIR.iterdir()):
        print(f"{path.name:<16} {path.stat().st_size:>12,}")
