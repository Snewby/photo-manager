"""Filtering the library, and counting what a filter would find.

This is the layer behind FR-7.2, FR-7.3 and FR-7.4 — and behind success
criterion 3, *"show me everything from St Aldo rated 4+" returns instantly*.

Four ideas, and the last two are the ones that took the thinking.

**A location is not a column.** FR-3.4 says a photograph's location is its
session's unless it overrides it, so filtering by place means filtering on
``coalesce(override, session)`` and never on a stored field. That expression is
:data:`_LOCATION`, it appears in exactly one place here, and it is the same
precedence :meth:`PhotoRepository.location` implements — the query layer
agreeing with the detail view is not a coincidence worth leaving to chance.

**AND and OR are decided by the data, not by taste.** A photograph has one
effective location and belongs to one session, so *two* locations can only
sensibly mean "either" — asking for both would return nothing, every time. It
can have several people and several tags, so two names there mean "both", which
is the question somebody actually asks of a photograph ("the ones with Alice
*and* Bob"). Hence: location and event are OR, people and tags are AND, and
the four dimensions still read identically in :class:`Query`.

**"Unset" is a value.** ``None`` in any of those four means *no location*, *no
event*, *nobody tagged*, *no tags*. Without it the facets would be useless on
this library today, where no photograph has a location yet and the whole
weight of the thing sits in the untagged pile — which is exactly what FR-7.4
exists to show. It is also FR-7.6's backlog seen from the query layer.

**Every facet count is the size of the result set you would get by clicking
it.** That single rule settles what would otherwise be a pile of separate
decisions, because it forces each dimension's count to be computed against the
right filter:

* For a single-valued dimension — location, event, rating, date, has-RAW — the
  count is taken with *that* dimension's own constraint lifted. Computed with
  it applied, a location facet would show the selected place and a column of
  zeroes, which tells nobody anything.
* For a multi-valued one — people, tags — the count is taken with everything
  applied, including the current selection, because clicking *adds*. "Alice, 54"
  then "Bob, 12" reads as the narrowing it actually is.

Both readings are the same sentence, so each :class:`FacetValue` carries the
:class:`Query` that selects it, and the suite asserts the rule directly: for
every value of every dimension, run that query and require the total to equal
the count that was displayed.

Nothing here writes. The catalog is opened, read, and left alone.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace

from .catalog import ARCHIVED, Catalog, PhotoRepository
from .models import Photo

__all__ = [
    "DIMENSIONS",
    "BacklogUnit",
    "FacetValue",
    "Facets",
    "Neighbours",
    "Query",
    "QueryError",
    "Results",
    "backlog",
    "count",
    "facets",
    "neighbours",
    "places_for",
    "search",
]


class QueryError(Exception):
    """A filter asked for something that cannot be expressed."""


#: The facet dimensions, in the order a filter panel would show them.
#: ``triage`` leads, because on this library today it is the only row with
#: anything to say (FR-7.6).
DIMENSIONS: tuple[str, ...] = (
    "triage",
    "location",
    "event",
    "person",
    "tag",
    "rating",
    "year",
    "raw",
    "storage",
)

#: Where a photograph's files are, as three values that never overlap: only
#: on this Mac (not yet on the archive drive), on both, or only on the drive
#: (*Free up Mac space* has let the Mac's copy go).
STORAGE: tuple[str, ...] = ("mac-only", "both", "drive-only")

_STORAGE_LABELS = {
    "mac-only": "Only on this Mac",
    "both": "On this Mac and the drive",
    "drive-only": "Only on the archive drive",
}

_STORAGE = (
    f"CASE WHEN p.on_mac = 0 THEN 'drive-only' WHEN {ARCHIVED} THEN 'both' "
    "ELSE 'mac-only' END"
)

#: FR-3.4's precedence, as SQL. A photograph's location is its own override if
#: it has one, otherwise its session's. Every query in this module that touches
#: place goes through this expression, so there is only one of it to be wrong.
_LOCATION = "coalesce(pl.location_id, s.location_id)"

#: FR-7.6's backlog, as SQL: a photograph **nobody has said anything about**.
#: No effective location, no event, nobody in it, nothing on it.
#:
#: Rating is deliberately not in this list. A star is a verdict and not a
#: description — it says a frame was worth keeping, not what it is or where it
#: was — and 13 of the real library's photographs carry one from culling while
#: being otherwise untouched. Counting them as dealt with would take the only
#: photographs anybody has looked at and hide them from the screen whose job is
#: to find the ones nobody has looked at.
_UNTAGGED = (
    f"({_LOCATION} IS NULL"
    " AND (s.event IS NULL OR trim(s.event) = '')"
    " AND NOT EXISTS (SELECT 1 FROM photo_person x WHERE x.photo_id = p.id)"
    " AND NOT EXISTS (SELECT 1 FROM photo_tag x WHERE x.photo_id = p.id))"
)

#: Photographs alone, for a query that asks nothing about place.
_FROM_PHOTOS = "FROM photo p"

#: Photographs with the two LEFT JOINs that make :data:`_LOCATION` and
#: ``s.event`` readable. Both are lookups on a primary key — ``photo_location``
#: is keyed on ``photo_id`` and ``session`` on its own id — so neither can add,
#: drop or duplicate a photo row, and a query means exactly the same thing with
#: them as without. See :func:`_from` for why it is still worth leaving them out.
_FROM_WITH_PLACE = """
FROM photo p
LEFT JOIN photo_location pl ON pl.photo_id = p.id
LEFT JOIN session        s  ON s.id = p.session_id
"""


#: Sentinel for :meth:`Query.clearing` — "the whole dimension". ``None`` cannot
#: stand for that here, because ``None`` is itself a value you can filter on
#: (FR-7.2b): clearing "no location" is a different act from clearing the
#: location filter altogether.
_EVERYTHING = object()


# ----------------------------------------------------------------------
# The filter
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Query:
    """What to show. Every field empty means the whole library (FR-7.1).

    Dimensions combine with AND: a query with a location and a rating wants
    photographs that satisfy both (FR-7.3). *Within* a dimension see the module
    docstring — ``locations`` and ``events`` are "either", ``people`` and
    ``tags`` are "both" — and ``None`` in any of the four means "unset".
    """

    #: Location ids, or ``None`` for photographs with no location at all.
    locations: tuple[int | None, ...] = ()
    #: Event text as stored on the session, or ``None`` for no event.
    events: tuple[str | None, ...] = ()
    #: Person ids, all of which must be present; ``None`` means "nobody tagged".
    people: tuple[int | None, ...] = ()
    #: Tag ids, all of which must be present; ``None`` means "no tags".
    tags: tuple[int | None, ...] = ()

    #: Minimum rating, inclusive — FR-7.3's "rated 4+".
    rating_min: int | None = None

    #: Capture dates, both inclusive. Whole days: a photograph taken at any
    #: time on ``until`` is inside the range.
    since: dt.date | None = None
    until: dt.date | None = None

    #: True for photographs whose RAF was kept (D-2), False for those without.
    has_raw: bool | None = None

    #: True for FR-7.6's backlog — nothing said about it at all — and False
    #: for its complement. One field rather than four "unset" values spelled
    #: out together, because *this* is the question the triage screen asks and
    #: a filter somebody has to assemble from four parts is not first-class.
    untagged: bool | None = None

    #: One of :data:`STORAGE`: where the files are — the Mac, the archive
    #: drive, or both.
    storage: str | None = None

    def __post_init__(self) -> None:
        if self.storage is not None and self.storage not in STORAGE:
            raise QueryError(
                f"storage must be one of {', '.join(STORAGE)}, got {self.storage!r}"
            )
        if self.rating_min is not None and not 0 <= self.rating_min <= 5:
            raise QueryError(f"rating_min must be 0-5, got {self.rating_min}")
        if self.since is not None and self.until is not None and self.since > self.until:
            raise QueryError(
                f"since ({self.since}) is after until ({self.until}); "
                "that range can never match anything"
            )

    @property
    def is_empty(self) -> bool:
        """True when nothing is filtered — the whole library, newest first."""
        return self == Query()

    def selecting(self, dimension: str, value: object) -> Query:
        """The query you get by clicking *value* in *dimension*.

        Single-valued dimensions replace what was there; ``person`` and ``tag``
        add to it, which is what makes their counts a narrowing rather than a
        list of alternatives. See the module docstring.
        """
        if dimension not in DIMENSIONS:
            raise QueryError(f"Unknown facet dimension {dimension!r}")
        match dimension:
            case "triage":
                return replace(self, untagged=bool(value))
            case "location":
                return replace(self, locations=(value,))  # type: ignore[arg-type]
            case "event":
                return replace(self, events=(value,))  # type: ignore[arg-type]
            case "person":
                return replace(self, people=_added(self.people, value))
            case "tag":
                return replace(self, tags=_added(self.tags, value))
            case "rating":
                return replace(self, rating_min=value)  # type: ignore[arg-type]
            case "year":
                year = int(value)  # type: ignore[call-overload]
                return replace(self, since=dt.date(year, 1, 1), until=dt.date(year, 12, 31))
            case "storage":
                return replace(self, storage=str(value))
            case _:  # "raw"
                return replace(self, has_raw=bool(value))


    def selects(self, dimension: str, value: object) -> bool:
        """Is *value* already part of this query?

        A filter panel has to draw a chosen row differently from an offered
        one, and working that out from the outside would be a second copy of
        everything :meth:`selecting` knows. Kept here so the two cannot drift:
        the suite asserts that selecting a value makes this true, for every
        value of every dimension.
        """
        if dimension not in DIMENSIONS:
            raise QueryError(f"Unknown facet dimension {dimension!r}")
        match dimension:
            case "triage":
                return self.untagged is not None and bool(self.untagged) == bool(value)
            case "location":
                return value in self.locations
            case "event":
                return value in self.events
            case "person":
                return value in self.people
            case "tag":
                return value in self.tags
            case "rating":
                return self.rating_min == value
            case "year":
                year = int(value)  # type: ignore[call-overload]
                return (self.since == dt.date(year, 1, 1)
                        and self.until == dt.date(year, 12, 31))
            case "storage":
                return self.storage == value
            case _:  # "raw"
                return self.has_raw is not None and bool(self.has_raw) == bool(value)

    def clearing(self, dimension: str, value: object = _EVERYTHING) -> Query:
        """The query you get by taking *value* — or the whole dimension — off.

        Every filter that can be put on has to come off again, and from the
        panel that means a cross next to the row that is on. With no *value*
        the dimension is cleared entirely, which is what a "any location" row
        does.
        """
        if dimension not in DIMENSIONS:
            raise QueryError(f"Unknown facet dimension {dimension!r}")
        match dimension:
            case "triage":
                return replace(self, untagged=None)
            case "location":
                return replace(self, locations=_removed(self.locations, value))
            case "event":
                return replace(self, events=_removed(self.events, value))  # type: ignore[arg-type]
            case "person":
                return replace(self, people=_removed(self.people, value))
            case "tag":
                return replace(self, tags=_removed(self.tags, value))
            case "rating":
                return replace(self, rating_min=None)
            case "year":
                return replace(self, since=None, until=None)
            case "storage":
                return replace(self, storage=None)
            case _:  # "raw"
                return replace(self, has_raw=None)


def _removed(existing: tuple[object, ...], value: object) -> tuple[object, ...]:
    """Drop one value, or every one of them for the sentinel."""
    if value is _EVERYTHING:
        return ()
    return tuple(v for v in existing if v != value)


def _added(existing: tuple[int | None, ...], value: object) -> tuple[int | None, ...]:
    """Append unless it is already selected, so a second click is not a change."""
    return existing if value in existing else (*existing, value)  # type: ignore[return-value]


# ----------------------------------------------------------------------
# What comes back
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Results:
    """One page of photographs, and how many there were in total."""

    #: Matching photographs, newest first unless asked otherwise (FR-7.1).
    photos: tuple[Photo, ...]
    #: Every photograph the query matches, not just this page.
    total: int
    offset: int = 0
    #: The page size asked for, or None for "all of them".
    limit: int | None = None

    def __len__(self) -> int:
        return len(self.photos)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.photos)


@dataclass(frozen=True, slots=True)
class Neighbours:
    """Where one photograph sits in a filtered set, and what is either side.

    The detail view's whole navigation (WU-4.3, FR-7.5). It is computed by the
    server rather than read off whatever the grid happens to have loaded,
    because those are two different sets: a link to
    ``/library?rating=4&photo=812`` arrives with no grid at all, and paging
    means the frame after the one on screen may not have been fetched yet.
    Stepping therefore walks *the filter*, which is what somebody who asked for
    "rated 4+" means by the next photograph.

    :attr:`previous` is the newer one, because the grid is newest first
    (FR-7.1) and "previous" means the tile before this one on screen.
    """

    #: 1-based place in the filtered set, or None when this photograph is not
    #: in it at all — which happens the moment an edit takes it out of its own
    #: filter, and is a state worth being able to say rather than fake.
    position: int | None
    #: How many photographs the filter matches, so a view can say "4 of 7".
    total: int
    #: The next one up the page (newer), or None at the top.
    previous: int | None = None
    #: The next one down the page (older), or None at the bottom.
    next: int | None = None


@dataclass(frozen=True, slots=True)
class BacklogUnit:
    """One thing to name — which is an **outing**, not always a whole day.

    **Not by photograph**, because tagging is a session's action (FR-3.3): a
    backlog listing a few hundred photographs would be as many things to do where there are
    24, and typing a location fifty-four times is the failure the session
    exists to prevent.

    **And not by day either**, which is what this was first built as and what
    the refactor on 2026-09-22 corrected. A day that has been split into a
    morning and an evening is *two* things to name — that is the entire reason
    somebody split it — so a list keyed on the date could only offer one form
    for two answers, and had to send them elsewhere. Keyed on the session, the
    same day simply appears twice, and the screen that shows the work can do
    all of it.

    So a unit is either an existing session with nothing said about it, or the
    photographs of one day that are in no session at all. A half-grouped day
    is both, which is two honest rows rather than one apologetic one.
    """

    day: dt.date
    #: The outing this is, or None for photographs not yet in one.
    session_id: int | None
    #: Every photograph in this unit, tagged or not.
    photographs: int
    #: How many of them nobody has said anything about.
    untagged: int
    #: When the unit's photographs start and end, so a row that shares a date
    #: with another says which part of the day it is.
    started_at: dt.datetime
    ended_at: dt.datetime
    #: A few untagged frames from it, earliest first, so the screen can show
    #: what this *was* rather than only when it was.
    covers: tuple[int, ...] = ()

    @property
    def grouped(self) -> bool:
        """True when somebody has already decided this shape (FR-3.2a)."""
        return self.session_id is not None

    @property
    def partly(self) -> bool:
        """True when some of it has been dealt with and some has not."""
        return 0 < self.untagged < self.photographs


@dataclass(frozen=True, slots=True)
class FacetValue:
    """One row of a filter panel: a thing to click, and what clicking gives.

    ``count`` is not "how many have this" in the abstract — it is the size of
    the result set :attr:`query` returns. The two are the same thing said twice,
    and the suite checks they agree for every value of every dimension.
    """

    #: The id, text, number or flag this row stands for; ``None`` means unset.
    value: object
    #: What to put on screen.
    label: str
    count: int
    #: The query this row describes, ready to be run.
    query: Query = field(compare=False, repr=False, default_factory=Query)


@dataclass(frozen=True, slots=True)
class Facets:
    """Counts for every dimension, ready for FR-7.4's filter panel."""

    locations: tuple[FacetValue, ...] = ()
    events: tuple[FacetValue, ...] = ()
    people: tuple[FacetValue, ...] = ()
    tags: tuple[FacetValue, ...] = ()
    ratings: tuple[FacetValue, ...] = ()
    years: tuple[FacetValue, ...] = ()
    raw: tuple[FacetValue, ...] = ()
    triage: tuple[FacetValue, ...] = ()
    storage: tuple[FacetValue, ...] = ()

    _BY_DIMENSION = {
        "triage": "triage",
        "location": "locations",
        "event": "events",
        "person": "people",
        "tag": "tags",
        "rating": "ratings",
        "year": "years",
        "raw": "raw",
        "storage": "storage",
    }

    def of(self, dimension: str) -> tuple[FacetValue, ...]:
        """One dimension by name, for code that walks :data:`DIMENSIONS`."""
        try:
            return getattr(self, self._BY_DIMENSION[dimension])  # type: ignore[no-any-return]
        except KeyError:
            raise QueryError(f"Unknown facet dimension {dimension!r}") from None


