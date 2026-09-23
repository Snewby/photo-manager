"""Copying keepers into the library (WU-2.4).

The acceptance criteria point at four things, and the second is the one that
earns most of this file:

1. imported bytes are identical to the card;
2. **a partial failure leaves no half-file and no catalog row** — tested by
   injecting a failure mid-copy rather than by reasoning about it;
3. duplicates are skipped;
4. collisions are suffixed deterministically.

There is a fifth that is not in the list and matters as much: **the card is
read-only** (§4.1). An import is the first operation that reads every byte of
it, so it is the most likely place to accidentally open something for writing.
The snapshot test at the bottom is the one that would notice.
"""

from __future__ import annotations

import datetime as dt
import shutil
from collections.abc import Iterator
from pathlib import Path

import fixtures
import pytest

from photo_manager import cards, importing
from photo_manager.cards import CardStatus, scan_card
from photo_manager.catalog import Catalog
from photo_manager.config import Settings
from photo_manager.culling import Decision, Verdict
from photo_manager.hashing import content_hash
from photo_manager.importing import (
    ImportFailed,
    execute_import,
    plan_import,
    summarise,
    target_filename,
)
from photo_manager.models import Photo
from photo_manager.paths import directory_snapshot
from photo_manager.sidecars import Sidecar, SidecarError, read_sidecar, sidecar_path


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def never_probe_the_real_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cards, "candidate_volumes", lambda: ())


@pytest.fixture
def card(tmp_path: Path) -> Path:
    folder = tmp_path / "card" / "DCIM" / "100_FUJI"
    folder.mkdir(parents=True)
    return folder


def put(folder: Path, name: str, fixture: Path) -> Path:
    target = folder / name
    shutil.copy2(fixture, target)
    return target


def keep(*indices: int, rating: int = 0, raw: bool = False) -> dict[int, Decision]:
    return {
        index: Decision(verdict=Verdict.KEEP, rating=rating, keep_raw=raw)
        for index in indices
    }


@pytest.fixture
def catalog(settings: Settings) -> Iterator[Catalog]:
    with Catalog.open(settings) as open_catalog:
        yield open_catalog


def scan(settings: Settings):  # type: ignore[no-untyped-def]
    result = scan_card(settings)
    assert result.location.status == CardStatus.FOUND, result.location.message
    return result


# ----------------------------------------------------------------------
# Naming — D-7 and FR-6.3
# ----------------------------------------------------------------------


def test_a_name_carries_the_date_the_place_and_the_shutter_number() -> None:
    assert target_filename(
        dt.datetime(2026, 9, 6, 11, 6, 51), "DSCF1024.JPG", location_slug="st-aldo",
    ) == "2026-09-06_st-aldo_DSCF1024.jpg"


def test_no_location_falls_back_rather_than_blocking_the_import() -> None:
    """FR-6.3 says so in as many words, and today it is the only path there is:
    locations do not exist until WU-3.2, so every real import takes it."""
    assert target_filename(
        dt.datetime(2026, 9, 6, 11, 6, 51), "DSCF1024.JPG",
    ) == "2026-09-06_DSCF1024.jpg"


def test_an_empty_location_is_the_same_as_none() -> None:
    assert target_filename(
        dt.datetime(2026, 9, 6), "DSCF1.JPG", location_slug="",
    ) == "2026-09-06_DSCF1.jpg"


def test_the_extension_is_lower_cased() -> None:
    """D-7 writes .jpg, the camera writes .JPG, and a folder listing where both
    appear looks like two kinds of file."""
    assert target_filename(dt.datetime(2026, 9, 6), "DSCF1.JPG").endswith(".jpg")
    assert target_filename(
        dt.datetime(2026, 9, 6), "DSCF1.JPG", extension=".RAF",
    ).endswith(".raf")


# ----------------------------------------------------------------------
# Planning
# ----------------------------------------------------------------------


def test_only_keepers_are_planned(settings: Settings, card: Path) -> None:
    put(card, "DSCF0001.JPG", fixtures.BURST_FIRST)
    put(card, "DSCF0002.JPG", fixtures.BURST_SECOND)
    decisions = {0: Decision(verdict=Verdict.KEEP), 1: Decision(verdict=Verdict.SKIP)}

    plan = plan_import(scan(settings), decisions, settings)

    assert [photo.original_name for photo in plan.photos] == ["DSCF0001.JPG"]


