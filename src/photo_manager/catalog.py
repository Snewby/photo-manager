"""The data layer: one :class:`Catalog` object over the SQLite schema.

Two rules this module is responsible for enforcing, both from
REQUIREMENTS.md §4.2:

* **Nothing absolute reaches the database.** ``library_path`` and ``raw_path``
  are relative to the configured library root, so moving the library to a bigger
  volume (C-2) is a config edit rather than a rewrite of every row.
  :meth:`Catalog.relative_path` and :meth:`Catalog.absolute_path` are the border
  crossings, and they delegate to :class:`~photo_manager.config.Settings`, which
  refuses paths that escape the root.
* **Writes are composable.** Import is transactional (§4.1, FR-6.4): a copy that
  fails partway must leave no catalog row. A single repository call commits
  itself, but anything inside :meth:`Catalog.transaction` commits once, at the
  end, or not at all.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from . import db
from .config import Settings
from .models import Location, Person, Photo, Session, Tag

__all__ = ["Catalog", "CatalogError"]


class CatalogError(Exception):
    """A catalog operation was asked for something it cannot do."""


# ----------------------------------------------------------------------
# Datetimes
# ----------------------------------------------------------------------
#
# Stored as ISO 8601 text. EXIF timestamps carry no timezone, so these are
# naive local times and comparing them as strings sorts them correctly — which
# is what every index on captured_at relies on.


def _to_iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dt.datetime):
        raise CatalogError(f"Expected a datetime, got {type(value).__name__}: {value!r}")
    return value.isoformat()


def _from_iso(value: str | None) -> dt.datetime | None:
    return None if value is None else dt.datetime.fromisoformat(value)


class Catalog:
    """The SQLite catalog, open and migrated.

    Use it as a context manager, or call :meth:`close` when done::

        with Catalog.open(settings) as catalog:
            photo = catalog.photos.add(photo)
    """

    def __init__(self, connection: sqlite3.Connection, settings: Settings) -> None:
        self._connection = connection
        self._settings = settings
        self._transaction_depth = 0

        self.photos = PhotoRepository(self)
        self.sessions = SessionRepository(self)
        self.locations = LocationRepository(self)
        self.people = PersonRepository(self)
        self.tags = TagRepository(self)

    @classmethod
    def open(cls, settings: Settings, *, verify_root: bool = True) -> Catalog:
        """Open the configured catalog, creating and migrating it as needed.

        *verify_root* performs §4.2's relocation check first, so a config that
        has not caught up with a moved library fails loudly here instead of
        producing an empty catalog that looks like data loss.
        """
        if verify_root:
            settings.verify_library_root()
        assert settings.database_path is not None  # filled in by Settings.__post_init__
        connection = db.connect(settings.database_path)
        db.migrate(connection)
        return cls(connection, settings)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying connection, for queries this layer does not wrap."""
        return self._connection

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def schema_version(self) -> int:
        return db.schema_version(self._connection)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[Catalog]:
        """Group writes so they land together or not at all.

        Outside one of these, each repository write commits on its own. Inside,
        they queue until the block exits cleanly; any exception rolls the whole
        block back. Nesting is allowed — only the outermost block commits — so a
        function that writes transactionally stays correct when called from
        something that is already in a transaction.
        """
        self._transaction_depth += 1
        try:
            yield self
        except BaseException:
            self._transaction_depth -= 1
            if self._transaction_depth == 0:
                self._connection.rollback()
            raise
        else:
            self._transaction_depth -= 1
            if self._transaction_depth == 0:
                self._connection.commit()

    def _after_write(self) -> None:
        """Commit, unless a :meth:`transaction` block is holding the write open."""
        if self._transaction_depth == 0:
            self._connection.commit()

    # ------------------------------------------------------------------
    # The library-root border
    # ------------------------------------------------------------------

    def relative_path(self, path: str | Path) -> str:
        """An absolute path inside the library → the string stored in a row."""
        return self._settings.relativise(path)

    def absolute_path(self, photo: Photo | str) -> Path:
        """A stored ``library_path`` (or a whole :class:`Photo`) → a real path."""
        library_path = photo.library_path if isinstance(photo, Photo) else photo
        return self._settings.resolve(library_path)

    def absolute_raw_path(self, photo: Photo) -> Path | None:
        """The RAF's real path, or None when the RAF was not kept (D-2)."""
        return None if photo.raw_path is None else self._settings.resolve(photo.raw_path)


