"""Rebuilding the catalog from the library and its sidecars (WU-3.5).

This is the unit that makes the rest of the design safe, so the tests are
weighted towards the promise rather than the plumbing.

**Success criterion 4 is a round trip, not an assertion.** The strongest tests
here tag a library through the real tagging code, throw the database away,
rebuild from the files, and compare the two catalogs photograph by photograph.
Anything less would be checking that the rebuild does what it was written to
do, rather than that it recovers what was there.

**What cannot come back has to be measured, not assumed.** `imported_at`, a
session shape that was never tagged, and an override that agreed with its
session are all unrecoverable, and each has a test pinning *what* the rebuild
does instead, so the losses stay the known ones.

**The dated folder is evidence.** D-4 files a photograph under its capture
date, so for a file with no EXIF date the folder recovers a decision that mtime
would overwrite — including one the owner actually made at the WU-1.5 gate. That is
the difference between a rebuild and a re-guess, and it has tests of its own.

The real library is at the bottom: every photograph rebuilt from disk and
compared against the catalog that has been accumulating since WU-1.5.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

import fixtures
from photo_manager import tagging
from photo_manager.catalog import Catalog
from photo_manager.config import Settings
from photo_manager.migration import directory_snapshot
from photo_manager.models import Photo, Session
from photo_manager.rebuild import (
    Comparison,
    PhotoTags,
    RebuildError,
    compare,
    original_name_for,
    rebuild,
    tags_in,
)
from photo_manager.sessions import apply_proposal, propose
from photo_manager.sidecars import Sidecar, write_sidecar

DAY = dt.datetime(2026, 9, 6, 10, 6, 45)


# ----------------------------------------------------------------------
# Helpers — libraries on disk, because that is what a rebuild reads
# ----------------------------------------------------------------------


def place(
    root: Path,
    relative: str,
    *,
    source: Path | None = None,
    mtime: dt.datetime | None = None,
) -> Path:
    """Put a real photograph at a real path under the library root."""
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source or fixtures.JPEG_WITHOUT_RAW, target)
    # Bytes appended after a JPEG's end-of-image marker are ignored by every
    # decoder, so this keeps the EXIF intact while making each copy of one
    # fixture a distinct photograph. Without it these would share a content
    # hash, which is UNIQUE (C-3) — and a library of identical files is its
    # own test, below, rather than an accident in every other one.
    with target.open("ab") as stream:
        stream.write(relative.encode())
    if mtime is not None:
        stamp = mtime.timestamp()
        os.utime(target, (stamp, stamp))
    return target


def rebuilt_settings(settings: Settings, tmp_path: Path) -> Settings:
    return replace(settings, database_path=tmp_path / "rebuilt.sqlite3")


def do_rebuild(settings: Settings, tmp_path: Path) -> tuple[Settings, object]:
    target = tmp_path / "rebuilt.sqlite3"
    result = rebuild(settings, target)
    return replace(settings, database_path=target), result


def add_row(catalog: Catalog, relative: str, *, captured_at: dt.datetime, **columns: object) -> Photo:
    index = catalog.photos.count() + 1
    defaults: dict[str, object] = {
        "content_hash": f"{index:064x}",
        "library_path": relative,
        "original_name": Path(relative).name,
        "captured_at": captured_at,
        "imported_at": dt.datetime(2026, 9, 21, 12, 0),
    }
    defaults.update(columns)
    return catalog.photos.add(Photo(**defaults))  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Reading a name backwards (D-7)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("library_name", "expected"),
    [
        # D-7's shape, with and without a location slug (FR-6.3).
        ("2026-08-01_DSCF0866.jpg", "DSCF0866.JPG"),
        ("2026-09-06_harbour-market_DSCF1006.jpg", "DSCF1006.JPG"),
        ("2026-07-13_DSCF0823.raf", "DSCF0823.RAF"),
        # FR-6.5's collision suffix is the library's, not the camera's.
        ("2026-08-01_DSCF0866_2.jpg", "DSCF0866.JPG"),
        ("2026-09-06_st-aldo_DSCF1006_11.jpg", "DSCF1006.JPG"),
        # The migration did not rename (FR-8.1), so these are already original
        # — including the awkward ones with underscores and mixed case.
        ("birthday.JPG", "birthday.JPG"),
        ("Birthday_1.jpg", "Birthday_1.jpg"),
        ("seafront.JPG", "seafront.JPG"),
        ("DSCF0462.JPG", "DSCF0462.JPG"),
        # A date-like prefix that is not D-7: no separator, so nothing to undo.
        ("2026-08-01.jpg", "2026-08-01.jpg"),
        ("noextension", "noextension"),
    ],
)
def test_the_cameras_name_is_read_back_out_of_the_librarys(
    library_name: str, expected: str
) -> None:
    assert original_name_for(f"2026/2026-08-01/{library_name}") == expected


# ----------------------------------------------------------------------
# Reading the library
# ----------------------------------------------------------------------


def test_a_rebuild_finds_photographs_in_every_dated_folder(
    settings: Settings, tmp_path: Path
) -> None:
    place(settings.library_root, "2026/2026-09-06/a.jpg")
    place(settings.library_root, "2026/2026-09-07/b.jpg")
    place(settings.library_root, "2025/2025-01-02/c.jpg")

    _, result = do_rebuild(settings, tmp_path)

    assert result.photographs == 3


def test_a_raw_pair_is_one_photograph(settings: Settings, tmp_path: Path) -> None:
    """FR-1.2, and the pairing is case-insensitive because this library holds
    `Birthday.RAF` beside `birthday.JPG`."""
    place(settings.library_root, "2026/2026-07-13/DSCF0823.JPG", source=fixtures.RAW_PAIR_JPEG)
    place(settings.library_root, "2026/2026-07-13/dscf0823.RAF", source=fixtures.RAW_PAIR_RAF)

    rebuilt, result = do_rebuild(settings, tmp_path)

    assert result.photographs == 1
    with Catalog.open(rebuilt, verify_root=False) as catalog:
        photo = catalog.photos.all()[0]
        assert photo.has_raw
        assert photo.raw_path.endswith(".RAF")


def test_the_same_frame_number_on_two_days_is_two_photographs(
    settings: Settings, tmp_path: Path
) -> None:
    """The camera's counter rolls over, so the folder is part of the identity."""
    place(settings.library_root, "2026/2026-09-06/DSCF0007.jpg")
    place(settings.library_root, "2026/2026-09-07/DSCF0007.jpg", source=fixtures.BURST_FIRST)

    _, result = do_rebuild(settings, tmp_path)

    assert result.photographs == 2