# ----------------------------------------------------------------------
# Clauses
# ----------------------------------------------------------------------
#
# One fragment per dimension, kept apart rather than pre-joined, because a
# facet count is the same query with one of them missing. Assembling the WHERE
# is therefore the only place that decides which are in play, and there is no
# second implementation of "the current filter" to drift from the first.


def _clauses(query: Query) -> dict[str, tuple[str, list[object]]]:
    """Every dimension's SQL fragment, keyed by dimension name."""
    clauses: dict[str, tuple[str, list[object]]] = {}

    if query.locations:
        clauses["location"] = _either(_LOCATION, query.locations)
    if query.events:
        clauses["event"] = _either("s.event", query.events)
    if query.people:
        clauses["person"] = _both("photo_person", "person_id", query.people)
    if query.tags:
        clauses["tag"] = _both("photo_tag", "tag_id", query.tags)
    if query.rating_min is not None:
        clauses["rating"] = ("p.rating >= ?", [query.rating_min])

    # Both ends are inclusive days compared against a full ISO timestamp, and
    # the two ends are not symmetrical. '2026-09-06' sorts before every time on
    # that day, including '2026-09-06T00:00:00', so the lower bound takes the
    # whole day whether it is written >= or > — they cannot be told apart while
    # captured_at always carries a time, which datetime.isoformat guarantees.
    # The upper bound has no such luck: <= the bare date would drop every frame
    # after midnight, which is all of them, so it is < the following midnight.
    bounds: list[str] = []
    parameters: list[object] = []
    if query.since is not None:
        bounds.append("p.captured_at >= ?")
        parameters.append(query.since.isoformat())
    if query.until is not None:
        bounds.append("p.captured_at < ?")
        parameters.append((query.until + dt.timedelta(days=1)).isoformat())
    if bounds:
        clauses["year"] = (" AND ".join(bounds), parameters)

    if query.has_raw is not None:
        clauses["raw"] = ("p.has_raw = ?", [int(query.has_raw)])

    if query.untagged is not None:
        clauses["triage"] = (_UNTAGGED if query.untagged else f"NOT {_UNTAGGED}", [])

    if query.storage is not None:
        clauses["storage"] = (f"({_STORAGE}) = ?", [query.storage])

    return clauses


