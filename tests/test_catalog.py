"""The data layer: round trips, the joins, transactions, and relative paths.

The relative-path tests at the bottom are the ones that matter most. Storage is
being expanded separately (REQUIREMENTS.md C-2), so the library *will* move to
another volume, and the promise made in §4.2 is that this costs a config edit
and no catalog migration. That promise is only real if it is tested.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from photo_manager.catalog import Catalog, CatalogError
from photo_manager.config import Settings
from photo_manager.models import Location, Person, Photo, Session, Tag

CAPTURED = dt.datetime(2026, 7, 13, 12, 50, 56)
IMPORTED = dt.datetime(2026, 9, 20, 9, 0, 0)


def a_photo(**overrides: object) -> Photo:
    """A minimal valid photo. Override whatever the test is actually about."""
    values: dict[str, object] = {
        "content_hash": "a" * 64,
        "library_path": "2026/2026-07-13/2026-07-13_DSCF0807.jpg",
        "original_name": "DSCF0807.JPG",
        "captured_at": CAPTURED,
        "imported_at": IMPORTED,
    }
    values.update(overrides)
    return Photo(**values)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Opening
# ----------------------------------------------------------------------


def test_open_creates_and_migrates_the_catalog(settings: Settings) -> None:
    assert settings.database_path is not None
    assert not settings.database_path.exists()

    with Catalog.open(settings) as catalog:
        assert catalog.schema_version > 0
        assert catalog.photos.count() == 0


def test_open_fails_loudly_when_the_library_has_moved(settings: Settings) -> None:
    """§4.2 — better a clear error than an empty catalog that looks like loss."""
    from photo_manager.config import LibraryRootError

    moved = Settings(
        library_root=settings.library_root / "somewhere-else",
        state_dir=settings.state_dir,
    )

    with pytest.raises(LibraryRootError):
        Catalog.open(moved)


# ----------------------------------------------------------------------
# photo
# ----------------------------------------------------------------------


def test_photo_round_trips_every_field(catalog: Catalog) -> None:
    original = a_photo(
        raw_path="2026/2026-07-13/2026-07-13_DSCF0807.raf",
        captured_at_inferred=True,
        width=6240,
        height=4160,
        camera_model="X-T30 III",
        lens="XF18-55mmF2.8-4 R LM OIS",
        exposure="1/500",
        f_number=5.6,
        iso=160,
        focal_length=35.0,
        orientation=1,
        rating=4,
    )

    stored = catalog.photos.add(original)
    read_back = catalog.photos.get(stored.id)

    assert stored.id is not None
    assert read_back == stored
    # Everything came back as itself, not as text.
    assert read_back.captured_at == CAPTURED
    assert read_back.captured_at_inferred is True
    assert read_back.f_number == 5.6
    assert read_back.rating == 4


def test_has_raw_follows_raw_path(catalog: Catalog) -> None:
    """D-2: the RAF is opt-in, and the flag is derived so it cannot drift."""
    without = catalog.photos.add(a_photo())
    with_raw = catalog.photos.add(
        a_photo(
            content_hash="b" * 64,
            library_path="2026/2026-07-13/other.jpg",
            raw_path="2026/2026-07-13/other.raf",
        )
    )

    assert without.has_raw is False
    assert with_raw.has_raw is True
    # And the stored column agrees, because FR-7.2 filters on it.
    stored = {
        row["library_path"]: row["has_raw"]
        for row in catalog.connection.execute("SELECT library_path, has_raw FROM photo")
    }
    assert stored == {without.library_path: 0, with_raw.library_path: 1}


def test_adding_a_photo_that_already_has_an_id_is_refused(catalog: Catalog) -> None:
    stored = catalog.photos.add(a_photo())

    with pytest.raises(CatalogError, match="already has id"):
        catalog.photos.add(stored)


def test_photo_update_writes_every_mutable_field(catalog: Catalog) -> None:
    stored = catalog.photos.add(a_photo())

    from dataclasses import replace

    catalog.photos.update(replace(stored, rating=5, lens="XF35mmF2 R WR"))

    read_back = catalog.photos.get(stored.id)
    assert read_back.rating == 5
    assert read_back.lens == "XF35mmF2 R WR"


def test_updating_an_unsaved_photo_is_refused(catalog: Catalog) -> None:
    with pytest.raises(CatalogError, match="no id"):
        catalog.photos.update(a_photo())


def test_lookup_by_hash_is_the_duplicate_check(catalog: Catalog) -> None:
    """C-3: re-inserting the card must not re-offer what is already imported."""
    stored = catalog.photos.add(a_photo())

    assert catalog.photos.by_hash("a" * 64) == stored
    assert catalog.photos.by_hash("f" * 64) is None


def test_hashes_returns_the_whole_set_in_one_query(catalog: Catalog) -> None:
    """A card scan checks a thousand frames against this (FR-1.4)."""
    catalog.photos.add(a_photo())
    catalog.photos.add(a_photo(content_hash="b" * 64, library_path="2026/b.jpg"))

    assert catalog.photos.hashes() == {"a" * 64, "b" * 64}


def test_lookup_by_library_path(catalog: Catalog) -> None:
    stored = catalog.photos.add(a_photo())

    assert catalog.photos.by_library_path(stored.library_path) == stored
    assert catalog.photos.by_library_path("2026/nope.jpg") is None


def test_photos_come_back_newest_first(catalog: Catalog) -> None:
    """FR-7.1 — the grid's default order."""
    older = catalog.photos.add(
        a_photo(captured_at=dt.datetime(2026, 4, 2, 17, 27, 14))
    )
    newer = catalog.photos.add(
        a_photo(
            content_hash="b" * 64,
            library_path="2026/b.jpg",
            captured_at=dt.datetime(2026, 9, 6, 11, 6, 51),
        )
    )

    assert [p.id for p in catalog.photos.all()] == [newer.id, older.id]
    assert [p.id for p in catalog.photos.all(newest_first=False)] == [older.id, newer.id]