def test_sidecars_are_read_with_their_photograph_and_never_as_one(
    settings: Settings, tmp_path: Path
) -> None:
    photo = place(settings.library_root, "2026/2026-09-06/a.jpg")
    write_sidecar(photo, Sidecar(rating=4))

    _, result = do_rebuild(settings, tmp_path)

    assert result.photographs == 1
    assert result.sidecars_read == 1
    assert result.unreadable == ()


def test_something_that_is_not_a_photograph_is_reported_not_skipped(
    settings: Settings, tmp_path: Path
) -> None:
    """FR-1.3a decides by magic bytes. A file in the library that is not a
    photograph is worth a human knowing about."""
    place(settings.library_root, "2026/2026-09-06/a.jpg")
    (settings.library_root / "2026" / "notes.txt").write_text("hello")

    _, result = do_rebuild(settings, tmp_path)

    assert result.photographs == 1
    assert [u.path.name for u in result.unreadable] == ["notes.txt"]
    assert "magic bytes" in result.unreadable[0].reason


def test_a_lone_raf_is_catalogued_rather_than_dropped(
    settings: Settings, tmp_path: Path
) -> None:
    """Not ordinary (FR-1.2), but a rebuild that dropped it would leave a file
    with no row — breaking the very thing an index is for."""
    place(settings.library_root, "2026/2026-07-13/DSCF0823.RAF", source=fixtures.RAW_PAIR_RAF)

    rebuilt, result = do_rebuild(settings, tmp_path)

    assert result.photographs == 1
    assert any("no JPEG" in note for note in result.notes)
    with Catalog.open(rebuilt, verify_root=False) as catalog:
        assert catalog.photos.all()[0].library_path.endswith(".RAF")


def test_every_file_on_disk_ends_up_with_a_row(settings: Settings, tmp_path: Path) -> None:
    """The invariant that makes the catalog an index of the library."""
    place(settings.library_root, "2026/2026-07-13/DSCF0823.JPG", source=fixtures.RAW_PAIR_JPEG)
    place(settings.library_root, "2026/2026-07-13/DSCF0823.RAF", source=fixtures.RAW_PAIR_RAF)
    place(settings.library_root, "2026/2026-09-06/a.jpg")

    rebuilt, _ = do_rebuild(settings, tmp_path)

    with Catalog.open(rebuilt, verify_root=False) as catalog:
        catalogued = set()
        for photo in catalog.photos.all():
            catalogued.add(catalog.absolute_path(photo))
            if photo.has_raw:
                catalogued.add(catalog.absolute_raw_path(photo))
        on_disk = {
            p for p in settings.library_root.rglob("*")
            if p.is_file() and p.suffix.lower() != ".xmp"
        }
    assert on_disk == catalogued