def test_an_undecided_frame_is_not_imported_by_accident(
    settings: Settings, card: Path
) -> None:
    put(card, "DSCF0001.JPG", fixtures.BURST_FIRST)
    assert plan_import(scan(settings), {}, settings).photos == ()


def test_a_photo_lands_in_its_capture_date_folder(settings: Settings, card: Path) -> None:
    put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)  # 2026-09-06 11:06:51

    plan = plan_import(scan(settings), keep(0), settings)

    assert plan.photos[0].target == "2026/2026-09/2026-09-06_DSCF1029.jpg"
    assert plan.photos[0].captured_at_inferred is False


def test_a_frame_with_no_exif_date_is_filed_under_its_mtime_and_flagged(
    settings: Settings, card: Path
) -> None:
    """FR-8.2a's rule, applied identically here and in the migration."""
    put(card, "seafront.JPG", fixtures.WITHOUT_CAPTURE_DATE)

    planned = plan_import(scan(settings), keep(0), settings).photos[0]

    assert planned.captured_at_inferred is True
    assert planned.target.startswith(f"{planned.captured_at.year}/")


def test_the_raf_is_planned_only_when_it_was_flagged(
    settings: Settings, card: Path
) -> None:
    """D-2 is opt-in per photo, which is the whole of the decision."""
    put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    put(card, "DSCF0807.RAF", fixtures.RAW_PAIR_RAF)

    without = plan_import(scan(settings), keep(0), settings).photos[0]
    with_raw = plan_import(scan(settings), keep(0, raw=True), settings).photos[0]

    assert without.raw_source is None and without.raw_target is None
    assert with_raw.raw_target == "2026/2026-07/2026-07-13_DSCF0807.raf"
    assert with_raw.total_bytes > without.total_bytes


def test_a_pair_keeps_one_basename_so_it_stays_pairable(
    settings: Settings, card: Path
) -> None:
    put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    put(card, "DSCF0807.RAF", fixtures.RAW_PAIR_RAF)

    planned = plan_import(scan(settings), keep(0, raw=True), settings).photos[0]

    assert planned.raw_target is not None
    assert Path(planned.target).stem == Path(planned.raw_target).stem


def test_an_unreadable_keeper_is_reported_rather_than_guessed_at(
    settings: Settings, card: Path
) -> None:
    (card / "DSCF0001.JPG").write_bytes(b"\xff\xd8 not a photograph")
    put(card, "DSCF0002.JPG", fixtures.BURST_SECOND)

    plan = plan_import(scan(settings), keep(0, 1), settings)

    assert len(plan.photos) == 1
    assert [entry.name for entry in plan.unplannable] == ["DSCF0001.JPG"]


def test_the_plan_measures_free_space_rather_than_assuming_it(
    settings: Settings, card: Path
) -> None:
    put(card, "DSCF0001.JPG", fixtures.BURST_FIRST)
    plan = plan_import(scan(settings), keep(0), settings)
    assert plan.free_bytes == shutil.disk_usage(settings.library_root).free


def test_replanning_an_unchanged_card_produces_identical_names(
    settings: Settings, card: Path
) -> None:
    """FR-6.5's "deterministically". A name that depends on iteration order is
    a name that changes when nothing did."""
    for index in range(4):
        put(card, f"DSCF000{index}.JPG", fixtures.BURST_FIRST)

    first = plan_import(scan(settings), keep(0, 1, 2, 3), settings)
    again = plan_import(scan(settings), keep(0, 1, 2, 3), settings)

    assert [p.target for p in first.photos] == [p.target for p in again.photos]


# ----------------------------------------------------------------------
# Collisions — FR-6.5
# ----------------------------------------------------------------------


def test_a_name_already_in_the_library_is_suffixed(
    settings: Settings, card: Path
) -> None:
    put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)
    occupied = settings.library_root / "2026" / "2026-09"
    occupied.mkdir(parents=True)
    (occupied / "2026-09-06_DSCF1029.jpg").write_bytes(b"someone got here first")

    planned = plan_import(scan(settings), keep(0), settings).photos[0]

    assert planned.target == "2026/2026-09/2026-09-06_DSCF1029_1.jpg"
    assert planned.collided is True