def test_deleting_a_photo_removes_only_the_row(catalog: Catalog) -> None:
    stored = catalog.photos.add(a_photo())

    assert catalog.photos.delete(stored.id) is True
    assert catalog.photos.delete(stored.id) is False
    assert catalog.photos.count() == 0


# ----------------------------------------------------------------------
# session
# ----------------------------------------------------------------------


def test_session_round_trips(catalog: Catalog) -> None:
    location = catalog.locations.add(Location(name="St Aldo", slug="st-aldo"))
    original = Session(
        started_at=dt.datetime(2026, 9, 6, 11, 6, 51),
        ended_at=dt.datetime(2026, 9, 6, 14, 30, 0),
        location_id=location.id,
        event="harbour walk",
    )

    stored = catalog.sessions.add(original)

    assert stored.id is not None
    assert catalog.sessions.get(stored.id) == stored


def test_a_session_may_still_be_open(catalog: Catalog) -> None:
    """ended_at is NULL while an ingest is still assembling the session."""
    stored = catalog.sessions.add(Session(started_at=dt.datetime(2026, 9, 6, 11, 0)))

    assert catalog.sessions.get(stored.id).ended_at is None


def test_assigning_photos_to_a_session(catalog: Catalog) -> None:
    """FR-3.3 — the session is the unit tagging operates on."""
    session = catalog.sessions.add(Session(started_at=dt.datetime(2026, 9, 6, 11, 0)))
    first = catalog.photos.add(a_photo(captured_at=dt.datetime(2026, 9, 6, 11, 6, 51)))
    second = catalog.photos.add(
        a_photo(
            content_hash="b" * 64,
            library_path="2026/b.jpg",
            captured_at=dt.datetime(2026, 9, 6, 11, 6, 53),
        )
    )

    catalog.sessions.assign(session.id, [first.id, second.id])

    assert [p.id for p in catalog.photos.in_session(session.id)] == [first.id, second.id]

    catalog.sessions.assign(None, [second.id])
    assert [p.id for p in catalog.photos.in_session(session.id)] == [first.id]