def test_a_rebuild_writes_nothing_in_the_library(settings: Settings, tmp_path: Path) -> None:
    """Checked by snapshotting, not promised — the same habit as FR-8.3."""
    photo = place(settings.library_root, "2026/2026-09-06/a.jpg")
    write_sidecar(photo, Sidecar(rating=3, location="St Aldo"))
    before = directory_snapshot(settings.library_root)

    do_rebuild(settings, tmp_path)

    assert directory_snapshot(settings.library_root) == before


# ----------------------------------------------------------------------
# When the shutter fired, and which source said so
# ----------------------------------------------------------------------


def test_exif_wins_and_is_not_marked_inferred(settings: Settings, tmp_path: Path) -> None:
    place(settings.library_root, "2026/2026-07-13/a.jpg", source=fixtures.RAW_PAIR_JPEG,
          mtime=dt.datetime(2020, 1, 1, 0, 0))

    rebuilt, result = do_rebuild(settings, tmp_path)

    with Catalog.open(rebuilt, verify_root=False) as catalog:
        photo = catalog.photos.all()[0]
    assert photo.captured_at.year == 2026
    assert not photo.captured_at_inferred
    assert result.inferred_dates == 0


def test_a_paired_raf_supplies_a_missing_date_without_the_flag(
    settings: Settings, tmp_path: Path
) -> None:
    """FR-8.2a: a pair is one frame, so the RAF's time is that frame's real
    time rather than a guess about it."""
    place(settings.library_root, "2026/2026-07-13/x.JPG", source=fixtures.WITHOUT_CAPTURE_DATE)
    place(settings.library_root, "2026/2026-07-13/x.RAF", source=fixtures.RAW_PAIR_RAF)

    rebuilt, _ = do_rebuild(settings, tmp_path)

    with Catalog.open(rebuilt, verify_root=False) as catalog:
        photo = catalog.photos.all()[0]
    assert not photo.captured_at_inferred


def test_the_dated_folder_beats_mtime_and_recovers_a_decision(
    settings: Settings, tmp_path: Path
) -> None:
    """**The reason this rebuild is not a re-guess.**

    D-4 files a photograph under its capture date, so the folder records a
    decision — including one a human made at the migration review. The real
    library has exactly this case: `Birthday_1.jpg` has no EXIF date, its
    mtime is 2026-04-03, and the owner put it in `2026-04-02/`. mtime alone would
    quietly move it a day and undo that decision.
    """
    place(settings.library_root, "2026/2026-04-02/Birthday_1.jpg",
          source=fixtures.WITHOUT_CAPTURE_DATE,
          mtime=dt.datetime(2026, 4, 3, 9, 7, 54))

    rebuilt, result = do_rebuild(settings, tmp_path)

    with Catalog.open(rebuilt, verify_root=False) as catalog:
        photo = catalog.photos.all()[0]
    assert photo.captured_at.date() == dt.date(2026, 4, 2), "mtime overrode the folder"
    # Still a guess, and still flagged as one — what changed is which guess.
    assert photo.captured_at_inferred
    assert result.inferred_dates == 1
    assert any("dated folder" in note for note in result.notes)


def test_mtime_is_used_when_the_folder_agrees_or_is_not_dated(
    settings: Settings, tmp_path: Path
) -> None:
    """The folder is only consulted where it has something different to say."""
    place(settings.library_root, "loose/x.jpg", source=fixtures.WITHOUT_CAPTURE_DATE,
          mtime=dt.datetime(2026, 5, 9, 15, 45, 0))

    rebuilt, result = do_rebuild(settings, tmp_path)

    with Catalog.open(rebuilt, verify_root=False) as catalog:
        photo = catalog.photos.all()[0]
    assert photo.captured_at == dt.datetime(2026, 5, 9, 15, 45, 0)
    assert photo.captured_at_inferred
    assert not any("dated folder" in note for note in result.notes)


# ----------------------------------------------------------------------
# Rebuilding the outings from the only evidence there is
# ----------------------------------------------------------------------