def _either(expression: str, values: Sequence[object]) -> tuple[str, list[object]]:
    """OR across a single-valued dimension, with ``None`` meaning unset."""
    present = [v for v in values if v is not None]
    parts: list[str] = []
    parameters: list[object] = []
    if present:
        parts.append(f"{expression} IN ({', '.join('?' * len(present))})")
        parameters.extend(present)
    if any(v is None for v in values):
        parts.append(f"{expression} IS NULL")
    return "(" + " OR ".join(parts) + ")", parameters


def _both(table: str, column: str, values: Sequence[object]) -> tuple[str, list[object]]:
    """AND across a multi-valued dimension, with ``None`` meaning none at all.

    One EXISTS per value rather than a join, so the number of names asked for
    never multiplies the rows being counted.
    """
    parts: list[str] = []
    parameters: list[object] = []
    for value in values:
        if value is None:
            parts.append(f"NOT EXISTS (SELECT 1 FROM {table} x WHERE x.photo_id = p.id)")
        else:
            parts.append(
                f"EXISTS (SELECT 1 FROM {table} x "
                f"WHERE x.photo_id = p.id AND x.{column} = ?)"
            )
            parameters.append(value)
    return "(" + " AND ".join(parts) + ")", parameters


def _from(query: Query, *, without: Iterable[str] = (), place: bool = False) -> str:
    """The FROM clause: the joins to place, only when something reads them.

    They are free of meaning (see :data:`_FROM_WITH_PLACE`) and expensive of
    time. Two random page lookups per row do not fit a page cache the way a
    sequential scan does, and at 10,000 photographs that turned a 4 ms grouped
    scan into 42 ms — for a query about ratings, which has nothing to do with
    where a photograph was taken.

    *place* forces them on for the two facets that select a location or an
    event whether or not they filter on one.
    """
    lifted = set(without)
    active = set(_clauses(query)) - lifted
    if place or active & {"location", "event", "triage"}:
        return _FROM_WITH_PLACE
    return _FROM_PHOTOS


