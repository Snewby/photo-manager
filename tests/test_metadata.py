"""The EXIF reader.

Most of what makes this unit interesting is the awkward cases, and the fixture
set was built to carry them: a file with no capture date, a file whose lens
string is NUL-padded and another whose isn't, a raw file Pillow cannot open at
all, and a truncated file that must not take an import of a thousand frames down
with it.
"""

from __future__ import annotations

import datetime as dt
import io
import shutil
from pathlib import Path
from typing import BinaryIO

import fixtures
import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from photo_manager.metadata import (
    MetadataError,
    PhotoMetadata,
    read_metadata,
    read_metadata_from,
)


class CountingReader:
    """A file object that remembers how much was actually read through it."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        self.bytes_read += len(data)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._stream.seek(offset, whence)

    def tell(self) -> int:
        return self._stream.tell()

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True


# ----------------------------------------------------------------------
# The happy path
# ----------------------------------------------------------------------


def test_every_field_comes_off_the_full_size_original() -> None:
    """REQUIREMENTS.md §5.1's EXIF-derived columns, all of them.

    FULL_SIZE_ORIGINAL is the only fixture whose EXIF dimensions and real pixels
    agree, so it is the only one safe to assert dimensions against.
    """
    metadata = read_metadata(fixtures.FULL_SIZE_ORIGINAL)

    assert metadata == PhotoMetadata(
        captured_at=dt.datetime(2026, 7, 13, 12, 50, 56),
        width=6240,
        height=4160,
        camera_model="X-T30 III",
        lens="XF18-55mmF2.8-4 R LM OIS",
        exposure="1/160",
        f_number=14.0,
        iso=160,
        focal_length=50.5,
        orientation=1,
        has_gps=False,
    )
    assert (metadata.width, metadata.height) == fixtures.FULL_SIZE_DIMENSIONS


@pytest.mark.parametrize(
    "photo", fixtures.ALL_PHOTOS, ids=lambda p: p.name
)
def test_every_readable_fixture_yields_metadata(photo: Path) -> None:
    metadata = read_metadata(photo)

    assert metadata.camera_model == fixtures.CAMERA_MODEL
    assert metadata.lens == fixtures.LENS_MODEL


@pytest.mark.parametrize(
    "name, expected", sorted(fixtures.CAPTURE_TIMES.items())
)
def test_capture_times_match_what_the_camera_recorded(
    name: str, expected: dt.datetime | None
) -> None:
    assert read_metadata(fixtures.PHOTO_DIR / name).captured_at == expected


def test_dimensions_describe_the_file_not_its_exif() -> None:
    """The downscaled fixtures still *claim* 6240 x 4160 in EXIF.

    Reporting the stored pixels rather than the EXIF tag is what keeps the
    catalog honest about what is actually on disk.
    """
    metadata = read_metadata(fixtures.BURST_FIRST)

    assert max(metadata.width, metadata.height) == fixtures.DOWNSCALED_LONG_EDGE
    assert (metadata.width, metadata.height) != fixtures.FULL_SIZE_DIMENSIONS


def test_exposure_reads_the_way_a_photographer_writes_it() -> None:
    """The camera stores 10/1600; nobody wants to see that."""
    assert read_metadata(fixtures.FULL_SIZE_ORIGINAL).exposure == "1/160"
    assert read_metadata(fixtures.RENAMED_EVENT).exposure == "1/320"


# ----------------------------------------------------------------------
# The NUL-padded lens string (EXECUTION_PLAN.md §8)
# ----------------------------------------------------------------------


def test_the_nul_padded_lens_string_is_stripped() -> None:
    """Camera-written LensModel carries 39 trailing NUL bytes.

    This string is destined for a database column and potentially a filename,
    so the padding has to come off before it gets anywhere near either.
    """
    lens = read_metadata(fixtures.FULL_SIZE_ORIGINAL).lens

    assert lens == fixtures.LENS_MODEL
    assert "\x00" not in lens
    assert lens == lens.strip()


def test_padded_and_unpadded_lens_strings_agree() -> None:
    """One file has been through other software and lost the padding.

    Both must produce the same value, or the same lens would become two
    different strings in the catalog.
    """
    padded = read_metadata(fixtures.FULL_SIZE_ORIGINAL).lens
    unpadded = read_metadata(fixtures.UNPADDED_LENS).lens

    assert padded == unpadded == fixtures.LENS_MODEL


# ----------------------------------------------------------------------
# Missing EXIF is normal, not an error
# ----------------------------------------------------------------------


def test_a_file_with_no_capture_date_reads_cleanly() -> None:
    """FR-8.2a's file. Absent, not zero, not guessed, and not an exception.

    WU-1.4 is what turns this None into an mtime and flags the row; deciding
    that here would hide it.
    """
    metadata = read_metadata(fixtures.WITHOUT_CAPTURE_DATE)

    assert metadata.captured_at is None
    # Everything else the file does still have came through.
    assert metadata.camera_model == fixtures.CAMERA_MODEL
    assert metadata.iso == 160


def test_a_missing_orientation_tag_is_none_not_a_default() -> None:
    """Defaulting to 1 would be a guess dressed up as data."""
    assert read_metadata(fixtures.WITHOUT_ORIENTATION).orientation is None


def test_a_jpeg_with_no_exif_at_all_is_not_an_error(tmp_path: Path) -> None:
    plain = tmp_path / "plain.jpg"
    Image.new("RGB", (64, 48)).save(plain, "JPEG")

    metadata = read_metadata(plain)

    assert metadata == PhotoMetadata(width=64, height=48)


# ----------------------------------------------------------------------
# GPS — C-1
# ----------------------------------------------------------------------


@pytest.mark.parametrize("photo", fixtures.ALL_PHOTOS, ids=lambda p: p.name)
def test_no_fixture_carries_gps(photo: Path) -> None:
    """C-1, re-confirmed on every run rather than taken on trust.

    The camera records no GPS, which is the single finding that made location a
    manual-entry problem (D-5). If this ever goes red, that premise changed.
    """
    assert read_metadata(photo).has_gps is False


def test_gps_is_detected_when_a_file_actually_has_it(tmp_path: Path) -> None:
    """Proof that has_gps looks, rather than being hardcoded to the C-1 answer."""
    with_gps = tmp_path / "phone-photo.jpg"
    exif = Image.Exif()
    exif[0x8825] = {1: "N", 2: (50.0, 12.0, 30.0), 3: "W", 4: (5.0, 28.0, 50.0)}
    Image.new("RGB", (8, 8)).save(with_gps, "JPEG", exif=exif)

    assert read_metadata(with_gps).has_gps is True


# ----------------------------------------------------------------------
# RAF
# ----------------------------------------------------------------------


def test_raf_metadata_comes_from_its_embedded_preview() -> None:
    """Pillow cannot open a RAF, but the camera's EXIF is inside it."""
    metadata = read_metadata(fixtures.RAW_PAIR_RAF)

    assert metadata.captured_at == fixtures.RAW_PAIR_CAPTURE_TIME
    assert metadata.camera_model == fixtures.CAMERA_MODEL
    assert metadata.lens == fixtures.LENS_MODEL
    assert metadata.iso == 160