def test_two_frames_wanting_one_name_are_both_given_one(
    settings: Settings, card: Path
) -> None:
    """Same shutter number, same day, two different frames — which is what a
    second camera body or a rolled-over counter produces."""
    put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)
    second = card.parent / "101_FUJI"
    second.mkdir()
    put(second, "DSCF1029.JPG", fixtures.BURST_SECOND)

    targets = [p.target for p in plan_import(scan(settings), keep(0, 1), settings).photos]

    assert len(set(targets)) == 2
    assert any(target.endswith("_1.jpg") for target in targets)


def test_a_colliding_pair_is_suffixed_as_a_unit(settings: Settings, card: Path) -> None:
    put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    put(card, "DSCF0807.RAF", fixtures.RAW_PAIR_RAF)
    occupied = settings.library_root / "2026" / "2026-07"
    occupied.mkdir(parents=True)
    (occupied / "2026-07-13_DSCF0807.jpg").write_bytes(b"taken")

    planned = plan_import(scan(settings), keep(0, raw=True), settings).photos[0]

    assert planned.raw_target is not None
    assert planned.target.endswith("_1.jpg")
    assert planned.raw_target.endswith("_1.raf")


# ----------------------------------------------------------------------
# Executing — the bytes
# ----------------------------------------------------------------------


def test_the_imported_bytes_are_identical_to_the_card(
    settings: Settings, card: Path, catalog: Catalog
) -> None:
    source = put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)
    plan = plan_import(scan(settings), keep(0), settings)

    outcome = execute_import(plan, catalog)

    landed = settings.library_root / plan.photos[0].target
    assert landed.is_file()
    assert content_hash(landed) == content_hash(source)
    assert outcome.imported[0].content_hash == content_hash(source)


def test_a_row_is_written_with_the_metadata_and_the_rating(
    settings: Settings, card: Path, catalog: Catalog
) -> None:
    put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)
    plan = plan_import(scan(settings), keep(0, rating=4), settings)

    execute_import(plan, catalog)

    row = catalog.photos.all()[0]
    assert row.library_path == plan.photos[0].target
    assert row.original_name == "DSCF1029.JPG"
    assert row.camera_model == fixtures.CAMERA_MODEL
    assert row.rating == 4
    assert row.captured_at == fixtures.CAPTURE_TIMES["DSCF1029.JPG"]
    assert catalog.absolute_path(row).is_file()


def test_a_flagged_raf_travels_with_its_jpeg(
    settings: Settings, card: Path, catalog: Catalog
) -> None:
    put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    raw_source = put(card, "DSCF0807.RAF", fixtures.RAW_PAIR_RAF)
    plan = plan_import(scan(settings), keep(0, raw=True), settings)

    execute_import(plan, catalog)

    row = catalog.photos.all()[0]
    assert row.has_raw is True
    raw_path = catalog.absolute_raw_path(row)
    assert raw_path is not None and raw_path.is_file()
    assert content_hash(raw_path) == content_hash(raw_source)


def test_an_unflagged_raf_is_left_on_the_card(
    settings: Settings, card: Path, catalog: Catalog
) -> None:
    put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    put(card, "DSCF0807.RAF", fixtures.RAW_PAIR_RAF)

    execute_import(plan_import(scan(settings), keep(0), settings), catalog)

    row = catalog.photos.all()[0]
    assert row.has_raw is False
    assert not list(settings.library_root.rglob("*.raf"))


def test_the_originals_on_the_card_are_not_touched(
    settings: Settings, card: Path, catalog: Catalog
) -> None:
    """§4.1, and this is the first operation that reads every byte of the card."""
    put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    put(card, "DSCF0807.RAF", fixtures.RAW_PAIR_RAF)
    put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)
    volume = card.parent.parent
    before = directory_snapshot(volume)

    execute_import(plan_import(scan(settings), keep(0, 1, raw=True), settings), catalog)

    assert directory_snapshot(volume) == before


# ----------------------------------------------------------------------
# Sidecars — D-1's half of an import
# ----------------------------------------------------------------------