def _where(
    query: Query, *, without: Iterable[str] = ()
) -> tuple[str, list[object]]:
    """The WHERE clause, optionally with some dimensions lifted out of it."""
    lifted = set(without)
    clauses = _clauses(query)
    parts = [sql for name, (sql, _) in clauses.items() if name not in lifted]
    parameters = [
        value
        for name, (_, values) in clauses.items()
        if name not in lifted
        for value in values
    ]
    if not parts:
        return "", []
    return "WHERE " + " AND ".join(parts), parameters


# ----------------------------------------------------------------------
# Reading
# ----------------------------------------------------------------------


def search(
    catalog: Catalog,
    query: Query = Query(),
    *,
    limit: int | None = None,
    offset: int = 0,
    newest_first: bool = True,
) -> Results:
    """Photographs matching *query*, newest first (FR-7.1).

    *total* is the whole matching set regardless of *limit*, because a grid
    that shows 200 of something still has to say how many the something is.
    """
    if offset < 0:
        raise QueryError(f"offset must not be negative, got {offset}")
    if limit is not None and limit < 0:
        raise QueryError(f"limit must not be negative, got {limit}")

    where, parameters = _where(query)
    direction = "DESC" if newest_first else "ASC"

    sql = (
        f"SELECT {PhotoRepository.column_list('p')} {_from(query)} {where} "
        f"ORDER BY p.captured_at {direction}, p.id {direction}"
    )
    page = list(parameters)
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        page.extend((limit, offset))
    elif offset:
        # SQLite has no OFFSET without a LIMIT; -1 is its documented "no limit".
        sql += " LIMIT -1 OFFSET ?"
        page.append(offset)

    rows = catalog.connection.execute(sql, page)
    photos = tuple(PhotoRepository.from_row(row) for row in rows)
    return Results(
        photos=photos,
        total=count(catalog, query),
        offset=offset,
        limit=limit,
    )