class _Repository:
    """Shared plumbing: the connection, and when to commit."""

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog

    @property
    def _connection(self) -> sqlite3.Connection:
        return self._catalog.connection

    def _execute(self, sql: str, parameters: Sequence[object] = ()) -> sqlite3.Cursor:
        return self._connection.execute(sql, parameters)

    def _write(self, sql: str, parameters: Sequence[object] = ()) -> sqlite3.Cursor:
        cursor = self._connection.execute(sql, parameters)
        self._catalog._after_write()
        return cursor


class PhotoRepository(_Repository):
    """Rows in ``photo``, plus its three join tables."""

    #: Every column of ``photo``, in the order :meth:`from_row` reads them.
    #: Public, along with :meth:`from_row`, because a filtered query (FR-7.2)
    #: cannot be expressed through the methods below and has to build its own
    #: SQL — and a second copy of this list would be a second chance for the
    #: two to disagree about what a photo row is.
    COLUMNS: tuple[str, ...] = (
        "id", "content_hash", "library_path", "original_name", "has_raw",
        "raw_path", "captured_at", "captured_at_inferred", "imported_at",
        "width", "height", "camera_model", "lens", "exposure", "f_number",
        "iso", "focal_length", "orientation", "rating", "session_id",
    )

    @classmethod
    def column_list(cls, alias: str = "") -> str:
        """``COLUMNS`` as a SELECT list, optionally qualified by a table alias."""
        prefix = f"{alias}." if alias else ""
        return ", ".join(prefix + name for name in cls.COLUMNS)

    # -- rows ---------------------------------------------------------

    def add(self, photo: Photo) -> Photo:
        """Insert a photo and return it with its assigned id."""
        if photo.id is not None:
            raise CatalogError(f"Photo already has id {photo.id}; use update() instead")
        cursor = self._write(
            """
            INSERT INTO photo (
                content_hash, library_path, original_name, has_raw, raw_path,
                captured_at, captured_at_inferred, imported_at, width, height,
                camera_model, lens, exposure, f_number, iso, focal_length,
                orientation, rating, session_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                photo.content_hash,
                photo.library_path,
                photo.original_name,
                int(photo.has_raw),
                photo.raw_path,
                _to_iso(photo.captured_at),
                int(photo.captured_at_inferred),
                _to_iso(photo.imported_at),
                photo.width,
                photo.height,
                photo.camera_model,
                photo.lens,
                photo.exposure,
                photo.f_number,
                photo.iso,
                photo.focal_length,
                photo.orientation,
                photo.rating,
                photo.session_id,
            ),
        )
        return replace(photo, id=cursor.lastrowid)

    def update(self, photo: Photo) -> Photo:
        """Write every mutable field of an existing row."""
        if photo.id is None:
            raise CatalogError("Photo has no id; use add() instead")
        cursor = self._write(
            """
            UPDATE photo SET
                content_hash = ?, library_path = ?, original_name = ?,
                has_raw = ?, raw_path = ?, captured_at = ?,
                captured_at_inferred = ?, imported_at = ?, width = ?, height = ?,
                camera_model = ?, lens = ?, exposure = ?, f_number = ?, iso = ?,
                focal_length = ?, orientation = ?, rating = ?, session_id = ?
            WHERE id = ?
            """,
            (
                photo.content_hash,
                photo.library_path,
                photo.original_name,
                int(photo.has_raw),
                photo.raw_path,
                _to_iso(photo.captured_at),
                int(photo.captured_at_inferred),
                _to_iso(photo.imported_at),
                photo.width,
                photo.height,
                photo.camera_model,
                photo.lens,
                photo.exposure,
                photo.f_number,
                photo.iso,
                photo.focal_length,
                photo.orientation,
                photo.rating,
                photo.session_id,
                photo.id,
            ),
        )
        if cursor.rowcount == 0:
            raise CatalogError(f"No photo with id {photo.id}")
        return photo

    def get(self, photo_id: int) -> Photo | None:
        row = self._execute(
            f"SELECT {self.column_list()} FROM photo WHERE id = ?", (photo_id,)
        ).fetchone()
        return None if row is None else self.from_row(row)

    def by_hash(self, content_hash: str) -> Photo | None:
        """The duplicate check behind C-3 and FR-1.4."""
        row = self._execute(
            f"SELECT {self.column_list()} FROM photo WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return None if row is None else self.from_row(row)

    def hashes(self) -> set[str]:
        """Every content hash in the catalog.

        A card scan checks a thousand photos against this (FR-1.4); pulling the set
        once beats a query per frame.
        """
        return {row[0] for row in self._execute("SELECT content_hash FROM photo")}

    def original_names(self) -> dict[int, str]:
        """Photo id to the camera's original filename, for every row.

        This is the cheap half of card dedup (FR-1.4). A content hash costs
        14 MB of card reads per frame — about ten minutes for a full card on
        this reader — while a filename costs nothing and narrows a thousand frames
        to the handful worth hashing. It is evidence rather than proof, and
        :func:`photo_manager.cards.confirm_imports` supplies the proof.
        """
        return {
            row[0]: row[1]
            for row in self._execute("SELECT id, original_name FROM photo")
        }

    def by_library_path(self, library_path: str) -> Photo | None:
        row = self._execute(
            f"SELECT {self.column_list()} FROM photo WHERE library_path = ?", (library_path,)
        ).fetchone()
        return None if row is None else self.from_row(row)

    def all(self, *, newest_first: bool = True) -> list[Photo]:
        """Every photo. Newest first by default, as the grid shows them (FR-7.1)."""
        direction = "DESC" if newest_first else "ASC"
        rows = self._execute(
            f"SELECT {self.column_list()} FROM photo ORDER BY captured_at {direction}, id {direction}"
        )
        return [self.from_row(row) for row in rows]

    def unassigned(self) -> list[Photo]:
        """Every photo not yet in a session, oldest first.

        The input to grouping (§6.3) and, read as a count, the earliest form of
        FR-7.6's triage backlog: a photograph with no session is one nobody has
        looked at a day of yet.
        """
        rows = self._execute(
            f"SELECT {self.column_list()} FROM photo WHERE session_id IS NULL "
            "ORDER BY captured_at, id"
        )
        return [self.from_row(row) for row in rows]

    def in_session(self, session_id: int) -> list[Photo]:
        rows = self._execute(
            f"SELECT {self.column_list()} FROM photo WHERE session_id = ? "
            "ORDER BY captured_at, id",
            (session_id,),
        )
        return [self.from_row(row) for row in rows]

    def count(self) -> int:
        return int(self._execute("SELECT count(*) FROM photo").fetchone()[0])

    def delete(self, photo_id: int) -> bool:
        """Remove a catalog row. Touches no file on disk."""
        cursor = self._write("DELETE FROM photo WHERE id = ?", (photo_id,))
        return cursor.rowcount > 0

    # -- people, tags, location override ------------------------------

    def add_person(self, photo_id: int, person_id: int) -> None:
        self._write(
            "INSERT OR IGNORE INTO photo_person (photo_id, person_id) VALUES (?, ?)",
            (photo_id, person_id),
        )

    def remove_person(self, photo_id: int, person_id: int) -> None:
        self._write(
            "DELETE FROM photo_person WHERE photo_id = ? AND person_id = ?",
            (photo_id, person_id),
        )

    def people(self, photo_id: int) -> list[Person]:
        rows = self._execute(
            "SELECT p.id, p.name, p.use_count FROM person p "
            "JOIN photo_person pp ON pp.person_id = p.id "
            "WHERE pp.photo_id = ? ORDER BY p.name",
            (photo_id,),
        )
        return [Person(id=r["id"], name=r["name"], use_count=r["use_count"]) for r in rows]

    def add_tag(self, photo_id: int, tag_id: int) -> None:
        self._write(
            "INSERT OR IGNORE INTO photo_tag (photo_id, tag_id) VALUES (?, ?)",
            (photo_id, tag_id),
        )

    def remove_tag(self, photo_id: int, tag_id: int) -> None:
        self._write(
            "DELETE FROM photo_tag WHERE photo_id = ? AND tag_id = ?", (photo_id, tag_id)
        )

    def tags(self, photo_id: int) -> list[Tag]:
        rows = self._execute(
            "SELECT t.id, t.name, t.use_count FROM tag t "
            "JOIN photo_tag pt ON pt.tag_id = t.id "
            "WHERE pt.photo_id = ? ORDER BY t.name",
            (photo_id,),
        )
        return [Tag(id=r["id"], name=r["name"], use_count=r["use_count"]) for r in rows]

    def set_location(self, photo_id: int, location_id: int) -> None:
        """Override this photo's session location (FR-3.4). At most one."""
        self._write(
            "INSERT INTO photo_location (photo_id, location_id) VALUES (?, ?) "
            "ON CONFLICT(photo_id) DO UPDATE SET location_id = excluded.location_id",
            (photo_id, location_id),
        )

    def clear_location(self, photo_id: int) -> None:
        """Drop the override so the session's location applies again."""
        self._write("DELETE FROM photo_location WHERE photo_id = ?", (photo_id,))

    def location_override(self, photo_id: int) -> Location | None:
        """The override alone — not the session's location. See :meth:`location`."""
        row = self._execute(
            "SELECT l.id, l.name, l.slug, l.latitude, l.longitude, l.use_count "
            "FROM location l JOIN photo_location pl ON pl.location_id = l.id "
            "WHERE pl.photo_id = ?",
            (photo_id,),
        ).fetchone()
        return None if row is None else LocationRepository._to_location(row)

    def location(self, photo_id: int) -> Location | None:
        """The location that actually applies: the override, else the session's.

        This precedence *is* FR-3.4 — a session is tagged once and a single
        frame can disagree with it.
        """
        row = self._execute(
            """
            SELECT l.id, l.name, l.slug, l.latitude, l.longitude, l.use_count
            FROM photo p
            LEFT JOIN photo_location pl ON pl.photo_id = p.id
            LEFT JOIN session s         ON s.id = p.session_id
            JOIN location l             ON l.id = coalesce(pl.location_id, s.location_id)
            WHERE p.id = ?
            """,
            (photo_id,),
        ).fetchone()
        return None if row is None else LocationRepository._to_location(row)

    # -- mapping ------------------------------------------------------

    @staticmethod
    def from_row(row: sqlite3.Row) -> Photo:
        captured_at = _from_iso(row["captured_at"])
        imported_at = _from_iso(row["imported_at"])
        assert captured_at is not None and imported_at is not None  # NOT NULL columns
        return Photo(
            id=row["id"],
            content_hash=row["content_hash"],
            library_path=row["library_path"],
            original_name=row["original_name"],
            # has_raw is derived from raw_path on the model; the column is a
            # denormalisation for querying and a CHECK keeps the two in step.
            raw_path=row["raw_path"],
            captured_at=captured_at,
            captured_at_inferred=bool(row["captured_at_inferred"]),
            imported_at=imported_at,
            width=row["width"],
            height=row["height"],
            camera_model=row["camera_model"],
            lens=row["lens"],
            exposure=row["exposure"],
            f_number=row["f_number"],
            iso=row["iso"],
            focal_length=row["focal_length"],
            orientation=row["orientation"],
            rating=row["rating"],
            session_id=row["session_id"],
        )


class SessionRepository(_Repository):
    """Rows in ``session`` — the outings that tagging operates on (§6.3)."""

    def add(self, session: Session) -> Session:
        if session.id is not None:
            raise CatalogError(f"Session already has id {session.id}; use update()")
        cursor = self._write(
            "INSERT INTO session (started_at, ended_at, location_id, event) "
            "VALUES (?, ?, ?, ?)",
            (
                _to_iso(session.started_at),
                _to_iso(session.ended_at),
                session.location_id,
                session.event,
            ),
        )
        return replace(session, id=cursor.lastrowid)

    def update(self, session: Session) -> Session:
        if session.id is None:
            raise CatalogError("Session has no id; use add() instead")
        cursor = self._write(
            "UPDATE session SET started_at = ?, ended_at = ?, location_id = ?, "
            "event = ? WHERE id = ?",
            (
                _to_iso(session.started_at),
                _to_iso(session.ended_at),
                session.location_id,
                session.event,
                session.id,
            ),
        )
        if cursor.rowcount == 0:
            raise CatalogError(f"No session with id {session.id}")
        return session

    def get(self, session_id: int) -> Session | None:
        row = self._execute(
            "SELECT id, started_at, ended_at, location_id, event FROM session WHERE id = ?",
            (session_id,),
        ).fetchone()
        return None if row is None else self._from_row(row)

    def all(self, *, newest_first: bool = True) -> list[Session]:
        direction = "DESC" if newest_first else "ASC"
        rows = self._execute(
            "SELECT id, started_at, ended_at, location_id, event FROM session "
            f"ORDER BY started_at {direction}, id {direction}"
        )
        return [self._from_row(row) for row in rows]

    def assign(self, session_id: int | None, photo_ids: Sequence[int]) -> None:
        """Put photos in a session — or, with None, take them out of one."""
        with self._catalog.transaction():
            for photo_id in photo_ids:
                self._write(
                    "UPDATE photo SET session_id = ? WHERE id = ?", (session_id, photo_id)
                )

    def delete(self, session_id: int) -> bool:
        """Remove a session. Its photos survive, unassigned (ON DELETE SET NULL)."""
        cursor = self._write("DELETE FROM session WHERE id = ?", (session_id,))
        return cursor.rowcount > 0

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Session:
        started_at = _from_iso(row["started_at"])
        assert started_at is not None  # NOT NULL column
        return Session(
            id=row["id"],
            started_at=started_at,
            ended_at=_from_iso(row["ended_at"]),
            location_id=row["location_id"],
            event=row["event"],
        )


class _VocabularyRepository(_Repository):
    """Shared behaviour for ``location``, ``person`` and ``tag``.

    All three are name-keyed vocabularies with a ``use_count`` that ranks
    autocomplete, because with no GPS and no face recognition every one of them
    is typed by hand (C-1, FR-5.1, FR-5.2, FR-5.4).
    """

    _table: str
    _columns: str

    def get(self, row_id: int):  # type: ignore[no-untyped-def]
        row = self._execute(
            f"SELECT {self._columns} FROM {self._table} WHERE id = ?", (row_id,)
        ).fetchone()
        return None if row is None else self._to_model(row)

    def by_name(self, name: str):  # type: ignore[no-untyped-def]
        row = self._execute(
            f"SELECT {self._columns} FROM {self._table} WHERE name = ?", (name,)
        ).fetchone()
        return None if row is None else self._to_model(row)

    def all(self):  # type: ignore[no-untyped-def]
        """Most-used first — the order autocomplete wants (FR-5.1)."""
        rows = self._execute(
            f"SELECT {self._columns} FROM {self._table} ORDER BY use_count DESC, name"
        )
        return [self._to_model(row) for row in rows]

    def increment_use(self, row_id: int, by: int = 1) -> int:
        """Bump ``use_count`` and return the new value."""
        cursor = self._write(
            f"UPDATE {self._table} SET use_count = use_count + ? WHERE id = ?",
            (by, row_id),
        )
        if cursor.rowcount == 0:
            raise CatalogError(f"No {self._table} with id {row_id}")
        return int(
            self._execute(
                f"SELECT use_count FROM {self._table} WHERE id = ?", (row_id,)
            ).fetchone()[0]
        )

    def delete(self, row_id: int) -> bool:
        cursor = self._write(f"DELETE FROM {self._table} WHERE id = ?", (row_id,))
        return cursor.rowcount > 0

    def _to_model(self, row: sqlite3.Row):  # type: ignore[no-untyped-def]
        raise NotImplementedError


class LocationRepository(_VocabularyRepository):
    _table = "location"
    _columns = "id, name, slug, latitude, longitude, use_count"

    def add(self, location: Location) -> Location:
        if location.id is not None:
            raise CatalogError(f"Location already has id {location.id}; use update()")
        cursor = self._write(
            "INSERT INTO location (name, slug, latitude, longitude, use_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                location.name,
                location.slug,
                location.latitude,
                location.longitude,
                location.use_count,
            ),
        )
        return replace(location, id=cursor.lastrowid)

    def update(self, location: Location) -> Location:
        if location.id is None:
            raise CatalogError("Location has no id; use add() instead")
        cursor = self._write(
            "UPDATE location SET name = ?, slug = ?, latitude = ?, longitude = ?, "
            "use_count = ? WHERE id = ?",
            (
                location.name,
                location.slug,
                location.latitude,
                location.longitude,
                location.use_count,
                location.id,
            ),
        )
        if cursor.rowcount == 0:
            raise CatalogError(f"No location with id {location.id}")
        return location

    def by_slug(self, slug: str) -> Location | None:
        row = self._execute(
            f"SELECT {self._columns} FROM location WHERE slug = ?", (slug,)
        ).fetchone()
        return None if row is None else self._to_location(row)

    def _to_model(self, row: sqlite3.Row) -> Location:
        return self._to_location(row)

    @staticmethod
    def _to_location(row: sqlite3.Row) -> Location:
        return Location(
            id=row["id"],
            name=row["name"],
            slug=row["slug"],
            latitude=row["latitude"],
            longitude=row["longitude"],
            use_count=row["use_count"],
        )


