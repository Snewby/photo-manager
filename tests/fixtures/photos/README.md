# Test fixture photos

**Generated, not committed.** `tests/make_fixtures.py` writes these files the
first time the suite runs (from `conftest.py`), and git ignores everything in
this folder except this README. They are synthetic — gradients, shapes and
noise inside EXIF written to look the way a Fujifilm X-T30 writes it — so the
repository contains no photographs. Rebuild them by hand with:

```
python tests/make_fixtures.py --force
```

Reference them through `tests/fixtures.py`, which gives each one a name that
says what it is for. Don't hardcode filenames in tests.

## What each one covers

| File | Size | Capture time | Covers |
|---|---|---|---|
| `DSCF0807.JPG` | ~16 MB | 2026-07-13 12:50:56 | **Full size, 6240 × 4160.** The only fixture safe for dimension assertions, embedded-thumbnail byte-counting (FR-2.1) and `draft()` timing (FR-2.2). Also the JPEG of the RAW pair, and side A of the session split. |
| `DSCF0807.RAF` | ~0.3 MB | 2026-07-13 12:50:56 | The RAW of the RAW+JPEG pair, paired by basename (FR-1.2): a RAF header, a JPEG preview carrying the camera's EXIF, and a CFA header giving the frame size. It holds no sensor data — nothing here reads any. |
| `DSCF0818.JPG` | ~0.2 MB | 2026-07-13 19:42:01 | Side B of the session split: **6h51m after `DSCF0807`, same calendar day.** |
| `DSCF1029.JPG` | ~0.2 MB | 2026-09-06 11:06:51 | Burst frame A, and a JPEG with **no RAW** beside it. |
| `DSCF1030.JPG` | ~0.2 MB | 2026-09-06 11:06:53 | Burst frame B, **2 s after A**. Must not be split into its own session. |
| `seafront.JPG` | ~0.2 MB | *none* | Hand-renamed, carrying a place in its name (FR-8.2). Also the awkward one: **no `DateTimeOriginal`, no embedded thumbnail, no orientation, and an unpadded lens string**, as if it had been through other software. |
| `birthday.JPG` | ~0.2 MB | 2026-04-02 17:27:14 | The other hand-renamed file, and the contrast to `seafront.JPG` — it kept its date *and* its embedded thumbnail. Its name suggests an event, not a place. |
| `truncated.JPG` | 64 KB | — | The first 64 KB of the full-size JPEG. A malformed file must raise a handled error. |

None of them carry GPS, as the camera records none (C-1).

## Two things that will bite you

**1. Downscaled fixtures claim the full size in EXIF.** Everything except
`DSCF0807.JPG`, its RAF and `seafront.JPG` is 1600 × 1067 pixels, but its EXIF
says **6240 × 4160** — which is what a resized camera file with its EXIF passed
through looks like. Use `fixtures.FULL_SIZE_ORIGINAL` for anything that compares
EXIF dimensions to real ones. `tests/test_fixtures.py` pins this down.

**2. The lens string is NUL-padded.** EXIF `LensModel` is
`'XF18-55mmF2.8-4 R LM OIS'` followed by 39 NUL bytes, as this camera writes it.
It needs stripping before it reaches the database or a filename.