def tagged_library(settings: Settings) -> None:
    """Three days: one tagged as an outing with an override, two untagged."""
    for minute in range(4):
        place(settings.library_root, f"2026/2026-09-06/a{minute}.jpg",
              source=fixtures.JPEG_WITHOUT_RAW,
              mtime=DAY + dt.timedelta(minutes=minute))
    place(settings.library_root, "2026/2026-09-07/b.jpg", source=fixtures.BURST_FIRST)


def test_an_untagged_day_comes_back_as_one_session(
    settings: Settings, tmp_path: Path
) -> None:
    """FR-3.1's default, and the honest answer: nothing recorded a split."""
    for minute in (0, 30, 400):  # a gap of over three hours, never tagged
        place(settings.library_root, f"2026/2026-09-06/a{minute}.jpg",
              source=fixtures.JPEG_WITHOUT_RAW, mtime=DAY + dt.timedelta(minutes=minute))

    rebuilt, result = do_rebuild(settings, tmp_path)

    assert result.sessions == 1
    with Catalog.open(rebuilt, verify_root=False) as catalog:
        assert len(catalog.photos.in_session(catalog.sessions.all()[0].id)) == 3


def test_a_day_whose_halves_were_tagged_differently_comes_back_as_two(
    settings: Settings, tmp_path: Path
) -> None:
    """The event is a session's own field, so a change of event within a day is
    a change of session — evidence that survives where the shape does not."""
    for minute, event in ((0, "Morning"), (10, "Morning"), (300, "Evening")):
        photo = place(settings.library_root, f"2026/2026-09-06/a{minute}.jpg",
                      source=fixtures.JPEG_WITHOUT_RAW,
                      mtime=DAY + dt.timedelta(minutes=minute))
        write_sidecar(photo, Sidecar(location="St Aldo", event=event))

    rebuilt, result = do_rebuild(settings, tmp_path)

    assert result.sessions == 2
    with Catalog.open(rebuilt, verify_root=False) as catalog:
        events = sorted(s.event for s in catalog.sessions.all())
    assert events == ["Evening", "Morning"]


def test_the_odd_frame_out_comes_back_as_an_override(
    settings: Settings, tmp_path: Path
) -> None:
    """Nothing recorded which location was the session's and which was the
    exception — the majority says so, which is exactly the shape that "tag the
    outing, then correct one frame" produces."""
    for minute in range(4):
        photo = place(settings.library_root, f"2026/2026-09-06/a{minute}.jpg",
                      source=fixtures.JPEG_WITHOUT_RAW, mtime=DAY + dt.timedelta(minutes=minute))
        where = "Penzance" if minute == 2 else "St Aldo"
        write_sidecar(photo, Sidecar(location=where, event="Beach day"))

    rebuilt, result = do_rebuild(settings, tmp_path)

    assert result.sessions == 1
    with Catalog.open(rebuilt, verify_root=False) as catalog:
        session = catalog.sessions.all()[0]
        assert catalog.locations.get(session.location_id).name == "St Aldo"
        overrides = [
            p for p in catalog.photos.all()
            if catalog.photos.location_override(p.id) is not None
        ]
        assert len(overrides) == 1
        assert catalog.photos.location(overrides[0].id).name == "Penzance"


def test_a_located_and_an_unlocated_photograph_are_not_put_in_one_session(
    settings: Settings, tmp_path: Path
) -> None:
    """The one way this could *invent* a tag rather than lose one: the
    untagged frame would inherit a location its sidecar never claimed."""
    first = place(settings.library_root, "2026/2026-09-06/a.jpg", mtime=DAY)
    write_sidecar(first, Sidecar(location="St Aldo"))
    place(settings.library_root, "2026/2026-09-06/b.jpg",
          source=fixtures.BURST_FIRST, mtime=DAY + dt.timedelta(minutes=5))

    rebuilt, result = do_rebuild(settings, tmp_path)

    assert result.sessions == 2
    with Catalog.open(rebuilt, verify_root=False) as catalog:
        located = [p for p in catalog.photos.all() if catalog.photos.location(p.id)]
        assert len(located) == 1


def test_people_and_tags_come_back_with_their_use_counts(
    settings: Settings, tmp_path: Path
) -> None:
    for minute in range(3):
        photo = place(settings.library_root, f"2026/2026-09-06/a{minute}.jpg",
                      source=fixtures.JPEG_WITHOUT_RAW, mtime=DAY + dt.timedelta(minutes=minute))
        write_sidecar(photo, Sidecar(people=("Alice",), tags=("beach", "sunset")))

    rebuilt, _ = do_rebuild(settings, tmp_path)

    with Catalog.open(rebuilt, verify_root=False) as catalog:
        assert catalog.people.by_name("Alice").use_count == 3
        assert sorted(t.name for t in catalog.tags.all()) == ["beach", "sunset"]
        assert catalog.tags.by_name("beach").use_count == 3


