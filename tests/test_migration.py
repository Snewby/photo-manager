"""The dry-run migration planner.

Two things carry most of the weight here. The first is that **nothing moves**:
several tests snapshot the library before and after and compare, because the
unit's whole promise is that it can be pointed at hundreds of irreplaceable photos
safely. The second is the awkward-file handling the real library forced —
a file with no capture date, a crop of another frame, a RAF whose JPEG partner
differs in case — since those are precisely what the manifest exists to
surface.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
from dataclasses import replace
from pathlib import Path

import fixtures
import pytest
from PIL import Image

from photo_manager.catalog import Catalog
from photo_manager.config import Settings
from photo_manager.hashing import content_hash
from photo_manager.migration import (
    MANIFEST_VERSION,
    REVERSAL_VERSION,
    MigrationError,
    MigrationPlan,
    NoteKind,
    OverrideError,
    directory_snapshot,
    execute_migration,
    load_overrides,
    parse_name_hint,
    plan_migration,
    read_reversal_log,
    reverse_migration,
    summarise,
    target_directory,
    write_manifest,
    write_report,
)
from photo_manager.sidecars import read_sidecar

FIXED_NOW = dt.datetime(2026, 9, 20, 16, 0, 0)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def stock(library_root: Path, *names: str) -> Path:
    """Copy fixture photos into a throwaway library root, flat."""
    for name in names:
        shutil.copy2(fixtures.PHOTO_DIR / name, library_root / name)
    return library_root


def plan_for(library_root: Path, tmp_path: Path) -> MigrationPlan:
    settings = Settings(library_root=library_root, state_dir=tmp_path / "state")
    return plan_migration(settings, now=FIXED_NOW)


def by_name(plan: MigrationPlan, name: str):
    matches = [p for p in plan.photos if p.source.name == name]
    assert matches, f"{name} is not in the plan: {[p.source.name for p in plan.photos]}"
    assert len(matches) == 1
    return matches[0]


def set_mtime(path: Path, when: dt.datetime) -> None:
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))


def write_bare_jpeg(path: Path, size: tuple[int, int] = (64, 48)) -> Path:
    """A JPEG with no EXIF at all — no date, no lens, no exposure.

    The real ``Birthday_1.jpg`` is like this, which is why the name is
    the only signal left that connects it to anything.
    """
    Image.new("RGB", size, "grey").save(path, "JPEG")
    return path


# ----------------------------------------------------------------------
# Names (FR-8.2)
# ----------------------------------------------------------------------


@pytest.mark.parametrize("stem", ["DSCF0807", "DSCF1029", "dscf1234", "DSCF0000"])
def test_camera_written_names_carry_no_hint(stem: str) -> None:
    assert parse_name_hint(stem) is None


@pytest.mark.parametrize(
    ("stem", "phrase"),
    [
        ("birthday", "Birthday"),
        ("family", "Family"),
        ("seafront", "Seafront"),
        ("wall", "Wall"),
        ("Birthday", "Birthday"),
        ("st-aldo beach", "St Aldo Beach"),
    ],
)
def test_a_hand_given_name_becomes_a_readable_phrase(stem: str, phrase: str) -> None:
    hint = parse_name_hint(stem)
    assert hint is not None
    assert hint.phrase == phrase


def test_the_hint_does_not_guess_which_field_the_name_belongs_to() -> None:
    """Deliberate. 'St Aldo' is a place and 'Birthday' is a pub, and no
    rule tells them apart — so the manifest asks rather than assumes."""
    hint = parse_name_hint("seafront")
    assert hint is not None
    assert hint.as_dict()["suggested_location"] == ""
    assert hint.as_dict()["suggested_event"] == ""


@pytest.mark.parametrize(
    ("stem", "base", "marker"),
    [
        ("Birthday_1", "birthday", "1"),
        ("wall-2", "wall", "2"),
        ("holiday (3)", "holiday", "3"),
        ("family", "family", None),
    ],
)
def test_copy_markers_are_stripped_back_to_the_original_name(
    stem: str, base: str, marker: str | None
) -> None:
    hint = parse_name_hint(stem)
    assert hint is not None
    assert hint.base == base
    assert hint.copy_marker == marker


def test_a_nameless_stem_yields_no_hint() -> None:
    assert parse_name_hint("") is None
    assert parse_name_hint("___") is None


def test_target_directory_follows_d4() -> None:
    assert target_directory(dt.datetime(2026, 9, 6, 11, 6, 51)) == "2026/2026-09"
    assert target_directory(dt.datetime(2025, 1, 2, 0, 0, 0)) == "2025/2025-01"


# ----------------------------------------------------------------------
# The plan
# ----------------------------------------------------------------------


@pytest.fixture
def stocked(library_root: Path) -> Path:
    """A flat library holding every readable fixture, like the real one.

    Function scoped, so a test may add to it or change a timestamp.
    """
    return stock(library_root, *(p.name for p in fixtures.ALL_PHOTOS))


@pytest.fixture(scope="module")
def stocked_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One stocked library shared by every test that only reads the plan.

    Planning it means hashing 49 MB of fixtures, 47 MB of which is the RAW
    pair. Doing that once instead of per test keeps the standing "run the whole
    suite every session" rule (EXECUTION_PLAN.md §4.3) cheap enough to obey.
    Anything that mutates the library uses ``stocked`` instead.
    """
    root = tmp_path_factory.mktemp("stocked-library")
    return stock(root, *(p.name for p in fixtures.ALL_PHOTOS))