def places_for(catalog: Catalog, photo_ids: Sequence[int]) -> dict[int, str]:
    """The location that applies to each of these photographs, by id.

    A grid of two hundred tiles wants two hundred place names, and asking for
    them one at a time is two hundred round trips through three joins. It lives
    here rather than in the page that wants it because :data:`_LOCATION` is the
    only place FR-3.4's precedence is written down, and a caller reimplementing
    ``coalesce(override, session)`` to save an import is exactly how the grid
    and the detail view would come to disagree about where a photograph was.

    Photographs with no location are absent from the result rather than mapped
    to ``None`` — "no location" is a thing the caller already has to draw
    differently, and a key that is there but empty invites it not to.
    """
    if not photo_ids:
        return {}
    holes = ", ".join("?" * len(photo_ids))
    rows = catalog.connection.execute(
        f"""
        SELECT p.id AS photo_id, l.name AS name
        {_FROM_WITH_PLACE}
        JOIN location l ON l.id = {_LOCATION}
        WHERE p.id IN ({holes})
        """,
        list(photo_ids),
    )
    return {int(row["photo_id"]): row["name"] for row in rows}


def count(catalog: Catalog, query: Query = Query()) -> int:
    """How many photographs match, without building any of them."""
    where, parameters = _where(query)
    row = catalog.connection.execute(
        f"SELECT count(*) {_from(query)} {where}", parameters
    ).fetchone()
    return int(row[0])


def neighbours(
    catalog: Catalog, photo: Photo, query: Query = Query()
) -> Neighbours:
    """What is either side of *photo* within *query*, and where it sits.

    Three small queries rather than a page of results, because the alternative
    — fetching the whole filtered set to find one row's index — is the 418 ms
    NFR-1 explicitly does not cover. The ordering is :func:`search`'s, and it
    is a compound one, so the comparison is a row value:
    ``(captured_at, id) < (?, ?)``. Comparing on ``captured_at`` alone would
    lose its way among frames of a burst, which share a timestamp to the
    second and are exactly where somebody presses the arrow key most.

    A photograph that does not match the filter still gets neighbours — the
    two frames the filter *does* hold on either side of where it sits in time
    — and a position of None. That is the state after rating a photograph out
    of its own "rated 4+" view: it is still on screen, it is no longer in the
    set, and next still has somewhere to go.
    """
    assert photo.id is not None
    where, parameters = _where(query)
    place = _from(query)
    at = (photo.captured_at.isoformat(), photo.id)

    def adjacent(comparison: str, direction: str) -> int | None:
        clause = _and(where, f"(p.captured_at, p.id) {comparison} (?, ?)")
        row = catalog.connection.execute(
            f"SELECT p.id {place} {clause} "
            f"ORDER BY p.captured_at {direction}, p.id {direction} LIMIT 1",
            [*parameters, *at],
        ).fetchone()
        return None if row is None else int(row[0])

    newer = catalog.connection.execute(
        f"SELECT count(*) {place} {_and(where, '(p.captured_at, p.id) > (?, ?)')}",
        [*parameters, *at],
    ).fetchone()[0]
    inside = catalog.connection.execute(
        f"SELECT 1 {place} {_and(where, 'p.id = ?')} LIMIT 1",
        [*parameters, photo.id],
    ).fetchone()

    return Neighbours(
        position=None if inside is None else int(newer) + 1,
        total=count(catalog, query),
        previous=adjacent(">", "ASC"),
        next=adjacent("<", "DESC"),
    )