def test_an_imported_photograph_gets_a_sidecar_carrying_its_rating(
    settings: Settings, card: Path, catalog: Catalog
) -> None:
    """Until Phase 3, the cull's rating exists nowhere but the catalog — and
    this project calls the catalog rebuildable (NFR-2). A rating a rebuild
    would lose is a rating that is not really saved."""
    put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)
    plan = plan_import(scan(settings), keep(0, rating=4), settings)

    execute_import(plan, catalog)

    landed = settings.library_root / plan.photos[0].target
    assert read_sidecar(landed) == Sidecar(rating=4)


def test_both_halves_of_a_pair_get_a_sidecar(
    settings: Settings, card: Path, catalog: Catalog
) -> None:
    """§5.2 keeps the whole filename so a JPEG and its RAF do not collide on
    one sidecar, which is only worth solving if both can have one. Someone
    opening the RAF elsewhere should find the rating on it."""
    put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    put(card, "DSCF0807.RAF", fixtures.RAW_PAIR_RAF)
    plan = plan_import(scan(settings), keep(0, rating=5, raw=True), settings)

    execute_import(plan, catalog)

    planned = plan.photos[0]
    assert planned.raw_target is not None
    assert read_sidecar(settings.library_root / planned.target) == Sidecar(rating=5)
    assert read_sidecar(settings.library_root / planned.raw_target) == Sidecar(rating=5)


def test_an_unrated_keeper_gets_no_sidecar_saying_nothing(
    settings: Settings, card: Path, catalog: Catalog
) -> None:
    """A sidecar whose whole content is "unrated" carries no information, and
    one per keeper would double the file count in every folder — against
    success criterion 5, which is about Explorer becoming more useful."""
    put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)

    execute_import(plan_import(scan(settings), keep(0), settings), catalog)

    assert len(list(settings.library_root.rglob("*.jpg"))) == 1
    assert list(settings.library_root.rglob("*.xmp")) == []


def test_an_unflagged_raf_leaves_no_sidecar_for_a_file_that_is_not_there(
    settings: Settings, card: Path, catalog: Catalog
) -> None:
    put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    put(card, "DSCF0807.RAF", fixtures.RAW_PAIR_RAF)

    execute_import(plan_import(scan(settings), keep(0, rating=3), settings), catalog)

    assert len(list(settings.library_root.rglob("*.xmp"))) == 1


def test_a_sidecar_that_cannot_be_written_fails_the_photograph(
    settings: Settings, card: Path, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-5.6, at its sharpest. A photograph in the library whose tags did not
    persist looks saved and is not, so it does not go in at all."""
    put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)
    plan = plan_import(scan(settings), keep(0, rating=3), settings)

    def refuse(path, sidecar):  # type: ignore[no-untyped-def]
        raise SidecarError("the sidecar could not be written")

    monkeypatch.setattr(importing, "write_sidecar", refuse)
    outcome = execute_import(plan, catalog)

    assert len(outcome.failed) == 1
    assert "sidecar" in outcome.failed[0].reason
    assert catalog.photos.count() == 0
    assert list(settings.library_root.rglob("*.jpg")) == []


def test_an_imported_sidecar_is_readable_by_exiv2(
    settings: Settings, card: Path, catalog: Catalog
) -> None:
    """The end of D-1's chain: off the card, into the library, and legible to
    software that has never heard of this application."""
    pyexiv2 = pytest.importorskip("pyexiv2")
    put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)
    plan = plan_import(scan(settings), keep(0, rating=5), settings)

    execute_import(plan, catalog)

    landed = settings.library_root / plan.photos[0].target
    with pyexiv2.Image(str(sidecar_path(landed))) as image:
        assert image.read_xmp()["Xmp.xmp.Rating"] == "5"


# ----------------------------------------------------------------------
# Executing — failure, which is the acceptance criterion
# ----------------------------------------------------------------------


def test_a_failure_mid_copy_leaves_no_half_file_and_no_row(
    settings: Settings, card: Path, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion, by injection rather than by argument."""
    put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)
    plan = plan_import(scan(settings), keep(0), settings)

    real_replace = importing.os.replace

    def fail_once(source, destination):  # type: ignore[no-untyped-def]
        raise OSError("the disk went away mid-copy")

    monkeypatch.setattr(importing.os, "replace", fail_once)
    outcome = execute_import(plan, catalog)
    monkeypatch.setattr(importing.os, "replace", real_replace)

    assert len(outcome.failed) == 1
    assert catalog.photos.count() == 0
    # Neither the finished name nor the temporary one is left behind — and
    # nor is a sidecar, which would be a tag file for a photograph that is not
    # in the library.
    assert list(settings.library_root.rglob("*.jpg")) == []
    assert list(settings.library_root.rglob("*.partial")) == []
    assert list(settings.library_root.rglob("*.xmp")) == []