@pytest.fixture(scope="module")
def stocked_settings(stocked_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Settings:
    return Settings(library_root=stocked_root, state_dir=tmp_path_factory.mktemp("state"))


@pytest.fixture(scope="module")
def stocked_plan(stocked_settings: Settings) -> MigrationPlan:
    return plan_migration(stocked_settings, now=FIXED_NOW)


def test_every_file_appears_exactly_once(stocked_plan: MigrationPlan) -> None:
    plan = stocked_plan
    moved = [source.name for photo in plan.photos for source in photo.sources]
    assert sorted(moved) == sorted(p.name for p in fixtures.ALL_PHOTOS)
    assert plan.file_count == len(fixtures.ALL_PHOTOS)


def test_targets_are_relative_forward_slashed_and_dated(
    stocked_plan: MigrationPlan,
) -> None:
    for photo in stocked_plan.photos:
        assert not Path(photo.target).is_absolute()
        assert "\\" not in photo.target
        year, day, name = photo.target.split("/")
        assert day.startswith(year)
        assert name == photo.source.name
        assert target_directory(photo.captured_at) == f"{year}/{day}"


def test_a_photo_is_filed_under_its_exif_capture_date(
    stocked_plan: MigrationPlan,
) -> None:
    photo = by_name(stocked_plan, "DSCF1029.JPG")
    assert photo.captured_at == fixtures.CAPTURE_TIMES["DSCF1029.JPG"]
    assert photo.captured_at_inferred is False
    assert photo.target == "2026/2026-09/DSCF1029.JPG"


def test_the_stored_target_is_a_valid_library_path(
    stocked_plan: MigrationPlan, stocked_settings: Settings
) -> None:
    """The catalog's CHECK constraints reject anything absolute or escaping,
    so the planner's targets have to survive a round trip through Settings."""
    for photo in stocked_plan.photos:
        resolved = stocked_settings.resolve(photo.target)
        assert stocked_settings.relativise(resolved) == photo.target


# -- pairing (FR-1.2) --------------------------------------------------


def test_a_raf_pairs_with_its_jpeg(stocked_plan: MigrationPlan) -> None:
    plan = stocked_plan
    photo = by_name(plan, "DSCF0807.JPG")
    assert photo.raw_source is not None
    assert photo.raw_source.name == "DSCF0807.RAF"
    assert photo.raw_target == "2026/2026-07/DSCF0807.RAF"
    assert photo.raw_content_hash and photo.raw_content_hash != photo.content_hash
    assert plan.raw_pair_count == 1


def test_pairing_ignores_case(library_root: Path, tmp_path: Path) -> None:
    """The real library holds Birthday.RAF beside birthday.JPG."""
    stock(library_root, "birthday.JPG", "DSCF0807.RAF")
    (library_root / "DSCF0807.RAF").rename(library_root / "Birthday.RAF")

    plan = plan_for(library_root, tmp_path)
    assert len(plan.photos) == 1
    photo = plan.photos[0]
    assert photo.source.name == "birthday.JPG"
    assert photo.raw_source is not None
    assert photo.raw_source.name == "Birthday.RAF"


def test_a_pair_lands_in_one_folder(stocked_plan: MigrationPlan) -> None:
    photo = by_name(stocked_plan, "DSCF0807.JPG")
    assert photo.raw_target is not None
    assert Path(photo.target).parent == Path(photo.raw_target).parent


def test_a_lone_raf_still_moves_but_is_flagged(
    library_root: Path, tmp_path: Path
) -> None:
    stock(library_root, "DSCF0807.RAF")
    plan = plan_for(library_root, tmp_path)
    assert len(plan.photos) == 1
    photo = plan.photos[0]
    assert photo.source.name == "DSCF0807.RAF"
    assert photo.target == "2026/2026-07/DSCF0807.RAF"
    assert NoteKind.UNPAIRED_RAW in photo.note_kinds()


def test_an_ambiguous_basename_is_flagged_rather_than_guessed(
    library_root: Path, tmp_path: Path
) -> None:
    """Two JPEGs and a RAF sharing a stem: which pairs with which is not
    knowable, so all three are planned separately and said so."""
    stock(library_root, "DSCF0807.JPG", "DSCF0807.RAF")
    shutil.copy2(fixtures.PHOTO_DIR / "DSCF1029.JPG", library_root / "dscf0807.jpeg")

    plan = plan_for(library_root, tmp_path)
    assert len(plan.photos) == 3
    for photo in plan.photos:
        assert NoteKind.AMBIGUOUS_PAIR in photo.note_kinds()
        assert photo.raw_source is None


# -- dates (FR-8.2a) ---------------------------------------------------


def test_a_file_with_no_exif_date_falls_back_to_mtime_and_is_flagged(
    stocked: Path, tmp_path: Path
) -> None:
    when = dt.datetime(2026, 7, 13, 21, 37, 35)
    set_mtime(stocked / "seafront.JPG", when)

    photo = by_name(plan_for(stocked, tmp_path), "seafront.JPG")
    assert photo.captured_at == when
    assert photo.captured_at_inferred is True
    assert photo.target == "2026/2026-07/seafront.JPG"
    assert NoteKind.INFERRED_DATE in photo.note_kinds()


def test_an_inferred_date_keeps_exifs_resolution(
    stocked: Path, tmp_path: Path
) -> None:
    """mtime has microseconds and EXIF does not. Carrying them into the catalog
    would make timestamps inconsistent and imply accuracy that isn't there."""
    os.utime(stocked / "seafront.JPG", (1_781_000_000.577041, 1_781_000_000.577041))
    photo = by_name(plan_for(stocked, tmp_path), "seafront.JPG")
    assert photo.captured_at.microsecond == 0


def test_the_inferred_flag_is_visible_in_the_manifest_and_the_report(
    stocked_plan: MigrationPlan,
) -> None:
    """FR-8.2a's whole point: the guess must be *seen*, not just recorded."""
    plan = stocked_plan
    entry = next(p for p in plan.to_manifest()["photos"] if p["source"] == "seafront.JPG")
    assert entry["captured_at_inferred"] is True

    report = plan.render_report()
    assert NoteKind.INFERRED_DATE in report
    assert "seafront.JPG" in report


def test_a_pair_takes_its_date_from_the_raf_when_the_jpeg_has_none(
    library_root: Path, tmp_path: Path
) -> None:
    """Not FR-8.2a's mtime fallback — a pair is one frame, so the RAF's EXIF
    is that frame's real capture time and nothing is being guessed."""
    stock(library_root, "DSCF0807.RAF")
    write_bare_jpeg(library_root / "DSCF0807.JPG")
    set_mtime(library_root / "DSCF0807.JPG", dt.datetime(2020, 1, 1, 0, 0, 0))

    photo = plan_for(library_root, tmp_path).photos[0]
    assert photo.captured_at == fixtures.RAW_PAIR_CAPTURE_TIME
    assert photo.captured_at_inferred is False
    assert NoteKind.DATE_FROM_RAW in photo.note_kinds()
    assert NoteKind.INFERRED_DATE not in photo.note_kinds()


def test_a_pair_disagreeing_about_its_capture_time_is_flagged(
    library_root: Path, tmp_path: Path
) -> None:
    stock(library_root, "DSCF0807.JPG", "DSCF0807.RAF")
    shutil.copy2(fixtures.PHOTO_DIR / "DSCF1029.JPG", library_root / "DSCF0807.JPG")

    photo = plan_for(library_root, tmp_path).photos[0]
    assert NoteKind.RAW_DATE_DISAGREES in photo.note_kinds()
    # The JPEG wins — it is the file the catalog row is about.
    assert photo.captured_at == fixtures.CAPTURE_TIMES["DSCF1029.JPG"]


# -- collisions (FR-6.5) -----------------------------------------------


def test_a_target_that_already_exists_gets_a_deterministic_suffix(
    library_root: Path, tmp_path: Path
) -> None:
    stock(library_root, "DSCF1029.JPG")
    occupied = library_root / "2026" / "2026-09"
    occupied.mkdir(parents=True)
    (occupied / "DSCF1029.JPG").write_bytes(b"something else entirely")

    photo = plan_for(library_root, tmp_path).photos[0]
    assert photo.target == "2026/2026-09/DSCF1029_1.JPG"
    assert NoteKind.COLLISION in photo.note_kinds()


def test_a_colliding_pair_is_suffixed_as_a_unit(
    library_root: Path, tmp_path: Path
) -> None:
    """If only the RAF's name were taken, suffixing just the RAF would break
    the shared basename that FR-1.2's pairing depends on."""
    stock(library_root, "DSCF0807.JPG", "DSCF0807.RAF")
    occupied = library_root / "2026" / "2026-07"
    occupied.mkdir(parents=True)
    (occupied / "DSCF0807.RAF").write_bytes(b"in the way")

    photo = plan_for(library_root, tmp_path).photos[0]
    assert photo.target == "2026/2026-07/DSCF0807_1.JPG"
    assert photo.raw_target == "2026/2026-07/DSCF0807_1.RAF"
    assert Path(photo.target).stem == Path(photo.raw_target).stem


def test_collision_suffixes_do_not_wander_when_the_plan_is_re_run(
    library_root: Path, tmp_path: Path
) -> None:
    stock(library_root, "DSCF1029.JPG", "DSCF1030.JPG")
    occupied = library_root / "2026" / "2026-09"
    occupied.mkdir(parents=True)
    (occupied / "DSCF1029.JPG").write_bytes(b"in the way")

    first = plan_for(library_root, tmp_path).to_manifest()
    second = plan_for(library_root, tmp_path).to_manifest()
    assert first == second


def test_no_collisions_when_nothing_is_in_the_way(
    stocked_plan: MigrationPlan,
) -> None:
    plan = stocked_plan
    assert plan.with_note(NoteKind.COLLISION) == ()
    targets = [p.target.casefold() for p in plan.photos]
    targets += [p.raw_target.casefold() for p in plan.photos if p.raw_target]
    assert len(targets) == len(set(targets))


# -- duplicates and derivatives ----------------------------------------


def test_byte_identical_files_are_flagged(library_root: Path, tmp_path: Path) -> None:
    """content_hash is UNIQUE, so WU-1.5 could only import one of them."""
    stock(library_root, "DSCF1029.JPG")
    shutil.copy2(library_root / "DSCF1029.JPG", library_root / "copy_of_1029.JPG")

    plan = plan_for(library_root, tmp_path)
    flagged = plan.with_note(NoteKind.DUPLICATE_CONTENT)
    assert {p.source.name for p in flagged} == {"DSCF1029.JPG", "copy_of_1029.JPG"}


def test_a_crop_sharing_a_capture_instant_is_flagged_as_a_derivative(
    library_root: Path, tmp_path: Path
) -> None:
    """The real case: wall.JPG is a crop of DSCF0953.JPG, different bytes, so
    no hash will ever catch it. Same EXIF instant is the giveaway."""
    stock(library_root, "DSCF1029.JPG")
    with Image.open(fixtures.PHOTO_DIR / "DSCF1029.JPG") as image:
        exif = image.info.get("exif")
        image.crop((0, 0, image.width // 2, image.height // 2)).save(
            library_root / "cropped.JPG", "JPEG", exif=exif
        )

    plan = plan_for(library_root, tmp_path)
    flagged = {p.source.name for p in plan.with_note(NoteKind.POSSIBLE_DERIVATIVE)}
    assert flagged == {"DSCF1029.JPG", "cropped.JPG"}


def test_a_raw_pair_is_not_mistaken_for_a_derivative(
    stocked_plan: MigrationPlan,
) -> None:
    """A RAF and its JPEG share a capture instant by definition. They are one
    photo, so they must never flag each other."""
    photo = by_name(stocked_plan, "DSCF0807.JPG")
    assert NoteKind.POSSIBLE_DERIVATIVE not in photo.note_kinds()


def test_a_dateless_file_is_matched_to_its_source_by_exposure_settings(
    stocked_plan: MigrationPlan,
) -> None:
    """seafront.JPG has no capture date, so the timestamp check cannot see it —
    but its exposure, aperture, ISO and focal length match exactly one frame."""
    photo = by_name(stocked_plan, "seafront.JPG")
    notes = [n for n in photo.notes if n.kind == NoteKind.POSSIBLE_DERIVATIVE]
    assert notes, "seafront.JPG should be matched to DSCF0807.JPG"
    assert "DSCF0807.JPG" in notes[0].detail


def test_exposure_matching_stays_quiet_when_several_frames_share_settings(
    library_root: Path, tmp_path: Path
) -> None:
    """Burst frames share settings constantly. A match is only evidence when
    it is unique, or the report fills with noise and stops being read.

    The contrast with the test above is the whole point: the same dateless
    seafront.JPG, and a second frame carrying the settings it matched, is enough
    to make the planner hold its tongue.
    """
    stock(library_root, "DSCF0807.JPG", "seafront.JPG")
    shutil.copy2(library_root / "DSCF0807.JPG", library_root / "DSCF0900.JPG")

    photo = by_name(plan_for(library_root, tmp_path), "seafront.JPG")
    assert NoteKind.POSSIBLE_DERIVATIVE not in photo.note_kinds()


def test_a_file_with_no_exif_at_all_is_linked_by_its_name(
    library_root: Path, tmp_path: Path
) -> None:
    """The real Birthday_1.jpg: no date, no lens, no exposure. Its name
    is the only remaining thread back to birthday.JPG."""
    stock(library_root, "birthday.JPG")
    write_bare_jpeg(library_root / "Birthday_1.jpg")

    photo = by_name(plan_for(library_root, tmp_path), "Birthday_1.jpg")
    notes = [n for n in photo.notes if n.kind == NoteKind.POSSIBLE_DERIVATIVE]
    assert notes
    assert "birthday.JPG" in notes[0].detail


def test_a_derivative_filed_away_from_its_source_says_so(
    library_root: Path, tmp_path: Path
) -> None:
    """The real Birthday_1.jpg landed a day from the frame it came from.
    Folders are months since 2026-09-23, so a day apart no longer separates
    them — a month apart still does, and a reader should not have to join the
    two facts up to see the problem."""
    stock(library_root, "birthday.JPG")
    write_bare_jpeg(library_root / "Birthday_1.jpg")
    set_mtime(library_root / "Birthday_1.jpg", dt.datetime(2026, 5, 3, 9, 7, 54))

    photo = by_name(plan_for(library_root, tmp_path), "Birthday_1.jpg")
    detail = next(n.detail for n in photo.notes if n.kind == NoteKind.POSSIBLE_DERIVATIVE)
    assert "2026/2026-05" in detail
    assert "2026/2026-04" in detail


def test_a_derivative_in_the_same_folder_is_not_nagged_about(
    library_root: Path, tmp_path: Path
) -> None:
    stock(library_root, "birthday.JPG")
    write_bare_jpeg(library_root / "Birthday_1.jpg")
    set_mtime(library_root / "Birthday_1.jpg", dt.datetime(2026, 4, 2, 23, 0, 0))

    photo = by_name(plan_for(library_root, tmp_path), "Birthday_1.jpg")
    detail = next(n.detail for n in photo.notes if n.kind == NoteKind.POSSIBLE_DERIVATIVE)
    assert "folder apart" not in detail


# -- awkward inputs (FR-1.3c) ------------------------------------------


def test_an_unreadable_file_is_reported_and_does_not_stop_the_rest(
    stocked: Path, tmp_path: Path
) -> None:
    shutil.copy2(fixtures.TRUNCATED, stocked / fixtures.TRUNCATED.name)

    plan = plan_for(stocked, tmp_path)
    assert [u.path.name for u in plan.unreadable] == [fixtures.TRUNCATED.name]
    assert len(plan.photos) == len(fixtures.ALL_PHOTOS) - 1  # the pair is one photo


def test_a_non_image_is_ignored_by_magic_bytes_not_extension(
    stocked: Path, tmp_path: Path
) -> None:
    (stocked / "notes.JPG").write_text("this is not a photograph", encoding="utf-8")

    plan = plan_for(stocked, tmp_path)
    assert [i.path.name for i in plan.ignored] == ["notes.JPG"]
    assert "notes.JPG" not in {p.source.name for p in plan.photos}


def test_an_already_migrated_folder_is_reported_rather_than_descended_into(
    stocked: Path, tmp_path: Path
) -> None:
    (stocked / "2025").mkdir()
    plan = plan_for(stocked, tmp_path)
    assert [i.path.name for i in plan.ignored] == ["2025"]


def test_an_empty_library_plans_nothing_and_does_not_fail(
    library_root: Path, tmp_path: Path
) -> None:
    plan = plan_for(library_root, tmp_path)
    assert plan.photos == ()
    assert plan.needs_review is False
    assert "Nothing flagged" in plan.render_report()


# ----------------------------------------------------------------------
# The dry-run promise
# ----------------------------------------------------------------------


def test_planning_changes_nothing_on_disk(stocked: Path, tmp_path: Path) -> None:
    """The acceptance criterion, stated the way the plan states it."""
    before = directory_snapshot(stocked)
    plan_for(stocked, tmp_path)
    assert directory_snapshot(stocked) == before


def test_planning_creates_no_files_anywhere(stocked: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    settings = Settings(library_root=stocked, state_dir=state)
    plan_migration(settings, now=FIXED_NOW)
    assert not state.exists()


def test_writing_the_manifest_into_the_library_is_refused(
    stocked_plan: MigrationPlan, stocked_root: Path
) -> None:
    """A dry run that wrote into the library would break its own proof."""
    with pytest.raises(ValueError, match="inside the library root"):
        write_manifest(stocked_plan, stocked_root / "manifest.json")
    with pytest.raises(ValueError, match="inside the library root"):
        write_report(stocked_plan, stocked_root / "deeper" / "report.md")


def test_the_manifest_and_report_land_outside_the_library(
    stocked_plan: MigrationPlan, stocked_root: Path, tmp_path: Path
) -> None:
    before = directory_snapshot(stocked_root)

    manifest = write_manifest(stocked_plan, tmp_path / "out" / "manifest.json")
    report = write_report(stocked_plan, tmp_path / "out" / "report.md")

    assert manifest.is_file() and report.is_file()
    assert directory_snapshot(stocked_root) == before


# ----------------------------------------------------------------------
# Manifest and report
# ----------------------------------------------------------------------


def test_the_manifest_is_json_serialisable_and_versioned(
    stocked_plan: MigrationPlan, tmp_path: Path
) -> None:
    plan = stocked_plan
    written = write_manifest(plan, tmp_path / "manifest.json")
    loaded = json.loads(written.read_text(encoding="utf-8"))

    assert loaded["manifest_version"] == MANIFEST_VERSION
    assert loaded["dry_run"] is True
    assert loaded["summary"]["files"] == plan.file_count
    assert len(loaded["photos"]) == len(plan.photos)


def test_the_manifest_maps_old_to_new_for_every_file(
    stocked_plan: MigrationPlan,
) -> None:
    """FR-8.4. A reversal is only possible if every moved file is in here."""
    manifest = stocked_plan.to_manifest()
    mapped = set()
    for entry in manifest["photos"]:
        mapped.add(entry["source"])
        if entry["raw_source"]:
            mapped.add(entry["raw_source"])
    assert mapped == {p.name for p in fixtures.ALL_PHOTOS}


def test_manifest_sources_are_relative_to_the_library_root(
    stocked_plan: MigrationPlan, stocked_root: Path
) -> None:
    for entry in stocked_plan.to_manifest()["photos"]:
        assert not Path(entry["source"]).is_absolute()
        assert str(stocked_root) not in entry["source"]


def test_the_plan_is_reproducible(stocked_settings: Settings) -> None:
    first = plan_migration(stocked_settings, now=FIXED_NOW).to_manifest()
    second = plan_migration(stocked_settings, now=FIXED_NOW).to_manifest()
    assert first == second


def test_the_report_names_every_flagged_photo(stocked_plan: MigrationPlan) -> None:
    plan = stocked_plan
    report = plan.render_report()
    for kind, count in plan.note_counts().items():
        assert kind in report, f"{kind} ({count} photos) is missing from the report"
    for photo in plan.photos:
        if photo.notes:
            assert photo.source.name in report


def test_the_report_explains_what_each_flag_means(
    stocked_plan: MigrationPlan,
) -> None:
    report = stocked_plan.render_report()
    for kind in stocked_plan.note_counts():
        assert NoteKind.EXPLANATIONS[kind] in report


def test_the_report_lists_every_move(stocked_plan: MigrationPlan) -> None:
    report = stocked_plan.render_report()
    for photo in fixtures.ALL_PHOTOS:
        assert photo.name in report


def test_hand_named_files_get_a_section_to_fill_in(
    stocked_plan: MigrationPlan,
) -> None:
    report = stocked_plan.render_report()
    assert "Names to interpret" in report
    assert "Birthday" in report
    assert "Seafront" in report


def test_summarise_is_one_readable_paragraph(stocked_plan: MigrationPlan) -> None:
    text = summarise(stocked_plan)
    assert "photographs" in text
    assert "RAW pairs" in text


def test_needs_review_is_true_when_anything_is_flagged(
    stocked_plan: MigrationPlan,
) -> None:
    assert stocked_plan.needs_review is True


# ----------------------------------------------------------------------
# The real library
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


@pytest.mark.needs_library
def test_the_real_library_has_been_migrated_and_nothing_is_left_flat(
    real_library: Settings, tmp_path: Path
) -> None:
    """WU-1.5 ran on 2026-09-20. This is what it left behind.

    Until then this test asserted the *pre*-migration shape — the flat files —
    and it failed the moment the migration ran, which is exactly what it was
    for. The planner finding nothing to do is itself the proof the migration is
    complete: it scans the flat top level, and there is nothing there.
    """
    root = real_library.library_root
    before = directory_snapshot(root)

    plan = plan_migration(replace(real_library, state_dir=tmp_path / "state"))

    assert directory_snapshot(root) == before, "planning modified the library"
    assert plan.photos == (), "there are still loose files in the library root"
    assert [Path(str(i.path)).name for i in plan.ignored] == ["2026"]


@pytest.mark.needs_library
def test_the_real_catalog_matches_the_real_library(real_library: Settings) -> None:
    """The catalog is an index (NFR-2), and an index that has drifted from what
    it indexes is worse than none. Cheap enough to check on every run.

    This used to assert a fixed number of rows and files, and went red the first time a
    card was imported (WU-2.5) — correctly, but for the wrong reason: the
    library is *supposed* to grow, and a test that has to be edited after every
    import is a test nobody will keep honest. What matters is not how many
    there are but that the two sides agree, which is true at any size.
    """
    with Catalog.open(real_library) as catalog:
        photos = catalog.photos.all()

        assert photos, "the catalog is empty"
        assert len(catalog.photos.hashes()) == len(photos), "two rows share a content hash"

        catalogued: set[Path] = set()
        for photo in photos:
            path = catalog.absolute_path(photo)
            assert path.is_file(), photo.library_path
            assert re.fullmatch(r"\d{4}/\d{4}-\d{2}/.+", photo.library_path)
            catalogued.add(path)
            if photo.has_raw:
                raw = catalog.absolute_raw_path(photo)
                assert raw is not None and raw.is_file()
                catalogued.add(raw)

        # And nothing on disk is missing from it. Sidecars are derived from the
        # rows rather than indexed by them, so they are not photographs here.
        on_disk = {
            path for path in real_library.library_root.rglob("*")
            if path.is_file() and path.suffix.lower() != ".xmp"
        }
        assert on_disk == catalogued, (
            f"{len(on_disk - catalogued)} file(s) in the library have no catalog row, "
            f"{len(catalogued - on_disk)} row(s) point at nothing"
        )


@pytest.mark.needs_library
def test_every_sidecar_in_the_real_library_agrees_with_its_row(
    real_library: Settings,
) -> None:
    """D-1's two stores, checked against each other on the real thing.

    A sidecar that disagrees with the catalog is the failure mode that makes
    "rebuild from sidecars" (success criterion 4) lose or invent data, and it
    would be invisible until the rebuild.
    """
    with Catalog.open(real_library) as catalog:
        for photo in catalog.photos.all():
            path = catalog.absolute_path(photo)
            sidecar = read_sidecar(path)
            if sidecar is None:
                # No sidecar is correct when there is nothing to record.
                assert photo.rating == 0, f"{photo.library_path} is rated but has none"
                continue
            assert sidecar.rating == photo.rating, photo.library_path


@pytest.mark.needs_library
def test_the_awkward_real_files_came_through_intact(real_library: Settings) -> None:
    """Hand-named files and RAW pairs are the ones the whole migration was
    careful about, so they are the ones worth re-hashing."""
    with Catalog.open(real_library) as catalog:
        interesting = [
            p for p in catalog.photos.all()
            if p.has_raw or parse_name_hint(Path(p.original_name).stem) is not None
        ]

        for photo in interesting:
            assert content_hash(catalog.absolute_path(photo)) == photo.content_hash


# ----------------------------------------------------------------------
# Overrides (the manifest review's decisions)
# ----------------------------------------------------------------------


def write_overrides(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "migration-overrides.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_no_override_file_is_not_an_error(tmp_path: Path) -> None:
    assert load_overrides(tmp_path / "absent.toml") == {}


def test_an_override_sets_the_date_and_says_so(
    library_root: Path, tmp_path: Path
) -> None:
    stock(library_root, "seafront.JPG")
    overrides = load_overrides(write_overrides(tmp_path, """
[overrides."seafront.JPG"]
captured_at = 2026-07-13T12:50:56
reason = "Same frame as DSCF0807."
"""))
    settings = Settings(library_root=library_root, state_dir=tmp_path / "state")

    photo = plan_migration(settings, now=FIXED_NOW, overrides=overrides).photos[0]

    assert photo.captured_at == dt.datetime(2026, 7, 13, 12, 50, 56)
    assert photo.target == "2026/2026-07/seafront.JPG"
    assert NoteKind.DATE_OVERRIDDEN in photo.note_kinds()
    detail = next(n.detail for n in photo.notes if n.kind == NoteKind.DATE_OVERRIDDEN)
    assert "Same frame as DSCF0807." in detail


def test_an_overridden_date_is_still_flagged_as_not_from_exif(
    library_root: Path, tmp_path: Path
) -> None:
    """A reviewed decision is better than mtime but it is still not this file's
    EXIF, and Phase 4's "uncertain dates" view should still surface it."""
    stock(library_root, "seafront.JPG")
    overrides = load_overrides(write_overrides(tmp_path, """
[overrides."seafront.JPG"]
captured_at = 2026-07-13T12:50:56
"""))
    settings = Settings(library_root=library_root, state_dir=tmp_path / "state")

    photo = plan_migration(settings, now=FIXED_NOW, overrides=overrides).photos[0]

    assert photo.captured_at_inferred is True


def test_an_override_naming_a_missing_file_stops_the_run(
    library_root: Path, tmp_path: Path
) -> None:
    """A typo that silently does nothing is worse than one that fails: the
    reviewer would believe a decision had been applied when it had not."""
    stock(library_root, "seafront.JPG")
    overrides = load_overrides(write_overrides(tmp_path, """
[overrides."seafrontz.JPG"]
captured_at = 2026-07-13T12:50:56
"""))
    settings = Settings(library_root=library_root, state_dir=tmp_path / "state")

    with pytest.raises(OverrideError, match="seafrontz.JPG"):
        plan_migration(settings, now=FIXED_NOW, overrides=overrides)


def test_override_matching_ignores_case(library_root: Path, tmp_path: Path) -> None:
    stock(library_root, "seafront.JPG")
    overrides = load_overrides(write_overrides(tmp_path, """
[overrides."SEAFRONT.jpg"]
captured_at = 2026-07-13T12:50:56
"""))
    settings = Settings(library_root=library_root, state_dir=tmp_path / "state")

    photo = plan_migration(settings, now=FIXED_NOW, overrides=overrides).photos[0]
    assert photo.captured_at == dt.datetime(2026, 7, 13, 12, 50, 56)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('[overrides."a.JPG"]\ncaptured_at = "not a date"\n', "TOML date-time"),
        ('[overrides."a.JPG"]\nnonsense = 1\n', "unknown key"),
        ("this is not toml = = =\n", "not valid TOML"),
    ],
)
def test_a_malformed_override_file_is_refused(
    tmp_path: Path, body: str, match: str
) -> None:
    with pytest.raises(OverrideError, match=match):
        load_overrides(write_overrides(tmp_path, body))


def test_the_owners_override_file_loads_and_says_why() -> None:
    """``private/migration-overrides.toml`` records a migration review's
    decisions. If it stops parsing, a decision silently stops being applied.

    It is the library owner's, so it is not in the public repository, and this
    is skipped wherever it is absent.
    """
    path = Path(__file__).resolve().parents[1] / "private" / "migration-overrides.toml"
    if not path.is_file():
        pytest.skip("no private/migration-overrides.toml on this machine")
    overrides = load_overrides(path)

    assert overrides
    for override in overrides.values():
        assert override.captured_at is not None
        assert override.reason


# ----------------------------------------------------------------------
# Executing (WU-1.5)
# ----------------------------------------------------------------------


@pytest.fixture
def executable(library_root: Path, tmp_path: Path) -> tuple[Settings, MigrationPlan]:
    """A small real library, planned and ready to move."""
    stock(library_root, "DSCF0807.JPG", "DSCF0807.RAF", "DSCF1029.JPG", "seafront.JPG")
    settings = Settings(library_root=library_root, state_dir=tmp_path / "state")
    return settings, plan_migration(settings, now=FIXED_NOW)


def run(settings: Settings, plan: MigrationPlan, tmp_path: Path):
    with Catalog.open(settings) as catalog:
        return execute_migration(
            plan, catalog, reversal_path=tmp_path / "reversal.jsonl"
        )


def test_every_file_ends_up_at_its_planned_path(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path
) -> None:
    settings, plan = executable
    outcome = run(settings, plan, tmp_path)

    assert outcome.complete
    for photo in plan.photos:
        for _, target, _ in [(photo.source, photo.target, photo.content_hash)]:
            assert (settings.library_root / target).is_file()
        assert not photo.source.exists()


def test_moved_files_are_byte_identical(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path
) -> None:
    settings, plan = executable
    run(settings, plan, tmp_path)

    for photo in plan.photos:
        assert content_hash(settings.library_root / photo.target) == photo.content_hash
        if photo.raw_target:
            assert content_hash(settings.library_root / photo.raw_target) == \
                photo.raw_content_hash


def test_a_catalog_row_per_photograph_not_per_file(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path
) -> None:
    settings, plan = executable
    outcome = run(settings, plan, tmp_path)

    assert outcome.photos_catalogued == len(plan.photos)
    assert outcome.files_moved == plan.file_count
    with Catalog.open(settings) as catalog:
        assert catalog.photos.count() == len(plan.photos)


def test_the_catalog_row_carries_the_metadata_and_the_pair(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path
) -> None:
    settings, plan = executable
    run(settings, plan, tmp_path)

    with Catalog.open(settings) as catalog:
        photo = catalog.photos.by_library_path("2026/2026-07/DSCF0807.JPG")
        assert photo is not None
        assert photo.original_name == "DSCF0807.JPG"
        assert photo.raw_path == "2026/2026-07/DSCF0807.RAF"
        assert photo.has_raw
        assert photo.camera_model == fixtures.CAMERA_MODEL
        assert photo.lens == fixtures.LENS_MODEL
        assert photo.captured_at == fixtures.CAPTURE_TIMES["DSCF0807.JPG"]
        assert photo.captured_at_inferred is False
        assert catalog.absolute_path(photo).is_file()


def test_an_inferred_date_survives_into_the_catalog(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path
) -> None:
    settings, plan = executable
    run(settings, plan, tmp_path)

    with Catalog.open(settings) as catalog:
        seafront = next(p for p in catalog.photos.all() if p.original_name == "seafront.JPG")
        assert seafront.captured_at_inferred is True


def test_nothing_is_left_in_the_flat_root(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path
) -> None:
    settings, plan = executable
    run(settings, plan, tmp_path)

    leftovers = [p.name for p in settings.library_root.iterdir() if p.is_file()]
    assert leftovers == []


def test_the_library_opens_sensibly_in_explorer(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path
) -> None:
    """Success criterion 5, as far as a test can speak to it: dated folders,
    nothing stray, every file inside one."""
    settings, plan = executable
    run(settings, plan, tmp_path)

    top = sorted(p.name for p in settings.library_root.iterdir())
    assert top == ["2026"]
    for month in (settings.library_root / "2026").iterdir():
        assert re.fullmatch(r"2026-\d{2}", month.name)
        assert any(month.iterdir())


# -- refusing to run on a library that moved on ------------------------


def test_a_changed_file_stops_the_migration_before_anything_moves(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path
) -> None:
    settings, plan = executable
    before = directory_snapshot(settings.library_root)
    (settings.library_root / "DSCF1029.JPG").write_bytes(b"\xff\xd8 different now")

    with Catalog.open(settings) as catalog:
        with pytest.raises(MigrationError, match="changed since the plan"):
            execute_migration(plan, catalog, reversal_path=tmp_path / "r.jsonl")

    after = directory_snapshot(settings.library_root)
    assert set(after) == set(before), "the preflight moved something before refusing"


def test_a_missing_file_stops_the_migration(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path
) -> None:
    settings, plan = executable
    (settings.library_root / "seafront.JPG").unlink()

    with Catalog.open(settings) as catalog:
        with pytest.raises(MigrationError, match="gone from the library"):
            execute_migration(plan, catalog, reversal_path=tmp_path / "r.jsonl")


def test_an_occupied_target_stops_the_migration(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path
) -> None:
    settings, plan = executable
    occupied = settings.library_root / "2026" / "2026-07"
    occupied.mkdir(parents=True)
    (occupied / "DSCF0807.JPG").write_bytes(b"in the way")

    with Catalog.open(settings) as catalog:
        with pytest.raises(MigrationError, match="already there"):
            execute_migration(plan, catalog, reversal_path=tmp_path / "r.jsonl")


def test_a_plan_with_unreadable_files_is_refused(
    library_root: Path, tmp_path: Path
) -> None:
    """Moving a library you could not fully read is moving it half-blind."""
    stock(library_root, "DSCF1029.JPG")
    shutil.copy2(fixtures.TRUNCATED, library_root / fixtures.TRUNCATED.name)
    settings = Settings(library_root=library_root, state_dir=tmp_path / "state")
    plan = plan_migration(settings, now=FIXED_NOW)

    with Catalog.open(settings) as catalog:
        with pytest.raises(MigrationError, match="could not be read"):
            execute_migration(plan, catalog, reversal_path=tmp_path / "r.jsonl")


# -- failing partway ---------------------------------------------------


def test_a_failure_partway_leaves_no_half_migrated_photograph(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.1: a move that fails leaves no orphaned file and no catalog row. The
    pair is the sharp case — its JPEG moves, then its RAF fails."""
    settings, plan = executable
    import photo_manager.migration as module
    real = module.content_hash
    calls = {"n": 0}

    def flaky(path, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if Path(path).name == "DSCF0807.RAF" and Path(path).parent.name == "2026-07":
            return "0" * 64  # the RAF "arrives" with the wrong bytes
        return real(path, **kwargs)

    monkeypatch.setattr(module, "content_hash", flaky)

    with Catalog.open(settings) as catalog:
        outcome = execute_migration(plan, catalog, reversal_path=tmp_path / "r.jsonl")

    assert not outcome.complete
    assert "DSCF0807" in (outcome.failure or "")
    # Both halves of the pair are back where they started, and there is no row.
    assert (settings.library_root / "DSCF0807.JPG").is_file()
    assert (settings.library_root / "DSCF0807.RAF").is_file()
    with Catalog.open(settings) as catalog:
        assert catalog.photos.by_library_path("2026/2026-07/DSCF0807.JPG") is None


def test_photographs_completed_before_a_failure_are_kept(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-photo transactions, not one transaction around all of them. Rolling back
    every row while leaving every file moved would be the worst of both."""
    settings, plan = executable
    import photo_manager.migration as module
    real = module._migrate_one
    seen = {"n": 0}

    def stop_after_two(photo, root, catalog, log, imported_at):  # type: ignore[no-untyped-def]
        seen["n"] += 1
        if seen["n"] > 2:
            raise MigrationError("simulated disk trouble")
        return real(photo, root, catalog, log, imported_at)

    monkeypatch.setattr(module, "_migrate_one", stop_after_two)

    with Catalog.open(settings) as catalog:
        outcome = execute_migration(plan, catalog, reversal_path=tmp_path / "r.jsonl")

    assert not outcome.complete
    assert outcome.photos_catalogued == 2
    with Catalog.open(settings) as catalog:
        assert catalog.photos.count() == 2


# -- the reversal manifest (FR-8.4) ------------------------------------


def test_the_reversal_manifest_records_every_file_as_it_moves(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path
) -> None:
    settings, plan = executable
    outcome = run(settings, plan, tmp_path)

    header, moves = read_reversal_log(outcome.reversal_path)
    assert header["reversal_version"] == REVERSAL_VERSION
    assert header["library_root"] == str(settings.library_root)
    assert len(moves) == plan.file_count
    assert {m.source for m in moves} == {
        p.name for p in fixtures.ALL_PHOTOS
        if p.name in {"DSCF0807.JPG", "DSCF0807.RAF", "DSCF1029.JPG", "seafront.JPG"}
    }


def test_the_manifest_survives_a_crash_partway(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Written and fsynced per move, so a power cut leaves a true account of
    what moved rather than an empty file."""
    settings, plan = executable
    import photo_manager.migration as module
    real = module._migrate_one
    seen = {"n": 0}

    def stop_after_one(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen["n"] += 1
        if seen["n"] > 1:
            raise MigrationError("power cut")
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "_migrate_one", stop_after_one)
    with Catalog.open(settings) as catalog:
        outcome = execute_migration(plan, catalog, reversal_path=tmp_path / "r.jsonl")

    _, moves = read_reversal_log(outcome.reversal_path)
    assert moves, "the manifest is empty after a crash, which is when it matters most"
    for move in moves:
        assert (settings.library_root / move.target).is_file()


def test_a_rolled_back_move_is_not_offered_for_reversal(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, plan = executable
    import photo_manager.migration as module
    real = module.content_hash
    monkeypatch.setattr(
        module, "content_hash",
        lambda path, **kw: ("0" * 64 if Path(path).name == "DSCF0807.RAF"
                            and Path(path).parent.name == "2026-07"
                            else real(path, **kw)),
    )

    with Catalog.open(settings) as catalog:
        outcome = execute_migration(plan, catalog, reversal_path=tmp_path / "r.jsonl")

    _, moves = read_reversal_log(outcome.reversal_path)
    assert "DSCF0807.JPG" not in {m.source for m in moves}


# -- putting it back ---------------------------------------------------


def test_a_migration_can_be_reversed_exactly(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path
) -> None:
    """WU-1.5's acceptance criterion: the reversal is tested, not asserted."""
    settings, plan = executable
    before = directory_snapshot(settings.library_root)
    hashes = {p.name: content_hash(p)
              for p in settings.library_root.iterdir() if p.is_file()}

    outcome = run(settings, plan, tmp_path)
    assert outcome.complete
    assert directory_snapshot(settings.library_root) != before

    reversal = reverse_migration(outcome.reversal_path)

    assert reversal.complete
    assert reversal.restored == plan.file_count
    assert {p.name: content_hash(p)
            for p in settings.library_root.iterdir() if p.is_file()} == hashes


def test_reversal_removes_the_folders_it_emptied(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path
) -> None:
    settings, plan = executable
    outcome = run(settings, plan, tmp_path)
    reverse_migration(outcome.reversal_path)

    assert [p.name for p in settings.library_root.iterdir() if p.is_dir()] == []


def test_reversing_twice_is_safe(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path
) -> None:
    settings, plan = executable
    outcome = run(settings, plan, tmp_path)

    first = reverse_migration(outcome.reversal_path)
    second = reverse_migration(outcome.reversal_path)

    assert first.complete and second.complete
    assert second.restored == 0
    assert second.skipped


def test_reversal_refuses_to_overwrite_something_at_the_original_path(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path
) -> None:
    settings, plan = executable
    outcome = run(settings, plan, tmp_path)
    (settings.library_root / "seafront.JPG").write_bytes(b"\xff\xd8 something new")

    reversal = reverse_migration(outcome.reversal_path)

    assert not reversal.complete
    assert any("seafront.JPG" in f for f in reversal.failures)
    assert (settings.library_root / "seafront.JPG").read_bytes().startswith(b"\xff\xd8 s")


def test_reversal_leaves_the_catalog_alone(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path
) -> None:
    """Deliberate: a catalog is rebuildable from the library (NFR-2), and a
    function that both moves files and deletes rows can half-do either."""
    settings, plan = executable
    outcome = run(settings, plan, tmp_path)
    reverse_migration(outcome.reversal_path)

    with Catalog.open(settings) as catalog:
        assert catalog.photos.count() == len(plan.photos)


def test_a_truncated_reversal_log_is_reported_not_half_read(
    executable: tuple[Settings, MigrationPlan], tmp_path: Path
) -> None:
    settings, plan = executable
    outcome = run(settings, plan, tmp_path)
    text = outcome.reversal_path.read_text(encoding="utf-8")
    outcome.reversal_path.write_text(text[:-20], encoding="utf-8")

    with pytest.raises(MigrationError, match="not valid JSON"):
        read_reversal_log(outcome.reversal_path)