def test_sessions_come_back_newest_first(catalog: Catalog) -> None:
    older = catalog.sessions.add(Session(started_at=dt.datetime(2026, 7, 13, 12, 0)))
    newer = catalog.sessions.add(Session(started_at=dt.datetime(2026, 9, 6, 11, 0)))

    assert [s.id for s in catalog.sessions.all()] == [newer.id, older.id]


def test_deleting_a_session_leaves_its_photos_behind(catalog: Catalog) -> None:
    session = catalog.sessions.add(Session(started_at=dt.datetime(2026, 9, 6, 11, 0)))
    photo = catalog.photos.add(a_photo(session_id=session.id))

    catalog.sessions.delete(session.id)

    assert catalog.photos.get(photo.id).session_id is None


def test_session_update(catalog: Catalog) -> None:
    from dataclasses import replace

    stored = catalog.sessions.add(Session(started_at=dt.datetime(2026, 9, 6, 11, 0)))

    catalog.sessions.update(replace(stored, event="harbour walk"))

    assert catalog.sessions.get(stored.id).event == "harbour walk"


# ----------------------------------------------------------------------
# location, person, tag
# ----------------------------------------------------------------------


def test_location_round_trips(catalog: Catalog) -> None:
    stored = catalog.locations.add(Location(name="St Aldo", slug="st-aldo"))

    assert catalog.locations.get(stored.id) == stored
    assert catalog.locations.by_name("St Aldo") == stored
    assert catalog.locations.by_slug("st-aldo") == stored
    # D-5: coordinates are reserved, not populated. There is no GPS (C-1).
    assert stored.latitude is None and stored.longitude is None


def test_location_carries_coordinates_when_given_them(catalog: Catalog) -> None:
    """Not used today; here so D-5's map needs no migration."""
    stored = catalog.locations.add(
        Location(name="St Aldo", slug="st-aldo", latitude=50.2084, longitude=-5.4807)
    )

    assert catalog.locations.get(stored.id).latitude == pytest.approx(50.2084)


def test_person_round_trips(catalog: Catalog) -> None:
    stored = catalog.people.add(Person(name="Alice"))

    assert catalog.people.get(stored.id) == stored
    assert catalog.people.by_name("Alice") == stored


def test_tag_round_trips(catalog: Catalog) -> None:
    stored = catalog.tags.add(Tag(name="sunset"))

    assert catalog.tags.get(stored.id) == stored
    assert catalog.tags.by_name("sunset") == stored


@pytest.mark.parametrize("vocabulary", ["locations", "people", "tags"])
def test_vocabularies_rank_by_actual_use(catalog: Catalog, vocabulary: str) -> None:
    """FR-5.1: autocomplete is ranked by use_count, not alphabetically."""
    repository = getattr(catalog, vocabulary)
    make = (
        (lambda name: Location(name=name, slug=name.lower()))
        if vocabulary == "locations"
        else (Person if vocabulary == "people" else Tag)
    )
    rare = repository.add(make("Aardvark"))
    common = repository.add(make("Zoo"))

    repository.increment_use(common.id, by=5)

    assert [row.name for row in repository.all()] == ["Zoo", "Aardvark"]
    assert repository.get(common.id).use_count == 5
    assert repository.get(rare.id).use_count == 0


def test_incrementing_use_on_a_missing_row_is_an_error(catalog: Catalog) -> None:
    with pytest.raises(CatalogError, match="No tag with id"):
        catalog.tags.increment_use(999)


def test_vocabulary_update_and_delete(catalog: Catalog) -> None:
    from dataclasses import replace

    stored = catalog.people.add(Person(name="Alice"))

    catalog.people.update(replace(stored, name="Alice Smith"))
    assert catalog.people.get(stored.id).name == "Alice Smith"

    assert catalog.people.delete(stored.id) is True
    assert catalog.people.get(stored.id) is None


# ----------------------------------------------------------------------
# photo_person, photo_tag, photo_location
# ----------------------------------------------------------------------


