"""The fixture set covers what EXECUTION_PLAN.md §4.2 requires.

Later units lean on these files heavily, so the coverage claim is asserted here
rather than trusted. If a fixture is lost or rebuilt wrongly, this fails now
instead of producing a confusing failure three units later.
"""

from __future__ import annotations

import datetime as dt

import pytest
from PIL import ExifTags, Image

import fixtures as fx

DATE_TIME_ORIGINAL = 0x9003
PIXEL_X_DIMENSION = 0xA002
GPS_IFD_POINTER = 0x8825
THUMBNAIL_OFFSET = 0x0201
THUMBNAIL_LENGTH = 0x0202


def capture_time(path) -> dt.datetime | None:
    with Image.open(path) as im:
        raw = im.getexif().get_ifd(ExifTags.IFD.Exif).get(DATE_TIME_ORIGINAL)
    return dt.datetime.strptime(raw, "%Y:%m:%d %H:%M:%S") if raw else None


def embedded_thumbnail_length(path) -> int | None:
    with Image.open(path) as im:
        return im.getexif().get_ifd(ExifTags.IFD.IFD1).get(THUMBNAIL_LENGTH)


# ----------------------------------------------------------------------
# Presence
# ----------------------------------------------------------------------


@pytest.mark.parametrize("path", fx.ALL_PHOTOS + (fx.TRUNCATED,), ids=lambda p: p.name)
def test_fixture_is_present(path) -> None:
    assert path.is_file(), f"missing fixture {path} — rebuild with tools/build_fixtures.py"


def test_fixtures_live_in_the_repo_not_in_the_real_library() -> None:
    # A guard against the fixture directory ever being pointed at the library
    # itself, which would make the suite depend on 210 files it must not touch.
    assert fx.PHOTO_DIR.parts[-3:] == ("tests", "fixtures", "photos")
    assert len(list(fx.PHOTO_DIR.glob("*.JPG"))) < 20


# ----------------------------------------------------------------------
# The six cases §4.2 names
# ----------------------------------------------------------------------


def test_a_raw_and_jpeg_pair_share_a_basename() -> None:
    assert fx.RAW_PAIR_JPEG.stem == fx.RAW_PAIR_RAF.stem
    assert fx.RAW_PAIR_JPEG.suffix.lower() == ".jpg"
    assert fx.RAW_PAIR_RAF.suffix.lower() == ".raf"


def test_a_jpeg_has_no_raw_beside_it() -> None:
    assert fx.JPEG_WITHOUT_RAW.is_file()
    assert not fx.JPEG_WITHOUT_RAW.with_suffix(".RAF").exists()


def test_hand_renamed_files_are_present() -> None:
    # The signal the whole project came from (FR-8.2).
    assert fx.RENAMED_LOCATION.name == "seafront.JPG"
    assert fx.RENAMED_EVENT.name == "birthday.JPG"


def test_the_burst_pair_is_seconds_apart() -> None:
    gap = capture_time(fx.BURST_SECOND) - capture_time(fx.BURST_FIRST)

    assert gap == fx.BURST_GAP
    assert gap < dt.timedelta(seconds=5)


def test_the_split_pair_is_hours_apart_on_one_calendar_day() -> None:
    first, second = capture_time(fx.SPLIT_FIRST), capture_time(fx.SPLIT_SECOND)

    assert second - first == fx.SPLIT_GAP
    assert second - first > dt.timedelta(hours=3)  # FR-3.2's suggestion threshold
    assert first.date() == second.date()  # ...but the same day, so FR-3.1 alone won't split it


def test_one_fixture_is_a_full_size_original() -> None:
    # §4.2 requires this: the embedded-thumbnail and draft() paths are only
    # meaningful against a real 14 MB file.
    size = fx.FULL_SIZE_ORIGINAL.stat().st_size

    assert size > 14_000_000, f"{fx.FULL_SIZE_ORIGINAL.name} is only {size / 1e6:.1f} MB"
    with Image.open(fx.FULL_SIZE_ORIGINAL) as im:
        assert im.size == fx.FULL_SIZE_DIMENSIONS


# ----------------------------------------------------------------------
# The awkward cases that make these fixtures worth having
# ----------------------------------------------------------------------


def test_capture_times_match_the_recorded_expectations() -> None:
    for name, expected in fx.CAPTURE_TIMES.items():
        assert capture_time(fx.PHOTO_DIR / name) == expected, name


def test_one_fixture_has_no_capture_date_at_all() -> None:
    # Migration (WU-1.4) cannot assume EXIF gives it a date for every file —
    # and it's the hand-renamed ones, the interesting ones, that lack it.
    assert capture_time(fx.WITHOUT_CAPTURE_DATE) is None


def test_one_fixture_has_no_embedded_thumbnail() -> None:
    # FR-2.1's fast path needs a fallback for files that came from elsewhere.
    assert embedded_thumbnail_length(fx.WITHOUT_EMBEDDED_THUMBNAIL) is None


def test_camera_written_fixtures_do_have_an_embedded_thumbnail() -> None:
    # The basis of FR-2.1. If this ever fails the thumbnail strategy is void.
    for path in (fx.FULL_SIZE_ORIGINAL, fx.BURST_FIRST, fx.SPLIT_SECOND, fx.RENAMED_EVENT):
        length = embedded_thumbnail_length(path)
        assert length and length > 1000, f"{path.name} has no usable embedded thumbnail"


def test_no_fixture_carries_gps() -> None:
    # C-1: the camera records none, so the "no coordinates" path is always the
    # one under test.
    for path in fx.ALL_PHOTOS:
        if path.suffix.lower() == ".raf":
            continue
        with Image.open(path) as im:
            assert GPS_IFD_POINTER not in im.getexif(), path.name


def test_the_truncated_fixture_really_is_broken() -> None:
    with pytest.raises(OSError):
        with Image.open(fx.TRUNCATED) as im:
            im.load()


def test_downscaled_fixtures_claim_the_originals_dimensions_in_exif() -> None:
    # Documented in the fixtures README, pinned here so it can never be a
    # surprise: downscaled fixtures kept their EXIF byte-for-byte (which is how
    # they kept their embedded thumbnails), so EXIF still says 6240 wide.
    with Image.open(fx.BURST_FIRST) as im:
        assert im.size[0] == fx.DOWNSCALED_LONG_EDGE
        claimed = im.getexif().get_ifd(ExifTags.IFD.Exif).get(PIXEL_X_DIMENSION)

    assert claimed == fx.FULL_SIZE_DIMENSIONS[0]


def test_the_full_size_fixture_does_not_have_that_problem() -> None:
    with Image.open(fx.FULL_SIZE_ORIGINAL) as im:
        claimed = im.getexif().get_ifd(ExifTags.IFD.Exif).get(PIXEL_X_DIMENSION)
        assert claimed == im.size[0] == fx.FULL_SIZE_DIMENSIONS[0]