def test_raf_reports_the_frame_size_not_the_preview_size() -> None:
    """The embedded preview is 4416 x 2944. The photograph is not.

    Taking the preview's dimensions would put a number in the catalog that
    describes a thumbnail rather than the raw frame.
    """
    metadata = read_metadata(fixtures.RAW_PAIR_RAF)

    assert (metadata.width, metadata.height) == fixtures.FULL_SIZE_DIMENSIONS


def test_a_raw_pair_agrees_about_the_photograph_it_is() -> None:
    """FR-1.2 pairs these by basename; they had better describe one frame."""
    jpeg = read_metadata(fixtures.RAW_PAIR_JPEG)
    raf = read_metadata(fixtures.RAW_PAIR_RAF)

    assert jpeg.captured_at == raf.captured_at
    assert jpeg.camera_model == raf.camera_model
    assert jpeg.lens == raf.lens
    assert (jpeg.width, jpeg.height) == (raf.width, raf.height)


def test_file_type_is_decided_by_content_not_by_extension(tmp_path: Path) -> None:
    """The library already has birthday.JPG beside Birthday.RAF.

    Case and suffix are not to be trusted; the magic bytes are.
    """
    disguised = tmp_path / "actually_a_raf.JPG"
    shutil.copy2(fixtures.RAW_PAIR_RAF, disguised)

    assert read_metadata(disguised) == read_metadata(fixtures.RAW_PAIR_RAF)


def test_a_truncated_raf_is_a_handled_error(tmp_path: Path) -> None:
    stump = tmp_path / "stump.RAF"
    stump.write_bytes(fixtures.RAW_PAIR_RAF.read_bytes()[:4096])

    with pytest.raises(MetadataError, match="truncated"):
        read_metadata(stump)