class PersonRepository(_VocabularyRepository):
    _table = "person"
    _columns = "id, name, use_count"

    def add(self, person: Person) -> Person:
        if person.id is not None:
            raise CatalogError(f"Person already has id {person.id}; use update()")
        cursor = self._write(
            "INSERT INTO person (name, use_count) VALUES (?, ?)",
            (person.name, person.use_count),
        )
        return replace(person, id=cursor.lastrowid)

    def update(self, person: Person) -> Person:
        if person.id is None:
            raise CatalogError("Person has no id; use add() instead")
        cursor = self._write(
            "UPDATE person SET name = ?, use_count = ? WHERE id = ?",
            (person.name, person.use_count, person.id),
        )
        if cursor.rowcount == 0:
            raise CatalogError(f"No person with id {person.id}")
        return person

    def _to_model(self, row: sqlite3.Row) -> Person:
        return Person(id=row["id"], name=row["name"], use_count=row["use_count"])


class TagRepository(_VocabularyRepository):
    _table = "tag"
    _columns = "id, name, use_count"

    def add(self, tag: Tag) -> Tag:
        if tag.id is not None:
            raise CatalogError(f"Tag already has id {tag.id}; use update()")
        cursor = self._write(
            "INSERT INTO tag (name, use_count) VALUES (?, ?)", (tag.name, tag.use_count)
        )
        return replace(tag, id=cursor.lastrowid)

    def update(self, tag: Tag) -> Tag:
        if tag.id is None:
            raise CatalogError("Tag has no id; use add() instead")
        cursor = self._write(
            "UPDATE tag SET name = ?, use_count = ? WHERE id = ?",
            (tag.name, tag.use_count, tag.id),
        )
        if cursor.rowcount == 0:
            raise CatalogError(f"No tag with id {tag.id}")
        return tag

    def _to_model(self, row: sqlite3.Row) -> Tag:
        return Tag(id=row["id"], name=row["name"], use_count=row["use_count"])