def test_people_link_round_trips(catalog: Catalog) -> None:
    photo = catalog.photos.add(a_photo())
    alice = catalog.people.add(Person(name="Alice"))
    carol = catalog.people.add(Person(name="Carol"))

    catalog.photos.add_person(photo.id, alice.id)
    catalog.photos.add_person(photo.id, carol.id)
    catalog.photos.add_person(photo.id, alice.id)  # again — must be a no-op

    assert [p.name for p in catalog.photos.people(photo.id)] == ["Alice", "Carol"]

    catalog.photos.remove_person(photo.id, carol.id)
    assert [p.name for p in catalog.photos.people(photo.id)] == ["Alice"]


def test_tag_link_round_trips(catalog: Catalog) -> None:
    photo = catalog.photos.add(a_photo())
    sunset = catalog.tags.add(Tag(name="sunset"))
    harbour = catalog.tags.add(Tag(name="harbour"))

    catalog.photos.add_tag(photo.id, sunset.id)
    catalog.photos.add_tag(photo.id, harbour.id)

    assert [t.name for t in catalog.photos.tags(photo.id)] == ["harbour", "sunset"]

    catalog.photos.remove_tag(photo.id, harbour.id)
    assert [t.name for t in catalog.photos.tags(photo.id)] == ["sunset"]


def test_a_photo_takes_its_session_location(catalog: Catalog) -> None:
    """§6.3 — location is a property of the outing, not of the frame."""
    st_aldo = catalog.locations.add(Location(name="St Aldo", slug="st-aldo"))
    session = catalog.sessions.add(
        Session(started_at=dt.datetime(2026, 9, 6, 11, 0), location_id=st_aldo.id)
    )
    photo = catalog.photos.add(a_photo(session_id=session.id))

    assert catalog.photos.location(photo.id) == st_aldo
    assert catalog.photos.location_override(photo.id) is None


def test_a_photo_may_override_its_session_location(catalog: Catalog) -> None:
    """FR-3.4 — one frame is allowed to disagree with the outing."""
    st_aldo = catalog.locations.add(Location(name="St Aldo", slug="st-aldo"))
    carbis = catalog.locations.add(Location(name="Carbis Bay", slug="carbis-bay"))
    session = catalog.sessions.add(
        Session(started_at=dt.datetime(2026, 9, 6, 11, 0), location_id=st_aldo.id)
    )
    photo = catalog.photos.add(a_photo(session_id=session.id))

    catalog.photos.set_location(photo.id, carbis.id)

    assert catalog.photos.location(photo.id) == carbis
    assert catalog.photos.location_override(photo.id) == carbis

    # Setting it again replaces rather than duplicating — there is at most one.
    catalog.photos.set_location(photo.id, st_aldo.id)
    assert catalog.photos.location_override(photo.id) == st_aldo

    catalog.photos.clear_location(photo.id)
    assert catalog.photos.location_override(photo.id) is None
    assert catalog.photos.location(photo.id) == st_aldo  # back to the session's


def test_a_photo_with_no_session_and_no_override_has_no_location(
    catalog: Catalog,
) -> None:
    """The untagged backlog FR-7.6 exists to surface."""
    photo = catalog.photos.add(a_photo())

    assert catalog.photos.location(photo.id) is None


# ----------------------------------------------------------------------
# Transactions
# ----------------------------------------------------------------------


def test_a_single_write_commits_on_its_own(settings: Settings) -> None:
    with Catalog.open(settings) as catalog:
        catalog.photos.add(a_photo())

    with Catalog.open(settings) as reopened:
        assert reopened.photos.count() == 1


def test_a_failed_transaction_leaves_no_rows(settings: Settings) -> None:
    """§4.1, FR-6.4 — a copy that fails partway leaves no catalog row."""
    with Catalog.open(settings) as catalog:
        with pytest.raises(sqlite3.IntegrityError):
            with catalog.transaction():
                catalog.photos.add(a_photo())
                catalog.photos.add(a_photo(library_path="2026/duplicate-hash.jpg"))

        assert catalog.photos.count() == 0

    with Catalog.open(settings) as reopened:
        assert reopened.photos.count() == 0


