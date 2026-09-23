"""Named handles on the fixture photos, which ``make_fixtures.py`` generates.

Tests reference these constants rather than filenames, so a test says what case
it is exercising and the fixture set can change without a sweep through the
suite. See ``tests/fixtures/photos/README.md`` for what each file is and why.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

PHOTO_DIR = Path(__file__).resolve().parent / "fixtures" / "photos"

#: The one fixture kept byte-identical at full resolution. Use this, and only
#: this, when EXIF dimensions must agree with the actual pixels, when counting
#: bytes read for the embedded-thumbnail path (FR-2.1), or when timing draft().
FULL_SIZE_ORIGINAL = PHOTO_DIR / "DSCF0807.JPG"

#: A JPEG and its RAF, paired by basename (FR-1.2).
RAW_PAIR_JPEG = FULL_SIZE_ORIGINAL
RAW_PAIR_RAF = PHOTO_DIR / "DSCF0807.RAF"

#: A JPEG with no RAF beside it — the common case under D-2.
JPEG_WITHOUT_RAW = PHOTO_DIR / "DSCF1029.JPG"

#: Two frames 2 seconds apart. Must stay in one session (FR-3.2).
BURST_FIRST = PHOTO_DIR / "DSCF1029.JPG"
BURST_SECOND = PHOTO_DIR / "DSCF1030.JPG"
BURST_GAP = dt.timedelta(seconds=2)

#: Two frames 6h51m apart on the same calendar day. A sub-session split should
#: be *suggested* here, never forced (FR-3.2).
SPLIT_FIRST = PHOTO_DIR / "DSCF0807.JPG"
SPLIT_SECOND = PHOTO_DIR / "DSCF0818.JPG"
SPLIT_GAP = dt.timedelta(hours=6, minutes=51, seconds=5)

#: Hand-renamed files, the signal that started this project (FR-8.2). The
#: place one has also been through other software and lost its EXIF date and
#: embedded thumbnail on the way.
RENAMED_LOCATION = PHOTO_DIR / "seafront.JPG"
RENAMED_EVENT = PHOTO_DIR / "birthday.JPG"

#: No EXIF DateTimeOriginal at all — migration must not assume one exists.
WITHOUT_CAPTURE_DATE = RENAMED_LOCATION

#: No embedded EXIF thumbnail — FR-2.1's fast path needs a fallback.
WITHOUT_EMBEDDED_THUMBNAIL = RENAMED_LOCATION

#: No EXIF Orientation tag either. Absent is a normal case, not an error.
WITHOUT_ORIENTATION = RENAMED_LOCATION

#: Lens string with no NUL padding, because this file has been through other
#: software. Contrast with any camera-written fixture, where it is padded.
UNPADDED_LENS = RENAMED_LOCATION

#: First 64 KB of a JPEG. Must raise something handled.
TRUNCATED = PHOTO_DIR / "truncated.JPG"

#: What the camera calls itself in EXIF. The model string, not the family
#: name — nothing may match on 'X-T30' (EXECUTION_PLAN.md §9).
CAMERA_MODEL = "X-T30 III"

#: The kit lens, *after* the NUL padding is stripped. Camera-written files pad
#: this tag to a fixed width with 39 trailing NUL bytes; files that have been
#: through other software do not. Both must read back as this string.
LENS_MODEL = "XF18-55mmF2.8-4 R LM OIS"

#: Capture times as recorded in EXIF, for tests that need expected values.
#: JPEGs only — `test_fixtures.py` checks these with Pillow directly, on
#: purpose, so that the fixture set is verified independently of our own reader,
#: and Pillow cannot open a RAF. The RAF's time is `RAW_PAIR_CAPTURE_TIME`.
CAPTURE_TIMES: dict[str, dt.datetime | None] = {
    "DSCF0807.JPG": dt.datetime(2026, 7, 13, 12, 50, 56),
    "DSCF0818.JPG": dt.datetime(2026, 7, 13, 19, 42, 1),
    "DSCF1029.JPG": dt.datetime(2026, 9, 6, 11, 6, 51),
    "DSCF1030.JPG": dt.datetime(2026, 9, 6, 11, 6, 53),
    "birthday.JPG": dt.datetime(2026, 4, 2, 17, 27, 14),
    "seafront.JPG": None,
}

#: The RAW pair is one frame, so its RAF and its JPEG must agree on this.
RAW_PAIR_CAPTURE_TIME = CAPTURE_TIMES["DSCF0807.JPG"]

#: True dimensions of the full-size original. Every downscaled fixture except
#: the one that has been through other software still *claims* these in EXIF
#: while actually being 1600 px on the long edge — see the README. This is why
#: FULL_SIZE_ORIGINAL exists.
FULL_SIZE_DIMENSIONS = (6240, 4160)
DOWNSCALED_LONG_EDGE = 1600

#: Every fixture that is a real, readable photo.
ALL_PHOTOS = (
    FULL_SIZE_ORIGINAL,
    RAW_PAIR_RAF,
    SPLIT_SECOND,
    BURST_FIRST,
    BURST_SECOND,
    RENAMED_LOCATION,
    RENAMED_EVENT,
)