def test_a_rebuilt_location_gets_a_slug(settings: Settings, tmp_path: Path) -> None:
    """D-7 needs one, and the sidecar carries only the name."""
    photo = place(settings.library_root, "2026/2026-09-06/a.jpg")
    write_sidecar(photo, Sidecar(location="St. Aldo"))

    rebuilt, _ = do_rebuild(settings, tmp_path)

    with Catalog.open(rebuilt, verify_root=False) as catalog:
        assert catalog.locations.by_name("St. Aldo").slug == "st-aldo"


# ----------------------------------------------------------------------
# Refusals
# ----------------------------------------------------------------------


def test_rebuilding_over_the_configured_catalog_is_refused(settings: Settings) -> None:
    """A rebuild is compared before it is adopted, and one that overwrote the
    thing it is checked against would leave nothing to check against."""
    place(settings.library_root, "2026/2026-09-06/a.jpg")

    with pytest.raises(RebuildError, match="never over it"):
        rebuild(settings, settings.database_path)


def test_rebuilding_into_an_existing_file_is_refused(
    settings: Settings, tmp_path: Path
) -> None:
    place(settings.library_root, "2026/2026-09-06/a.jpg")
    (tmp_path / "taken.sqlite3").write_bytes(b"")

    with pytest.raises(RebuildError, match="already exists"):
        rebuild(settings, tmp_path / "taken.sqlite3")


# ----------------------------------------------------------------------
# Comparing — the criterion, made measurable
# ----------------------------------------------------------------------


def test_comparing_a_catalog_with_itself_is_lossless(catalog: Catalog) -> None:
    add_row(catalog, "2026/2026-09-06/a.jpg", captured_at=DAY)

    result = compare(catalog, catalog)

    assert result.lossless
    assert result.compared == 1
    assert "0 lost" in result.summary()


def test_a_lost_photograph_is_not_lossless(settings: Settings, tmp_path: Path) -> None:
    with Catalog.open(settings) as before:
        add_row(before, "2026/2026-09-06/a.jpg", captured_at=DAY)
        add_row(before, "2026/2026-09-06/b.jpg", captured_at=DAY)
        after_settings = replace(settings, database_path=tmp_path / "other.sqlite3")
        with Catalog.open(after_settings, verify_root=False) as after:
            add_row(after, "2026/2026-09-06/a.jpg", captured_at=DAY)

            result = compare(before, after)

    assert not result.lossless
    assert len(result.missing) == 1


def test_a_changed_rating_is_reported_by_field(
    settings: Settings, tmp_path: Path
) -> None:
    with Catalog.open(settings) as before:
        add_row(before, "2026/2026-09-06/a.jpg", captured_at=DAY, rating=5)
        after_settings = replace(settings, database_path=tmp_path / "other.sqlite3")
        with Catalog.open(after_settings, verify_root=False) as after:
            add_row(after, "2026/2026-09-06/a.jpg", captured_at=DAY, rating=0)

            result = compare(before, after)

    assert not result.lossless
    assert result.changed[0].fields() == ["rating"]
    assert result.changed[0].library_path == "2026/2026-09-06/a.jpg"


def test_a_photograph_the_old_catalog_never_had_is_added_not_lost(
    settings: Settings, tmp_path: Path
) -> None:
    """The index having drifted from the library, which is the rebuild finding
    a real file rather than inventing one."""
    with Catalog.open(settings) as before:
        add_row(before, "2026/2026-09-06/a.jpg", captured_at=DAY)
        after_settings = replace(settings, database_path=tmp_path / "other.sqlite3")
        with Catalog.open(after_settings, verify_root=False) as after:
            add_row(after, "2026/2026-09-06/a.jpg", captured_at=DAY)
            add_row(after, "2026/2026-09-06/b.jpg", captured_at=DAY)

            result = compare(before, after)

    assert result.lossless, "an extra file is not a loss"
    assert len(result.added) == 1


def test_the_comparison_ignores_what_is_known_to_be_unrecoverable(
    settings: Settings, tmp_path: Path
) -> None:
    """`imported_at` cannot come back and the module says so up front. Counting
    it as a difference would bury the answer in noise that is already known."""
    with Catalog.open(settings) as before:
        add_row(before, "2026/2026-09-06/a.jpg", captured_at=DAY,
                imported_at=dt.datetime(2026, 9, 20, 9, 0))
        after_settings = replace(settings, database_path=tmp_path / "other.sqlite3")
        with Catalog.open(after_settings, verify_root=False) as after:
            add_row(after, "2026/2026-09-06/a.jpg", captured_at=DAY,
                    imported_at=dt.datetime(2001, 1, 1, 1, 1))

            assert compare(before, after).lossless


