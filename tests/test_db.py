"""The schema and the migration mechanism.

The schema is a first cut and REQUIREMENTS.md says so. These tests exist mostly
to make the *second* cut safe: that a migration applies to a database with real
rows in it, that a failed one leaves nothing half-applied, and that a catalog
written by a newer build is refused rather than mangled.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from photo_manager import db
from photo_manager.db import MIGRATIONS, Migration, MigrationError


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "catalog.sqlite3"


@pytest.fixture
def connection(database_path: Path):  # type: ignore[no-untyped-def]
    conn = db.connect(database_path)
    yield conn
    conn.close()


# ----------------------------------------------------------------------
# Connection pragmas
# ----------------------------------------------------------------------


def test_connect_creates_the_parent_directory(database_path: Path) -> None:
    assert not database_path.parent.exists()

    db.connect(database_path).close()

    assert database_path.exists()


def test_connection_is_in_wal_mode(connection: sqlite3.Connection) -> None:
    """NFR-2. Background thumbnailing must not block the UI's writes."""
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_foreign_keys_are_enforced(connection: sqlite3.Connection) -> None:
    """SQLite defaults these off, which would make every REFERENCES a comment."""
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_the_page_cache_is_big_enough_to_hold_a_real_catalog(
    connection: sqlite3.Connection,
) -> None:
    """A dashboard filter reads the same table seven times in one operation.

    SQLite's default is 2 MB, and a catalog of 10,000 photographs is about 5 —
    so each facet evicted what the one before it had just read, and a grouped
    scan cost 42 ms instead of 10 (NFR-1, FR-7.4). Negative is kibibytes, and
    the pages are allocated only as they are used.
    """
    assert connection.execute("PRAGMA cache_size").fetchone()[0] == -16000


def test_wal_mode_survives_reopening(database_path: Path) -> None:
    db.connect(database_path).close()

    reopened = db.connect(database_path)
    assert reopened.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    reopened.close()


# ----------------------------------------------------------------------
# Creating the schema from scratch
# ----------------------------------------------------------------------


def test_a_fresh_database_starts_at_version_zero(connection: sqlite3.Connection) -> None:
    assert db.schema_version(connection) == 0


def test_migrate_creates_the_schema_from_scratch(connection: sqlite3.Connection) -> None:
    applied = db.migrate(connection)

    assert [m.version for m in applied] == [m.version for m in MIGRATIONS]
    assert db.schema_version(connection) == db.SCHEMA_VERSION


def test_every_table_in_the_spec_exists(connection: sqlite3.Connection) -> None:
    """REQUIREMENTS.md §5.1, table for table."""
    db.migrate(connection)

    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "photo", "session", "location", "person", "tag",
        "photo_person", "photo_tag", "photo_location",
    } <= tables


def test_migrating_twice_changes_nothing(connection: sqlite3.Connection) -> None:
    db.migrate(connection)

    assert db.migrate(connection) == []
    assert db.schema_version(connection) == db.SCHEMA_VERSION


# ----------------------------------------------------------------------
# Migrating a database that already has data
# ----------------------------------------------------------------------
#
# The mechanism is tested with synthetic migrations rather than by inventing a
# throwaway second migration for the real schema. What matters is that the
# machinery works on a populated database, not what version 2 eventually is.

#: The next version after the shipped ones, so these tests keep exercising the
#: machinery whatever the real schema has grown to.
_NEXT = db.SCHEMA_VERSION + 1

_ADD_A_COLUMN = Migration(
    _NEXT, "add a column", "ALTER TABLE person ADD COLUMN nickname TEXT;"
)


def test_a_migration_applies_cleanly_to_an_existing_database(
    connection: sqlite3.Connection,
) -> None:
    db.migrate(connection)
    connection.execute("INSERT INTO person (name, use_count) VALUES ('Alice', 3)")
    connection.commit()

    applied = db.migrate(connection, MIGRATIONS + (_ADD_A_COLUMN,))

    assert [m.version for m in applied] == [_NEXT]
    assert db.schema_version(connection) == _NEXT
    # The existing row survived, and the new column is there and empty.
    row = connection.execute("SELECT name, use_count, nickname FROM person").fetchone()
    assert (row["name"], row["use_count"], row["nickname"]) == ("Alice", 3, None)


def test_migrations_apply_in_order_from_empty(connection: sqlite3.Connection) -> None:
    applied = db.migrate(connection, MIGRATIONS + (_ADD_A_COLUMN,))

    assert [m.version for m in applied] == list(range(1, _NEXT + 1))


def test_a_failing_migration_leaves_the_version_untouched(
    connection: sqlite3.Connection,
) -> None:
    """A half-applied migration on the owner's only catalog is the bad outcome."""
    db.migrate(connection)
    broken = Migration(
        _NEXT, "broken", "CREATE TABLE half_applied (id INTEGER); CREATE TABLE ;"
    )

    with pytest.raises(MigrationError, match=f"Migration {_NEXT}"):
        db.migrate(connection, MIGRATIONS + (broken,))

    assert db.schema_version(connection) == db.SCHEMA_VERSION
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "half_applied" not in tables


def test_a_database_from_a_newer_build_is_refused(connection: sqlite3.Connection) -> None:
    db.migrate(connection, MIGRATIONS + (_ADD_A_COLUMN,))

    with pytest.raises(MigrationError, match="newer version"):
        db.migrate(connection, MIGRATIONS)


def test_migrations_must_be_strictly_ascending(connection: sqlite3.Connection) -> None:
    out_of_order = (Migration(2, "second", ""), Migration(1, "first", ""))

    with pytest.raises(MigrationError, match="ascending"):
        db.migrate(connection, out_of_order)