def test_a_raf_header_with_nothing_behind_it_is_a_handled_error(
    tmp_path: Path,
) -> None:
    stump = tmp_path / "header-only.RAF"
    stump.write_bytes(b"FUJIFILMCCD-RAW " + b"\x00" * 16)

    with pytest.raises(MetadataError):
        read_metadata(stump)


# ----------------------------------------------------------------------
# Broken files raise something handled
# ----------------------------------------------------------------------


def test_a_truncated_jpeg_raises_a_handled_error() -> None:
    """The whole point: one bad frame must not crash an import of a thousand."""
    with pytest.raises(MetadataError):
        read_metadata(fixtures.TRUNCATED)


def test_the_error_names_the_file_it_failed_on() -> None:
    with pytest.raises(MetadataError, match=fixtures.TRUNCATED.name):
        read_metadata(fixtures.TRUNCATED)


@pytest.mark.parametrize(
    "contents",
    [b"", b"not an image at all", b"\xff\xd8\xff" + b"\x00" * 100],
    ids=["empty", "text", "jpeg-magic-but-garbage"],
)
def test_files_that_are_not_images_raise_a_handled_error(
    tmp_path: Path, contents: bytes
) -> None:
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(contents)

    with pytest.raises(MetadataError):
        read_metadata(broken)


def test_a_missing_file_is_not_a_metadata_error(tmp_path: Path) -> None:
    """A file that isn't there is a different problem from a file that is broken.

    WU-1.4 walks files it has just listed, so this would mean something moved
    underneath it — worth surfacing as itself rather than as 'unreadable image'.
    """
    with pytest.raises(FileNotFoundError):
        read_metadata(tmp_path / "gone.jpg")


# ----------------------------------------------------------------------
# FR-1.3 — read EXIF without decoding full image data
# ----------------------------------------------------------------------


def test_reading_a_jpeg_barely_touches_the_file() -> None:
    """FR-1.3, measured rather than asserted.

    Decoding a thousand 14 MB files over a card reader is ~16 GB and several minutes.
    This path has to stay a header read, so the test puts a ceiling on it.
    """
    size = fixtures.FULL_SIZE_ORIGINAL.stat().st_size

    with fixtures.FULL_SIZE_ORIGINAL.open("rb") as stream:
        counting = CountingReader(stream)
        metadata = read_metadata_from(counting)

    assert metadata.captured_at is not None  # it really did read the EXIF
    assert counting.bytes_read < 1_000_000
    assert counting.bytes_read < size * 0.05


def test_reading_metadata_never_decodes_pixels(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full decode of a 26 MP frame is the thing FR-1.3 exists to prevent.

    ``Image.load`` is the sentinel: reading size and EXIF never calls it, and
    any actual decode calls it several times.
    """

    def explode(self: Image.Image, *args: object, **kwargs: object) -> object:
        raise AssertionError("metadata reading must not decode image data")

    monkeypatch.setattr(Image.Image, "load", explode)

    assert read_metadata(fixtures.FULL_SIZE_ORIGINAL).iso == 160


# ----------------------------------------------------------------------
# Exposure formatting
# ----------------------------------------------------------------------
#
# Tested directly because no fixture is a long exposure, and '2 seconds'
# rendering as '1/0' or '2/1' is the kind of thing nobody notices until it is
# sitting in the catalog.


@pytest.mark.parametrize(
    "numerator, denominator, expected",
    [
        (10, 1600, "1/160"),   # as the camera actually writes it, unreduced
        (1, 160, "1/160"),
        (1, 4000, "1/4000"),
        (2, 125, "2/125"),     # not every shutter speed is 1/x
        (1, 1, "1"),
        (2, 1, "2"),
        (5, 2, "2.5"),         # a long exposure reads as seconds
        (30, 1, "30"),
    ],
)
def test_exposure_formatting(numerator: int, denominator: int, expected: str) -> None:
    from photo_manager.metadata import _format_exposure

    assert _format_exposure(IFDRational(numerator, denominator)) == expected


@pytest.mark.parametrize("value", [None, "not a number", IFDRational(0, 1)])
def test_nonsense_exposure_values_become_none(value: object) -> None:
    from photo_manager.metadata import _format_exposure

    assert _format_exposure(value) is None


# ----------------------------------------------------------------------
# Streams
# ----------------------------------------------------------------------


def test_metadata_can_be_read_from_an_in_memory_stream() -> None:
    """Used by the RAF path, and by anything that already has the bytes."""
    data = io.BytesIO(fixtures.BURST_FIRST.read_bytes())

    assert read_metadata_from(data).captured_at == fixtures.CAPTURE_TIMES["DSCF1029.JPG"]