# ----------------------------------------------------------------------
# The round trip — success criterion 4
# ----------------------------------------------------------------------


def test_deleting_the_database_and_rebuilding_loses_no_tags(
    settings: Settings, tmp_path: Path
) -> None:
    """**The acceptance criterion.**

    Tagged through the real tagging code, then thrown away and reconstructed
    from the files alone. Not "the rebuild does what it was written to do" —
    "what a person typed is still there afterwards".
    """
    for index in range(5):
        place(settings.library_root, f"2026/2026-09-06/a{index}.jpg",
              source=fixtures.JPEG_WITHOUT_RAW)
    place(settings.library_root, "2026/2026-07-13/b.JPG", source=fixtures.RAW_PAIR_JPEG)

    # Build a catalog the ordinary way, then tag it the ordinary way.
    seed, _ = do_rebuild(settings, tmp_path / "seed")
    with Catalog.open(seed, verify_root=False) as catalog:
        day = max(catalog.sessions.all(), key=lambda s: len(catalog.photos.in_session(s.id)))
        assert len(catalog.photos.in_session(day.id)) == 5

        tagging.set_session_location(catalog, day.id, "Harbour Market")
        tagging.set_session_event(catalog, day.id, "Saturday market")
        tagging.set_session_people(catalog, day.id, ["Alice", "Bob"])
        tagging.set_session_tags(catalog, day.id, ["market", "france"])
        odd = catalog.photos.in_session(day.id)[2]
        tagging.set_photo_location(catalog, odd.id, "Café de la Place")
        tagging.set_photo_rating(catalog, odd.id, 5)
        before_tags = tags_in(catalog)

    # Throw the database away entirely. The files are all that is left.
    (tmp_path / "seed" / "rebuilt.sqlite3").unlink()

    after_settings, result = do_rebuild(settings, tmp_path / "again")
    with Catalog.open(after_settings, verify_root=False) as after:
        assert tags_in(after) == before_tags, "a tag did not survive the round trip"
        assert result.photographs == 6

        # And the structure a person would recognise came back with it.
        outing = max(after.sessions.all(), key=lambda s: len(after.photos.in_session(s.id)))
        assert after.locations.get(outing.location_id).name == "Harbour Market"
        assert outing.event == "Saturday market"
        overrides = [p for p in after.photos.all()
                     if after.photos.location_override(p.id) is not None]
        assert len(overrides) == 1
        assert after.photos.location(overrides[0].id).name == "Café de la Place"
        assert after.photos.get(overrides[0].id).rating == 5


def test_a_library_with_two_identical_files_is_reported_not_fatal(
    settings: Settings, tmp_path: Path
) -> None:
    """A recovery tool is run precisely when something has already gone wrong.

    ``content_hash`` is UNIQUE because it is the photograph's identity (C-3),
    so two byte-identical files cannot both have a row — and falling over with
    a raw constraint error at that moment would be the worst time to be
    brittle (FR-1.3c). Found by writing the tests: copying one fixture to five
    paths made five identical files and took the whole rebuild down.
    """
    first = place(settings.library_root, "2026/2026-09-06/a.jpg")
    shutil.copy2(first, settings.library_root / "2026" / "2026-09-06" / "a-copy.jpg")

    rebuilt, result = do_rebuild(settings, tmp_path)

    assert result.photographs == 1
    assert len(result.duplicates) == 1
    assert any("byte-identical" in note for note in result.notes)
    with Catalog.open(rebuilt, verify_root=False) as catalog:
        assert catalog.photos.count() == 1
        kept = catalog.photos.all()[0].library_path

    # Which of two identical files keeps the row is arbitrary — they are the
    # same photograph — but it must not vary between runs, or "rebuild if in
    # doubt" would produce a different catalog each time.
    again_settings, again = do_rebuild(settings, tmp_path / "again")
    with Catalog.open(again_settings, verify_root=False) as catalog:
        assert catalog.photos.all()[0].library_path == kept
    assert [d.path for d in again.duplicates] == [d.path for d in result.duplicates]