def backlog(catalog: Catalog, *, covers: int = 5) -> tuple[BacklogUnit, ...]:
    """Everything with something still to say about it, newest first (FR-7.6).

    Two queries and no row objects: a backlog is a screen full of counts, and
    building hundreds of photographs to display two dozen numbers is the shape NFR-1
    warns about one screen over. Units with nothing left to do are not
    returned at all — the list *is* the work, so it has to empty as the work
    is done.

    **The grouping key is the session, falling back to the day.** An outing is
    what gets named, so it is what a row has to be; photographs not in one yet
    have only their day to be gathered by. Since a session never crosses
    midnight (FR-3.1) the two keys never fight, and grouping on both at once
    is the whole of it.

    ``captured_at`` is naive local ISO text (§5.1), so the day is its first ten
    characters. That is the same substring D-4 puts in the folder name, which
    is what makes this list and Explorer agree about which day a photograph
    belongs to.
    """
    if covers < 0:
        raise QueryError(f"covers must not be negative, got {covers}")

    rows = _rows(
        catalog,
        f"""
        SELECT p.session_id AS session_id,
               substr(p.captured_at, 1, 10) AS day,
               count(*) AS photographs,
               sum(CASE WHEN {_UNTAGGED} THEN 1 ELSE 0 END) AS untagged,
               min(p.captured_at) AS started_at,
               max(p.captured_at) AS ended_at
        {_FROM_WITH_PLACE}
        GROUP BY p.session_id, day
        HAVING untagged > 0
        ORDER BY day DESC, started_at DESC
        """,
        [],
    )
    thumbnails = _covers(catalog, covers)
    return tuple(
        BacklogUnit(
            day=dt.date.fromisoformat(row["day"]),
            session_id=None if row["session_id"] is None else int(row["session_id"]),
            photographs=int(row["photographs"]),
            untagged=int(row["untagged"]),
            started_at=dt.datetime.fromisoformat(row["started_at"]),
            ended_at=dt.datetime.fromisoformat(row["ended_at"]),
            covers=tuple(thumbnails.get((row["session_id"], row["day"]), ())),
        )
        for row in rows
    )


def _covers(catalog: Catalog, wanted: int) -> dict[tuple[int | None, str], list[int]]:
    """The first few untagged photographs of each unit, keyed the same way.

    One windowed query rather than one per unit, because a library with two
    hundred untagged days would otherwise be two hundred round trips to draw
    one screen.
    """
    if wanted == 0:
        return {}
    rows = _rows(
        catalog,
        f"""
        SELECT session_id, day, id FROM (
            SELECT p.session_id AS session_id,
                   substr(p.captured_at, 1, 10) AS day,
                   p.id AS id,
                   row_number() OVER (
                       PARTITION BY p.session_id, substr(p.captured_at, 1, 10)
                       ORDER BY p.captured_at, p.id
                   ) AS place
            {_FROM_WITH_PLACE}
            WHERE {_UNTAGGED}
        )
        WHERE place <= ?
        ORDER BY day DESC, place
        """,
        [wanted],
    )
    found: dict[tuple[int | None, str], list[int]] = {}
    for row in rows:
        found.setdefault((row["session_id"], row["day"]), []).append(int(row["id"]))
    return found


def facets(catalog: Catalog, query: Query = Query()) -> Facets:
    """Counts for every dimension against *query* (FR-7.4).

    Each count is the size of the result set that value's query returns — see
    the module docstring for why that one rule decides which constraints are in
    play for which dimension.
    """
    built = {
        "triage": _triage_facets(catalog, query),
        "location": _location_facets(catalog, query),
        "event": _event_facets(catalog, query),
        "person": _link_facets(catalog, query, "person"),
        "tag": _link_facets(catalog, query, "tag"),
        "rating": _rating_facets(catalog, query),
        "year": _year_facets(catalog, query),
        "raw": _raw_facets(catalog, query),
        "storage": _storage_facets(catalog, query),
    }
    kept = {
        dimension: _keep_selected(catalog, query, dimension, values)
        for dimension, values in built.items()
    }
    return Facets(
        triage=kept["triage"],
        locations=kept["location"],
        events=kept["event"],
        people=kept["person"],
        tags=kept["tag"],
        ratings=kept["rating"],
        years=kept["year"],
        raw=kept["raw"],
        storage=kept["storage"],
    )


def _keep_selected(catalog: Catalog, query: Query, dimension: str,
                   values: tuple[FacetValue, ...]) -> tuple[FacetValue, ...]:
    """Put back a filter that is on but matches nothing.

    A value with no rows is normally left out, because a row that returns
    nothing when clicked is a dead link rather than information. A value that
    is *currently selected* is the exception, and the difference matters: it is
    not an offer, it is the answer to "why am I looking at an empty screen",
    and it is the thing the reader needs in order to take it off again.

    Reachable by asking for it — a filter typed into the address bar, or a
    saved link to a rating nothing has any more. The panel itself never offers
    a zero.
    """
    missing = [
        value for value in _selected_values(query, dimension)
        if not any(existing.value == value for existing in values)
    ]
    if not missing:
        return values
    # ``query`` unchanged is the right link for these: the value is already
    # selected, so clicking it is a no-op — and the count is 0 because the
    # current filter, which contains it, matches nothing. FR-7.4a therefore
    # still holds, and the suite checks it rather than taking the argument.
    kept = values + tuple(
        FacetValue(
            value=value,
            label=_label_for(catalog, dimension, value),
            count=0,
            query=query,
        )
        for value in missing
    )
    if dimension in ("rating", "year"):
        # These two are ordered by their value rather than by their count, so
        # a row appended at the end would sit out of sequence.
        return tuple(sorted(kept, key=lambda v: int(v.value), reverse=True))  # type: ignore[call-overload]
    return kept