def test_a_copy_that_does_not_verify_is_not_committed(
    settings: Settings, card: Path, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-6.4. If the bytes on disk are not the bytes that were read, the
    photograph is not in the library, whatever the filesystem thinks."""
    put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)
    plan = plan_import(scan(settings), keep(0), settings)

    monkeypatch.setattr(importing, "content_hash", lambda path: "0" * 64)
    outcome = execute_import(plan, catalog)

    assert len(outcome.failed) == 1
    assert "not the original" in outcome.failed[0].reason
    assert catalog.photos.count() == 0
    assert list(settings.library_root.rglob("*.jpg")) == []


def test_a_failed_raf_takes_its_jpeg_back_out_with_it(
    settings: Settings, card: Path, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A photograph is one transaction. Half of one in the library, with no row
    pointing at it, is an orphan nothing will ever find."""
    put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    raw = put(card, "DSCF0807.RAF", fixtures.RAW_PAIR_RAF)
    plan = plan_import(scan(settings), keep(0, raw=True), settings)
    raw.unlink()  # the RAF vanishes between planning and copying

    outcome = execute_import(plan, catalog)

    assert len(outcome.failed) == 1
    assert catalog.photos.count() == 0
    assert list(settings.library_root.rglob("*.jpg")) == []
    assert list(settings.library_root.rglob("*.raf")) == []
    assert list(settings.library_root.rglob("*.xmp")) == []


def test_one_bad_frame_does_not_stop_the_others(
    settings: Settings, card: Path, catalog: Catalog
) -> None:
    """FR-1.3c, and the deliberate difference from the migration: nothing has
    been consumed, so pressing on is safe and stopping would be wasteful."""
    # Three *different* photographs. BURST_FIRST and JPEG_WITHOUT_RAW are the
    # same fixture file, so using both would make the third a real duplicate
    # and this test would be measuring dedup instead.
    put(card, "DSCF0001.JPG", fixtures.BURST_FIRST)
    put(card, "DSCF0002.JPG", fixtures.BURST_SECOND)
    put(card, "DSCF0003.JPG", fixtures.SPLIT_SECOND)
    plan = plan_import(scan(settings), keep(0, 1, 2), settings)

    # Break the one in the *middle of the plan*, not the middle of the folder.
    # The plan is ordered by capture time, so removing the last one to be
    # attempted would pass whether the loop carried on or stopped dead — which
    # is exactly what this test would then fail to notice.
    assert len(plan.photos) == 3
    doomed = plan.photos[1]
    doomed.source.unlink()

    outcome = execute_import(plan, catalog)

    assert [entry.planned.original_name for entry in outcome.failed] == [
        doomed.original_name
    ]
    assert len(outcome.imported) == 2, "the import stopped at the bad frame"
    assert catalog.photos.count() == 2


def test_the_import_refuses_to_start_if_it_would_fill_the_volume(
    settings: Settings, card: Path, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-4.5, at the point it actually matters, and re-measured here rather
    than trusted from a plan that may be ten minutes old."""
    put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)
    plan = plan_import(scan(settings), keep(0), settings)

    monkeypatch.setattr(
        importing.shutil, "disk_usage",
        lambda path: shutil._ntuple_diskusage(total=1000, used=0, free=1000),
    )
    with pytest.raises(ImportFailed, match="gigabyte spare"):
        execute_import(plan, catalog)

    assert catalog.photos.count() == 0
    assert list(settings.library_root.rglob("*.jpg")) == []


# ----------------------------------------------------------------------
# Duplicates — C-3, FR-6.6
# ----------------------------------------------------------------------


def test_a_photo_already_in_the_library_is_skipped_not_imported_twice(
    settings: Settings, card: Path, catalog: Catalog
) -> None:
    source = put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)
    catalog.photos.add(Photo(
        content_hash=content_hash(source),
        library_path="2026/2026-09-06/already-here.jpg",
        original_name="DSCF1029.JPG",
        captured_at=dt.datetime(2026, 9, 6, 11, 6, 51),
        imported_at=dt.datetime(2026, 9, 20),
    ))

    outcome = execute_import(plan_import(scan(settings), keep(0), settings), catalog)

    assert len(outcome.imported) == 0
    assert len(outcome.skipped) == 1
    assert outcome.skipped[0].existing_path == "2026/2026-09-06/already-here.jpg"
    assert catalog.photos.count() == 1


def test_a_skipped_duplicate_leaves_no_copy_behind(
    settings: Settings, card: Path, catalog: Catalog
) -> None:
    """It is copied before it is known to be a duplicate — the hash is what
    settles it and the hash costs the read — so the copy has to go."""
    source = put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)
    catalog.photos.add(Photo(
        content_hash=content_hash(source),
        library_path="2026/2026-09-06/already-here.jpg",
        original_name="DSCF1029.JPG",
        captured_at=dt.datetime(2026, 9, 6, 11, 6, 51),
        imported_at=dt.datetime(2026, 9, 20),
    ))

    execute_import(plan_import(scan(settings), keep(0), settings), catalog)

    assert list(settings.library_root.rglob("*.jpg")) == []
    assert list(settings.library_root.rglob("*.partial")) == []
    assert list(settings.library_root.rglob("*.xmp")) == []


def test_a_duplicate_does_not_cost_its_raf_as_well(
    settings: Settings, card: Path, catalog: Catalog
) -> None:
    """The JPEG settles the question first, so a 30 MB RAF is never read for a
    photograph that was already in the library."""
    source = put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    put(card, "DSCF0807.RAF", fixtures.RAW_PAIR_RAF)
    catalog.photos.add(Photo(
        content_hash=content_hash(source),
        library_path="2026/2026-07-13/already-here.jpg",
        original_name="DSCF0807.JPG",
        captured_at=dt.datetime(2026, 7, 13, 12, 50, 56),
        imported_at=dt.datetime(2026, 9, 20),
    ))

    execute_import(plan_import(scan(settings), keep(0, raw=True), settings), catalog)

    assert list(settings.library_root.rglob("*.raf")) == []


def test_importing_the_same_card_twice_adds_nothing_the_second_time(
    settings: Settings, card: Path, catalog: Catalog
) -> None:
    """Success criterion 2, from the other end: not just offered zero, but
    importing zero even if something did offer them."""
    put(card, "DSCF0001.JPG", fixtures.BURST_FIRST)
    put(card, "DSCF0002.JPG", fixtures.BURST_SECOND)

    first = execute_import(plan_import(scan(settings), keep(0, 1), settings), catalog)
    second = execute_import(plan_import(scan(settings), keep(0, 1), settings), catalog)

    assert len(first.imported) == 2
    assert len(second.imported) == 0
    assert len(second.skipped) == 2
    assert catalog.photos.count() == 2


# ----------------------------------------------------------------------
# Saying what happened — FR-6.6
# ----------------------------------------------------------------------


def test_the_summary_names_all_three_outcomes(
    settings: Settings, card: Path, catalog: Catalog
) -> None:
    source = put(card, "DSCF0001.JPG", fixtures.BURST_FIRST)
    put(card, "DSCF0002.JPG", fixtures.BURST_SECOND)
    put(card, "DSCF0003.JPG", fixtures.SPLIT_SECOND)
    catalog.photos.add(Photo(
        content_hash=content_hash(source),
        library_path="2026/2026-09-06/already-here.jpg",
        original_name="DSCF0001.JPG",
        captured_at=dt.datetime(2026, 9, 6, 11, 6, 51),
        imported_at=dt.datetime(2026, 9, 20),
    ))
    plan = plan_import(scan(settings), keep(0, 1, 2), settings)
    (card / "DSCF0003.JPG").unlink()

    text = summarise(execute_import(plan, catalog))

    assert "Imported 1" in text
    assert "Skipped 1" in text
    assert "FAILED 1" in text
    assert text.isascii()


def test_an_empty_import_says_so_without_failing(
    settings: Settings, card: Path, catalog: Catalog
) -> None:
    outcome = execute_import(plan_import(scan(settings), {}, settings), catalog)
    assert outcome.complete is True
    assert "Imported 0" in summarise(outcome)