def test_a_rebuild_of_a_rebuild_is_identical(settings: Settings, tmp_path: Path) -> None:
    """Idempotence. A recovery step that drifted a little each time would make
    "rebuild if in doubt" quietly dangerous."""
    photo = place(settings.library_root, "2026/2026-09-06/a.jpg", mtime=DAY)
    write_sidecar(photo, Sidecar(rating=3, location="St Aldo", event="Beach day",
                                 people=("Alice",), tags=("beach",)))

    first, _ = do_rebuild(settings, tmp_path / "one.sqlite3")
    second, _ = do_rebuild(settings, tmp_path / "two.sqlite3")

    with Catalog.open(first, verify_root=False) as a, Catalog.open(second, verify_root=False) as b:
        assert tags_in(a) == tags_in(b)
        assert compare(a, b).lossless


# ----------------------------------------------------------------------
# The real library, and the catalog since WU-1.5
# ----------------------------------------------------------------------


@pytest.fixture
def real_library() -> Settings:
    """The configured library, or a skip. Never written to."""
    try:
        settings = Settings.load()
    except Exception as exc:  # noqa: BLE001 — no config on this machine is fine
        pytest.skip(f"no usable config.toml: {exc}")
    if not settings.library_root.is_dir():
        pytest.skip(f"library root absent: {settings.library_root}")
    return settings


@pytest.fixture(scope="session")
def rebuilt_real_library(tmp_path_factory: pytest.TempPathFactory) -> tuple[Settings, object]:
    """One rebuild of the real library, shared by every test below.

    Hashing a few GB takes seconds, and doing it once per test put
    a minute and a half on the whole suite for no extra coverage — the three
    tests below ask different questions of the *same* rebuild.
    """
    try:
        settings = Settings.load()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no usable config.toml: {exc}")
    if not settings.library_root.is_dir():
        pytest.skip(f"library root absent: {settings.library_root}")

    target = tmp_path_factory.mktemp("rebuild") / "rebuilt.sqlite3"
    before = directory_snapshot(settings.library_root)
    result = rebuild(settings, target)
    assert directory_snapshot(settings.library_root) == before, (
        "the rebuild wrote to the library"
    )
    return replace(settings, database_path=target), result


@pytest.mark.needs_library
def test_the_real_catalog_can_be_rebuilt_from_the_real_library(
    real_library: Settings, rebuilt_real_library: tuple[Settings, object]
) -> None:
    """**Success criterion 4, on the photographs it was written for.**

    the owner's catalog has been accumulating since the migration — migrated rows, imported
    ones and the sidecars a real card run wrote. This throws all of it away and
    reconstructs it from the files on disk.

    It rebuilds into a temporary database and reads the real one. Nothing here
    writes to either the library or the catalog.
    """
    rebuilt, result = rebuilt_real_library

    with Catalog.open(real_library) as before, Catalog.open(rebuilt, verify_root=False) as after:
        assert result.photographs == before.photos.count()
        comparison = compare(before, after)

    assert comparison.lossless, comparison.summary()
    assert comparison.missing == ()
    assert comparison.changed == ()


@pytest.mark.needs_library
def test_the_rebuilt_real_catalog_agrees_on_every_column_that_can_come_back(
    real_library: Settings, rebuilt_real_library: tuple[Settings, object]
) -> None:
    """Beyond the tags: the columns a rebuild *should* reproduce exactly.

    `imported_at` is left out — it is not recoverable and the module says so.
    `captured_at` is checked to the **day** rather than the second, because one
    real file's time of day came from a human at the WU-1.5 review and only its
    date survives in the folder; the next test pins that one on its own rather
    than letting it weaken this one.
    """
    rebuilt, _ = rebuilt_real_library

    exact = ("library_path", "original_name", "raw_path", "width", "height",
             "camera_model", "lens", "exposure", "f_number", "iso",
             "focal_length", "orientation", "rating", "captured_at_inferred")

    with Catalog.open(real_library) as before, Catalog.open(rebuilt, verify_root=False) as after:
        original = {p.content_hash: p for p in before.photos.all()}
        made = {p.content_hash: p for p in after.photos.all()}
        assert set(original) == set(made)

        for digest, photo in original.items():
            other = made[digest]
            for column in exact:
                assert getattr(photo, column) == getattr(other, column), (
                    f"{photo.library_path}: {column}"
                )
            assert photo.captured_at.date() == other.captured_at.date(), photo.library_path