def _selected_values(query: Query, dimension: str) -> tuple[object, ...]:
    match dimension:
        case "triage":
            return () if query.untagged is None else (query.untagged,)
        case "location":
            return query.locations
        case "event":
            return query.events
        case "person":
            return query.people
        case "tag":
            return query.tags
        case "rating":
            return () if query.rating_min in (None, 0) else (query.rating_min,)
        case "year":
            year = query.since.year if query.since is not None else None
            return (year,) if year is not None and query.selects("year", year) else ()
        case "storage":
            return () if query.storage is None else (query.storage,)
        case _:  # "raw"
            return () if query.has_raw is None else (query.has_raw,)


def _label_for(catalog: Catalog, dimension: str, value: object) -> str:
    """What to call a value that has no rows to name it.

    A vocabulary row can be missing outright — a link to a location that has
    since been deleted — so this says so rather than drawing a blank chip.
    """
    match dimension:
        case "triage":
            return "Nothing said yet" if value else "Something said"
        case "location":
            row = None if value is None else catalog.locations.get(int(value))  # type: ignore[call-overload]
            return "No location" if value is None else (row.name if row else "Unknown place")
        case "event":
            return "No event" if value is None else str(value)
        case "person":
            row = None if value is None else catalog.people.get(int(value))  # type: ignore[call-overload]
            return "Nobody tagged" if value is None else (row.name if row else "Unknown person")
        case "tag":
            row = None if value is None else catalog.tags.get(int(value))  # type: ignore[call-overload]
            return "No tags" if value is None else (row.name if row else "Unknown tag")
        case "rating":
            return "5 stars" if value == 5 else f"{value}+ stars"
        case "year":
            return str(value)
        case "storage":
            return _STORAGE_LABELS.get(str(value), str(value))
        case _:  # "raw"
            return "RAW kept" if value else "JPEG only"


def _rows(catalog: Catalog, sql: str, parameters: Sequence[object]) -> list[sqlite3.Row]:
    return list(catalog.connection.execute(sql, parameters))


def _ranked(values: list[FacetValue]) -> tuple[FacetValue, ...]:
    """Biggest first — FR-7.4 is about seeing where the weight actually sits."""
    return tuple(sorted(values, key=lambda v: (-v.count, v.label)))


def _location_facets(catalog: Catalog, query: Query) -> tuple[FacetValue, ...]:
    where, parameters = _where(query, without=["location"])
    rows = _rows(
        catalog,
        f"""
        SELECT {_LOCATION} AS effective_location, l.name AS name, count(*) AS n
        {_from(query, without=["location"], place=True)}
        LEFT JOIN location l ON l.id = {_LOCATION}
        {where}
        GROUP BY effective_location
        """,
        parameters,
    )
    return _ranked(
        [
            FacetValue(
                value=row["effective_location"],
                label=row["name"] if row["effective_location"] is not None else "No location",
                count=int(row["n"]),
                query=query.selecting("location", row["effective_location"]),
            )
            for row in rows
        ]
    )


def _event_facets(catalog: Catalog, query: Query) -> tuple[FacetValue, ...]:
    where, parameters = _where(query, without=["event"])
    rows = _rows(
        catalog,
        f"SELECT s.event AS event, count(*) AS n "
        f'{_from(query, without=["event"], place=True)} {where} GROUP BY s.event',
        parameters,
    )
    return _ranked(
        [
            FacetValue(
                value=row["event"],
                label=row["event"] if row["event"] is not None else "No event",
                count=int(row["n"]),
                query=query.selecting("event", row["event"]),
            )
            for row in rows
        ]
    )


#: The two multi-valued dimensions, which differ only in which table they read.
_LINKS = {
    "person": ("photo_person", "person_id", "person", "Nobody tagged"),
    "tag": ("photo_tag", "tag_id", "tag", "No tags"),
}


def _link_facets(catalog: Catalog, query: Query, dimension: str) -> tuple[FacetValue, ...]:
    """People or tags: counted *inside* the current filter, because clicking adds.

    Counted off the link table rather than by joining ``photo`` to it. Every
    link row has a photograph — the foreign key says so — so with nothing
    filtered there is no need to visit one, and with something filtered the
    restriction is a set of ids rather than a row lookup per link. At 10,000
    photographs and 40,000 links that is the difference between about 70 ms a
    dimension and about 8 ms.
    """
    link_table, column, name_table, none_label = _LINKS[dimension]
    where, parameters = _where(query)

    restriction = (
        f"WHERE link.photo_id IN (SELECT p.id {_from(query)} {where})" if where else ""
    )
    rows = _rows(
        catalog,
        f"""
        -- count(*) rather than count(DISTINCT p.id): (photo_id, {column}) is
        -- this table's primary key, so a photograph is in a group at most once
        -- already and the DISTINCT would only buy a temporary b-tree.
        SELECT v.id AS id, v.name AS name, count(*) AS n
        FROM {link_table} link
        JOIN {name_table} v ON v.id = link.{column}
        {restriction}
        GROUP BY v.id
        """,
        parameters,
    )
    values = [
        FacetValue(
            value=row["id"],
            label=row["name"],
            count=int(row["n"]),
            query=query.selecting(dimension, row["id"]),
        )
        for row in rows
    ]

    # The unset bucket does not come out of a GROUP BY over the links, because
    # a photograph with no links has no row in them to be grouped.
    none_where = _and(
        where, f"NOT EXISTS (SELECT 1 FROM {link_table} x WHERE x.photo_id = p.id)"
    )
    none_count = int(
        _rows(
            catalog,
            f"SELECT count(*) AS n {_from(query)} {none_where}",
            parameters,
        )[0]["n"]
    )
    if none_count:
        values.append(
            FacetValue(
                value=None,
                label=none_label,
                count=none_count,
                query=query.selecting(dimension, None),
            )
        )
    return _ranked(values)


