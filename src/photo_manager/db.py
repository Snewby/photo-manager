"""SQLite connection handling, the schema, and the migration mechanism.

Three things this module exists to guarantee:

* **WAL mode** (REQUIREMENTS.md NFR-2), so a background thumbnail worker reading
  the catalog never blocks the UI writing to it.
* **Foreign keys on.** SQLite leaves them off by default, which would silently
  turn every ``REFERENCES`` in the schema into documentation.
* **The schema can change.** It is a first cut and it *will* move, so versioning
  is here from the first row rather than retrofitted around real data.

Versioning uses SQLite's own ``user_version`` pragma. No bookkeeping table, no
dependency, and the version travels inside the database file — a copied or
restored catalog carries its own version with it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Migration",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "MigrationError",
    "connect",
    "migrate",
    "schema_version",
]


class MigrationError(Exception):
    """A migration is malformed, out of order, or failed to apply."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One forward step of the schema.

    There is deliberately no ``down`` SQL. A catalog is rebuildable from the
    library and its sidecars (NFR-2), so the recovery path for a bad migration
    is to rebuild, not to reverse — and a half-tested down-migration is a worse
    risk than not having one.
    """

    version: int
    name: str
    sql: str


_INITIAL_SCHEMA = r"""
-- The photo table. One row per imported JPEG; the RAF, when kept (D-2), rides
-- along on the same row rather than being a photo in its own right (FR-1.2).
CREATE TABLE photo (
    id                   INTEGER PRIMARY KEY,

    -- SHA-256 of the image bytes. Drives duplicate detection, which is a
    -- correctness requirement because the card is not wiped between sessions
    -- (C-3), and keys the shared thumbnail cache (FR-2.3).
    content_hash         TEXT    NOT NULL UNIQUE,

    -- Relative to the library root, forward-slashed, so the whole library can
    -- move to another volume as a config edit (REQUIREMENTS.md §4.2). The CHECK
    -- is a backstop for the rule Settings.relativise already enforces: an
    -- absolute path in here would quietly pin the catalog to one drive letter.
    -- '_:/%' catches a forward-slashed Windows absolute path ('C:/Users/...'),
    -- which the backslash test alone would let straight through.
    library_path         TEXT    NOT NULL UNIQUE
                                 CHECK (library_path <> ''
                                        AND library_path NOT LIKE '/%'
                                        AND library_path NOT LIKE '_:/%'
                                        AND instr(library_path, '\') = 0
                                        AND instr(library_path, '..') = 0),
    original_name        TEXT    NOT NULL,

    -- has_raw is stored as well as derivable so FR-7.2's has-RAW filter is an
    -- index lookup, but the CHECK makes the two impossible to disagree.
    has_raw              INTEGER NOT NULL DEFAULT 0 CHECK (has_raw IN (0, 1)),
    raw_path             TEXT    CHECK (raw_path IS NULL
                                        OR (raw_path <> ''
                                            AND raw_path NOT LIKE '/%'
                                            AND raw_path NOT LIKE '_:/%'
                                            AND instr(raw_path, '\') = 0
                                            AND instr(raw_path, '..') = 0)),

    captured_at          TEXT    NOT NULL,
    -- 1 when EXIF had no capture date and mtime was used instead (FR-8.2a).
    -- Two real files in the library need this, and they are the hand-renamed,
    -- most-meaningful ones, so it must be visible rather than assumed.
    captured_at_inferred INTEGER NOT NULL DEFAULT 0
                                 CHECK (captured_at_inferred IN (0, 1)),
    imported_at          TEXT    NOT NULL,

    width                INTEGER,
    height               INTEGER,
    camera_model         TEXT,
    lens                 TEXT,
    exposure             TEXT,
    f_number             REAL,
    iso                  INTEGER,
    focal_length         REAL,
    orientation          INTEGER,

    rating               INTEGER NOT NULL DEFAULT 0 CHECK (rating BETWEEN 0 AND 5),

    -- Sessions are the tagging unit (FR-3.3). Losing one must not take its
    -- photos with it, hence SET NULL rather than CASCADE.
    session_id           INTEGER REFERENCES session(id) ON DELETE SET NULL,

    CHECK ((has_raw = 0 AND raw_path IS NULL)
           OR (has_raw = 1 AND raw_path IS NOT NULL))
);

CREATE INDEX idx_photo_captured_at ON photo(captured_at);
CREATE INDEX idx_photo_session     ON photo(session_id);
CREATE INDEX idx_photo_rating      ON photo(rating);
CREATE INDEX idx_photo_has_raw     ON photo(has_raw);

-- An outing. Location is a property of one of these, not of a frame (§6.3).
CREATE TABLE session (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT    NOT NULL,
    -- NULL while a session is still being assembled during an ingest.
    ended_at    TEXT,
    location_id INTEGER REFERENCES location(id) ON DELETE SET NULL,
    event       TEXT,

    CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE INDEX idx_session_started_at ON session(started_at);
CREATE INDEX idx_session_location   ON session(location_id);

-- The camera records no GPS (C-1), so location is pure data entry and the only
-- defence is making entry cheap: use_count ranks the autocomplete (D-5, FR-5.1).
CREATE TABLE location (
    id        INTEGER PRIMARY KEY,
    name      TEXT    NOT NULL UNIQUE CHECK (name <> ''),
    -- Unique because the slug's only job is to name files (D-7). Two locations
    -- sharing one would make 'st-aldo' mean two different places on disk.
    slug      TEXT    NOT NULL UNIQUE CHECK (slug <> ''),
    -- Always NULL today. Present from day one so D-5's map needs no migration.
    latitude  REAL,
    longitude REAL,
    use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0)
);

CREATE TABLE person (
    id        INTEGER PRIMARY KEY,
    name      TEXT    NOT NULL UNIQUE CHECK (name <> ''),
    use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0)
);

CREATE TABLE tag (
    id        INTEGER PRIMARY KEY,
    name      TEXT    NOT NULL UNIQUE CHECK (name <> ''),
    use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0)
);