@pytest.mark.needs_library
def test_every_real_capture_time_comes_back(
    real_library: Settings, rebuilt_real_library: tuple[Settings, object]
) -> None:
    """Including the one that used to be named here as the exception.

    `Birthday_1.jpg` has no EXIF date at all. The owner corrected it at the
    WU-1.5 gate to 2026-04-02T17:27:14, and until 2026-09-23 that decision
    survived on disk only as its day folder, so a rebuild recovered the day and
    took the time of day from an mtime a day out. The month re-file removed the
    day folder, and at the owner's choice set the file's mtime to the decided moment
    instead (refile.py) — which gives back the time as well as the day.
    """
    rebuilt, _ = rebuilt_real_library

    with Catalog.open(real_library) as before, Catalog.open(rebuilt, verify_root=False) as after:
        originals = {p.library_path: p for p in before.photos.all()}
        made = {p.library_path: p for p in after.photos.all()}
        differing = [
            path for path, photo in originals.items()
            if photo.captured_at != made[path].captured_at
        ]

    assert differing == [], differing


# ----------------------------------------------------------------------
# Gaps found by breaking the code on purpose
# ----------------------------------------------------------------------


def test_the_folder_is_only_consulted_when_it_disagrees_with_mtime(
    settings: Settings, tmp_path: Path
) -> None:
    """A dated folder that says the same thing as mtime has told us nothing,
    and reporting it as a recovered decision would be noise in the one place
    the notes exist to be read.

    Found by breaking the guard and watching the suite stay green: the previous
    test covered "the folder is not dated" but never "the folder agrees".
    """
    place(settings.library_root, "2026/2026-05-09/x.jpg",
          source=fixtures.WITHOUT_CAPTURE_DATE,
          mtime=dt.datetime(2026, 5, 9, 15, 45, 0))

    rebuilt, result = do_rebuild(settings, tmp_path)

    with Catalog.open(rebuilt, verify_root=False) as catalog:
        assert catalog.photos.all()[0].captured_at == dt.datetime(2026, 5, 9, 15, 45, 0)
    assert not any("dated folder" in note for note in result.notes)


def test_the_rafs_sidecar_is_read_when_the_jpegs_has_been_lost(
    settings: Settings, tmp_path: Path
) -> None:
    """Both halves of a pair get one and they agree (§5.2) — so if the JPEG's
    has been deleted and the RAF's has not, reading the RAF's is the difference
    between recovering the tags and losing them."""
    place(settings.library_root, "2026/2026-07-13/DSCF0823.JPG", source=fixtures.RAW_PAIR_JPEG)
    raw = place(settings.library_root, "2026/2026-07-13/DSCF0823.RAF",
                source=fixtures.RAW_PAIR_RAF)
    write_sidecar(raw, Sidecar(rating=4, location="St Aldo"))

    rebuilt, result = do_rebuild(settings, tmp_path)

    assert result.sidecars_read == 1
    with Catalog.open(rebuilt, verify_root=False) as catalog:
        photo = catalog.photos.all()[0]
        assert photo.rating == 4
        assert catalog.photos.location(photo.id).name == "St Aldo"


def test_a_rebuilt_row_carries_the_cameras_name_not_the_librarys(
    settings: Settings, tmp_path: Path
) -> None:
    """`original_name` is what the camera called it, and D-7 renamed the file
    around it (FR-6.3). Reading it back is what keeps FR-1.4's cheap dedup tier
    working after a rebuild."""
    place(settings.library_root, "2026/2026-09-06/2026-09-06_harbour-market_DSCF1006.jpg")

    rebuilt, _ = do_rebuild(settings, tmp_path)

    with Catalog.open(rebuilt, verify_root=False) as catalog:
        assert catalog.photos.all()[0].original_name == "DSCF1006.JPG"


def test_the_comparison_reads_the_location_that_applies(catalog: Catalog) -> None:
    """FR-3.4's precedence, in the instrument that measures tag loss.

    A photograph with no override of its own still *has* a location — its
    session's — and a comparison that read only the override would score a
    whole tagged outing as having no location at all, in both catalogs, and
    call losing it lossless.
    """
    photo = add_row(catalog, "2026/2026-09-06/a.jpg", captured_at=DAY)
    location = catalog.locations.add(
        __import__("photo_manager.models", fromlist=["Location"]).Location(
            name="St Aldo", slug="st-aldo"
        )
    )
    session = catalog.sessions.add(
        Session(started_at=DAY, ended_at=DAY, location_id=location.id, event="Beach day")
    )
    catalog.sessions.assign(session.id, [photo.id])

    tags = tags_in(catalog)[photo.content_hash]

    assert catalog.photos.location_override(photo.id) is None
    assert tags.location == "St Aldo"
    assert tags.event == "Beach day"