def test_the_shipped_migrations_are_well_ordered() -> None:
    assert [m.version for m in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1))
    assert db.SCHEMA_VERSION == MIGRATIONS[-1].version


# ----------------------------------------------------------------------
# Constraints that encode requirements
# ----------------------------------------------------------------------


def _insert_photo(connection: sqlite3.Connection, **overrides: object) -> None:
    values: dict[str, object] = {
        "content_hash": "a" * 64,
        "library_path": "2026/2026-07-13/DSCF0807.jpg",
        "original_name": "DSCF0807.JPG",
        "has_raw": 0,
        "raw_path": None,
        "captured_at": "2026-07-13T12:50:56",
        "imported_at": "2026-09-20T09:00:00",
        "rating": 0,
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO photo ({columns}) VALUES ({placeholders})", tuple(values.values())
    )


@pytest.fixture
def migrated(connection: sqlite3.Connection) -> sqlite3.Connection:
    db.migrate(connection)
    return connection


@pytest.mark.parametrize(
    "library_path",
    [
        r"C:\Users\you\Pictures\Photos\2026\DSCF0807.jpg",  # absolute, Windows
        "C:/Users/you/Pictures/Photos/2026/DSCF0807.jpg",   # absolute, forward slashes
        "/home/user/pictures/DSCF0807.jpg",                 # absolute, POSIX
        "2026/../../etc/DSCF0807.jpg",                      # escapes the root
        "",                                                 # nothing at all
    ],
)
def test_library_path_must_be_relative_to_the_library_root(
    migrated: sqlite3.Connection, library_path: str
) -> None:
    """§4.2 — an absolute path here would pin the catalog to one drive letter."""
    with pytest.raises(sqlite3.IntegrityError):
        _insert_photo(migrated, library_path=library_path)


@pytest.mark.parametrize(
    "raw_path",
    [
        r"C:\Users\you\Pictures\Photos\2026\DSCF0807.raf",
        "C:/Users/you/Pictures/Photos/2026/DSCF0807.raf",
        "/home/user/pictures/DSCF0807.raf",
        "2026/../../etc/DSCF0807.raf",
        "",
    ],
)
def test_raw_path_must_be_relative_too(
    migrated: sqlite3.Connection, raw_path: str
) -> None:
    """The RAF travels with the JPEG (D-2), under the same relocation promise."""
    with pytest.raises(sqlite3.IntegrityError):
        _insert_photo(migrated, has_raw=1, raw_path=raw_path)


def test_has_raw_cannot_disagree_with_raw_path(migrated: sqlite3.Connection) -> None:
    """D-2: the RAF is opt-in, and 'flagged but absent' is not a state."""
    with pytest.raises(sqlite3.IntegrityError):
        _insert_photo(migrated, has_raw=1, raw_path=None)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_photo(
            migrated, has_raw=0, raw_path="2026/2026-07-13/DSCF0807.raf",
            content_hash="b" * 64, library_path="2026/other.jpg",
        )


def test_the_same_photo_cannot_be_imported_twice(migrated: sqlite3.Connection) -> None:
    """C-3: the card is not wiped, so the hash is the identity."""
    _insert_photo(migrated)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_photo(migrated, library_path="2026/2026-07-13/copy.jpg")


def test_two_rows_cannot_claim_the_same_file(migrated: sqlite3.Connection) -> None:
    _insert_photo(migrated)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_photo(migrated, content_hash="b" * 64)


@pytest.mark.parametrize("rating", [-1, 6])
def test_rating_stays_within_zero_to_five(migrated: sqlite3.Connection, rating: int) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_photo(migrated, rating=rating)


def test_a_session_cannot_end_before_it_starts(migrated: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO session (started_at, ended_at) VALUES "
            "('2026-09-06T14:00:00', '2026-09-06T11:00:00')"
        )


def test_two_locations_cannot_share_a_slug(migrated: sqlite3.Connection) -> None:
    """The slug's only job is naming files (D-7), so a shared one is ambiguity."""
    migrated.execute("INSERT INTO location (name, slug) VALUES ('St Aldo', 'st-aldo')")

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute("INSERT INTO location (name, slug) VALUES ('St. Aldo', 'st-aldo')")


def test_deleting_a_photo_takes_its_links_with_it(migrated: sqlite3.Connection) -> None:
    _insert_photo(migrated)
    migrated.execute("INSERT INTO person (name) VALUES ('Alice')")
    migrated.execute("INSERT INTO tag (name) VALUES ('sunset')")
    migrated.execute("INSERT INTO photo_person (photo_id, person_id) VALUES (1, 1)")
    migrated.execute("INSERT INTO photo_tag (photo_id, tag_id) VALUES (1, 1)")

    migrated.execute("DELETE FROM photo WHERE id = 1")

    assert migrated.execute("SELECT count(*) FROM photo_person").fetchone()[0] == 0
    assert migrated.execute("SELECT count(*) FROM photo_tag").fetchone()[0] == 0
    # The vocabulary itself is not collateral damage.
    assert migrated.execute("SELECT count(*) FROM person").fetchone()[0] == 1


def test_deleting_a_session_does_not_delete_its_photos(
    migrated: sqlite3.Connection,
) -> None:
    """Sessions are a grouping, not an owner. Losing one must not lose frames."""
    migrated.execute("INSERT INTO session (started_at) VALUES ('2026-09-06T11:00:00')")
    _insert_photo(migrated, session_id=1)

    migrated.execute("DELETE FROM session WHERE id = 1")

    row = migrated.execute("SELECT session_id FROM photo").fetchone()
    assert row["session_id"] is None