CREATE TABLE photo_person (
    photo_id  INTEGER NOT NULL REFERENCES photo(id)  ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    PRIMARY KEY (photo_id, person_id)
);

CREATE INDEX idx_photo_person_person ON photo_person(person_id);

CREATE TABLE photo_tag (
    photo_id INTEGER NOT NULL REFERENCES photo(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tag(id)   ON DELETE CASCADE,
    PRIMARY KEY (photo_id, tag_id)
);

CREATE INDEX idx_photo_tag_tag ON photo_tag(tag_id);

-- A per-photo override of the session's location (FR-3.4). At most one, hence
-- photo_id alone as the primary key.
CREATE TABLE photo_location (
    photo_id    INTEGER PRIMARY KEY REFERENCES photo(id)    ON DELETE CASCADE,
    location_id INTEGER NOT NULL    REFERENCES location(id) ON DELETE CASCADE
);

CREATE INDEX idx_photo_location_location ON photo_location(location_id);
"""


_ARCHIVE = r"""
-- The Mac holds a working set; the archive drive holds everything. on_mac is
-- whether this photograph's files are still under library_root, which stops
-- being a given the moment *Free up Mac space* has run.
ALTER TABLE photo ADD COLUMN on_mac INTEGER NOT NULL DEFAULT 1
    CHECK (on_mac IN (0, 1));

-- NULL follows the keep_on_mac rule in config.toml; 1 always keeps the photo
-- on the Mac; 0 removes it at the next free-up whatever the rule says.
ALTER TABLE photo ADD COLUMN keep_on_mac INTEGER
    CHECK (keep_on_mac IS NULL OR keep_on_mac IN (0, 1));

CREATE INDEX idx_photo_on_mac ON photo(on_mac);

-- One row per file verified in the archive: a JPEG, a RAF or an .xmp, at the
-- same relative path it has under library_root. Written only after the copy on
-- the drive has been read back and hashed, so a row here *is* the answer to
-- "is this on my drive?" — and it can be given with the drive unplugged.
CREATE TABLE archive_file (
    path        TEXT    PRIMARY KEY
                        CHECK (path <> ''
                               AND path NOT LIKE '/%'
                               AND path NOT LIKE '_:/%'
                               AND instr(path, '\') = 0
                               AND instr(path, '..') = 0),
    sha256      TEXT    NOT NULL,
    bytes       INTEGER NOT NULL CHECK (bytes >= 0),
    verified_at TEXT    NOT NULL
);
"""


#: Every migration, in order. Append; never edit one that has shipped.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial schema", _INITIAL_SCHEMA),
    Migration(2, "archive drive and the Mac working set", _ARCHIVE),
)

#: The version a freshly migrated catalog ends up at.
SCHEMA_VERSION = MIGRATIONS[-1].version


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a catalog connection with the pragmas this app depends on.

    Does not migrate — :func:`migrate` is a separate, explicit step so that
    opening a catalog for inspection never rewrites it.
    """
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)

    # The pragmas have to run outside a transaction — SQLite silently ignores
    # `foreign_keys` inside one and refuses `journal_mode = WAL` outright — so
    # the connection starts in autocommit and is switched into explicit
    # transaction control once they are set.
    connection = sqlite3.connect(path, autocommit=True)
    connection.row_factory = sqlite3.Row
    # WAL survives reconnection (it is a property of the file), but setting it
    # every time costs nothing and means a catalog restored from a backup in
    # rollback-journal mode is corrected on first open.
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    # NORMAL is the documented-safe pairing with WAL: durable against a process
    # crash, and only at risk from losing power mid-write.
    connection.execute("PRAGMA synchronous = NORMAL")
    # The default page cache is 2 MB, and the catalog passes that at a few
    # thousand photographs — at 10,000 it is about 5 MB. A dashboard filter
    # (FR-7.4) runs seven aggregates over the same table in one operation, so a
    # cache smaller than the table makes each one evict what the last just
    # read: measured at 42 ms a facet with the default against 10 ms with this,
    # for the identical query. Negative means kibibytes rather than pages, and
    # SQLite allocates them only as they are used, so this is a ceiling and not
    # a reservation.
    connection.execute("PRAGMA cache_size = -16000")
    connection.autocommit = False
    return connection


def schema_version(connection: sqlite3.Connection) -> int:
    """The version this database is currently at. 0 means empty."""
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(connection: sqlite3.Connection,
            migrations: tuple[Migration, ...] = MIGRATIONS) -> list[Migration]:
    """Apply every migration newer than the database's current version.

    Returns the migrations actually applied, so a caller can log what happened
    to a catalog it did not create. Each one runs in its own transaction: a
    failure leaves the database at the last version that fully applied, never
    half way through one.
    """
    _check_ordering(migrations)

    current = schema_version(connection)
    if migrations and current > migrations[-1].version:
        raise MigrationError(
            f"Database is at schema version {current}, but this build only knows "
            f"up to {migrations[-1].version}. It was written by a newer version "
            f"of the app; upgrade rather than downgrade."
        )

    applied: list[Migration] = []
    for migration in migrations:
        if migration.version <= current:
            continue
        try:
            connection.executescript(migration.sql)
            # user_version takes no parameter binding, hence the f-string. The
            # value is an int straight off a Migration, never user input.
            connection.execute(f"PRAGMA user_version = {int(migration.version)}")
        except sqlite3.Error as exc:
            connection.rollback()
            raise MigrationError(
                f"Migration {migration.version} ({migration.name}) failed: {exc}"
            ) from exc
        connection.commit()
        applied.append(migration)

    return applied


def _check_ordering(migrations: tuple[Migration, ...]) -> None:
    """Catch a mis-numbered migration here rather than on the owner's data."""
    previous = 0
    for migration in migrations:
        if migration.version <= previous:
            raise MigrationError(
                f"Migrations must be strictly ascending from 1; "
                f"{migration.version} ({migration.name}) follows {previous}"
            )
        previous = migration.version