def _and(where: str, extra: str) -> str:
    return f"{where} AND {extra}" if where else f"WHERE {extra}"


def _rating_facets(catalog: Catalog, query: Query) -> tuple[FacetValue, ...]:
    """Thresholds, not star counts, because ``rating_min`` is what you click into."""
    where, parameters = _where(query, without=["rating"])
    rows = _rows(
        catalog,
        f'SELECT p.rating AS rating, count(*) AS n {_from(query, without=["rating"])} '
        f"{where} GROUP BY p.rating",
        parameters,
    )
    by_rating = {int(row["rating"]): int(row["n"]) for row in rows}

    values: list[FacetValue] = []
    for threshold in range(1, 6):
        at_or_above = sum(n for rating, n in by_rating.items() if rating >= threshold)
        if at_or_above:
            values.append(
                FacetValue(
                    value=threshold,
                    label="5 stars" if threshold == 5 else f"{threshold}+ stars",
                    count=at_or_above,
                    query=query.selecting("rating", threshold),
                )
            )
    # Descending: 5 first, because "my best of these" is what a rating filter
    # is reached for. Not _ranked() — a threshold list in count order is a list
    # in a scrambled order, since the counts are nested by construction.
    return tuple(reversed(values))


def _year_facets(catalog: Catalog, query: Query) -> tuple[FacetValue, ...]:
    where, parameters = _where(query, without=["year"])
    rows = _rows(
        catalog,
        f"""
        SELECT substr(p.captured_at, 1, 4) AS year, count(*) AS n
        {_from(query, without=["year"])} {where}
        GROUP BY year ORDER BY year DESC
        """,
        parameters,
    )
    return tuple(
        FacetValue(
            value=int(row["year"]),
            label=row["year"],
            count=int(row["n"]),
            query=query.selecting("year", int(row["year"])),
        )
        for row in rows
    )


def _triage_facets(catalog: Catalog, query: Query) -> tuple[FacetValue, ...]:
    """FR-7.6's backlog as a row you can click (FR-7.4).

    Shaped like :func:`_raw_facets` — two values, counted with their own
    constraint lifted because clicking one switches to it — but grouped on an
    expression rather than a column, since "nothing said about it" is not
    something the schema stores.
    """
    where, parameters = _where(query, without=["triage"])
    rows = _rows(
        catalog,
        f"SELECT {_UNTAGGED} AS untagged, count(*) AS n "
        f'{_from(query, without=["triage"], place=True)} {where} GROUP BY untagged',
        parameters,
    )
    by_flag = {bool(row["untagged"]): int(row["n"]) for row in rows}
    return tuple(
        FacetValue(
            value=flag,
            label="Nothing said yet" if flag else "Something said",
            count=by_flag[flag],
            query=query.selecting("triage", flag),
        )
        for flag in (True, False)
        if by_flag.get(flag)
    )


def _storage_facets(catalog: Catalog, query: Query) -> tuple[FacetValue, ...]:
    """Where the files are — offered only once there is an archive drive,
    because without one every photograph is on the Mac and the row would
    say nothing."""
    if catalog.settings.archive_root is None and query.storage is None:
        return ()
    where, parameters = _where(query, without=["storage"])
    rows = _rows(
        catalog,
        f'SELECT {_STORAGE} AS storage, count(*) AS n {_from(query, without=["storage"])} '
        f"{where} GROUP BY storage",
        parameters,
    )
    by_value = {row["storage"]: int(row["n"]) for row in rows}
    return tuple(
        FacetValue(
            value=value,
            label=_STORAGE_LABELS[value],
            count=by_value[value],
            query=query.selecting("storage", value),
        )
        for value in STORAGE
        if by_value.get(value)
    )


def _raw_facets(catalog: Catalog, query: Query) -> tuple[FacetValue, ...]:
    where, parameters = _where(query, without=["raw"])
    rows = _rows(
        catalog,
        f'SELECT p.has_raw AS has_raw, count(*) AS n {_from(query, without=["raw"])} '
        f"{where} GROUP BY p.has_raw",
        parameters,
    )
    by_flag = {bool(row["has_raw"]): int(row["n"]) for row in rows}
    return tuple(
        FacetValue(
            value=flag,
            label="RAW kept" if flag else "JPEG only",
            count=by_flag[flag],
            query=query.selecting("raw", flag),
        )
        for flag in (True, False)
        if by_flag.get(flag)
    )