def test_a_transaction_commits_once_at_the_end(settings: Settings) -> None:
    with Catalog.open(settings) as catalog:
        with catalog.transaction():
            photo = catalog.photos.add(a_photo())
            person = catalog.people.add(Person(name="Alice"))
            catalog.photos.add_person(photo.id, person.id)

    with Catalog.open(settings) as reopened:
        assert reopened.photos.count() == 1
        assert [p.name for p in reopened.photos.people(1)] == ["Alice"]


def test_nested_transactions_commit_only_at_the_outermost(catalog: Catalog) -> None:
    """sessions.assign() opens one internally; calling it inside one must work."""
    session = catalog.sessions.add(Session(started_at=dt.datetime(2026, 9, 6, 11, 0)))

    with pytest.raises(RuntimeError):
        with catalog.transaction():
            photo = catalog.photos.add(a_photo())
            catalog.sessions.assign(session.id, [photo.id])
            raise RuntimeError("import failed after the rows were written")

    assert catalog.photos.count() == 0


# ----------------------------------------------------------------------
# Relative paths — the promise that the library can move (§4.2, C-2)
# ----------------------------------------------------------------------


def test_stored_paths_are_relative_and_forward_slashed(catalog: Catalog) -> None:
    absolute = catalog.settings.library_root / "2026" / "2026-07-13" / "DSCF0807.jpg"

    stored = catalog.relative_path(absolute)

    assert stored == "2026/2026-07-13/DSCF0807.jpg"
    assert not Path(stored).is_absolute()


def test_an_absolute_path_cannot_reach_the_database(catalog: Catalog) -> None:
    """The schema's CHECK is the backstop for Settings.relativise."""
    absolute = str(catalog.settings.library_root / "DSCF0807.jpg")

    with pytest.raises(sqlite3.IntegrityError):
        catalog.photos.add(a_photo(library_path=absolute))


def test_the_same_catalog_resolves_under_two_different_library_roots(
    tmp_path: Path,
) -> None:
    """C-2's whole point: moving the library is a config edit, not a migration.

    One database file, two roots. Nothing in the catalog is rewritten and every
    photo still resolves — which is only true because rows store relative paths.
    """
    state = tmp_path / "state"
    old_root = tmp_path / "Pictures" / "XT30"
    new_root = tmp_path / "BigDisk" / "Photos"
    old_root.mkdir(parents=True)
    new_root.mkdir(parents=True)

    on_c_drive = Settings(library_root=old_root, state_dir=state)
    on_the_new_disk = Settings(library_root=new_root, state_dir=state)

    with Catalog.open(on_c_drive) as catalog:
        stored = catalog.photos.add(
            a_photo(raw_path="2026/2026-07-13/2026-07-13_DSCF0807.raf")
        )
        assert catalog.absolute_path(stored) == old_root / "2026" / "2026-07-13" / (
            "2026-07-13_DSCF0807.jpg"
        )

    # Same database file, config now points at the bigger volume.
    with Catalog.open(on_the_new_disk) as moved:
        photo = moved.photos.by_hash("a" * 64)

        assert photo == stored  # not one byte of the row changed
        assert moved.absolute_path(photo) == new_root / "2026" / "2026-07-13" / (
            "2026-07-13_DSCF0807.jpg"
        )
        assert moved.absolute_raw_path(photo) == new_root / "2026" / "2026-07-13" / (
            "2026-07-13_DSCF0807.raf"
        )


def test_absolute_raw_path_is_none_when_no_raw_was_kept(catalog: Catalog) -> None:
    photo = catalog.photos.add(a_photo())

    assert catalog.absolute_raw_path(photo) is None


def test_a_path_outside_the_library_cannot_be_stored(catalog: Catalog) -> None:
    with pytest.raises(ValueError, match="outside the library root"):
        catalog.relative_path(Path("D:/DCIM/100_FUJI/DSCF0807.JPG"))
