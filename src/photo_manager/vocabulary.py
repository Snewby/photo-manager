"""Locations, people and tags: the words, and how they get typed only once.

The camera records no GPS (C-1), and there is no face recognition (§2). So
**every location and every name in this library is typed by a person**, which
makes the cost of typing one the central design problem of tagging rather than
a detail of it. FR-5.1's answer is a single text field with autocomplete ranked
by use, creating a new value on first use and never a management screen to
visit first. This module is that answer's data layer.

Four things live here.

**Ranking that matches how a word gets reached for.** ``use_count`` is bumped
once per *act of tagging*, not once per photograph affected — tagging a session
of 54 frames with "St Aldo" is one reach for the word, the same as tagging a
single frame with it. Counting photographs would let one big day outrank five
separate trips somewhere, which is not what the person doing the typing means
by "the place I go most". Removing a tag decrements, because a ranking that
only ever grows makes a mistake permanent.

**Matching that forgives how a word gets typed.** "st aldo" finds "St Aldo" and
"malaga" finds "Málaga", because case and accents are typing, not meaning. A
name that matches an existing one after folding *is* that existing one — a
second row for the same place would defeat the autocomplete that exists to
prevent it.

**Slugs, which are only ever filenames.** D-7 puts one in the middle of every
imported name, and ``location.slug`` is UNIQUE (§5.1) precisely so one name on
disk cannot mean two places. Two differently-typed names can still want the
same slug — "St Aldo" and "St. Aldo" may well not be the
same town — so the generator **de-duplicates** rather than letting the insert
fail in front of somebody mid-tagging.

**A count that can be checked.** Everything ``use_count`` counts is a row that
still exists: a person on a photograph, a tag on a photograph, a location on a
session or overriding one photograph. So :func:`recount_uses` can rebuild the
whole column from those rows, and the test suite uses it to prove no sequence
of tagging and untagging drifts. The same instinct as NFR-2: a derived number
that cannot be re-derived is a number nobody can trust.

**What is deliberately not here: the XMP write-through.** FR-5.6 says every
change lands in SQLite *and* the sidecar, and these functions do only the first
half. That is WU-3.4's, where it is an acceptance criterion — but it means a
caller of this module alone leaves a sidecar stale, so every mutating function
returns the photographs it affected, and no caller can claim not to have known
which ones to rewrite. See EXECUTION_PLAN.md §8.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import replace

from .catalog import Catalog
from .models import Location, Person, Tag

__all__ = [
    "VocabularyError",
    "MAX_SLUG_LENGTH",
    "FALLBACK_SLUG",
    "fold",
    "normalise_name",
    "slugify",
    "slug_for",
    "resolve_location",
    "resolve_person",
    "resolve_tag",
    "suggest_locations",
    "suggest_people",
    "suggest_tags",
    "set_session_location",
    "clear_session_location",
    "set_photo_location",
    "clear_photo_location",
    "add_person_to_photo",
    "remove_person_from_photo",
    "add_tag_to_photo",
    "remove_tag_from_photo",
    "recount_uses",
]


class VocabularyError(Exception):
    """A name or a tagging operation was not usable."""


#: Longest slug that goes into a filename. A name is free to be as long as its
#: owner likes; the *filename* is not, and `YYYY-MM-DD_<slug>_DSCF1234.jpg` has
#: to stay comfortable inside a Windows path with the library root in front of
#: it. Truncation happens at a hyphen where there is one, so a cut slug is
#: still made of whole words.
MAX_SLUG_LENGTH = 40

#: What a name that contains nothing sluggable falls back to. A name written
#: entirely in a script this transliteration cannot reach — or in punctuation —
#: is still a perfectly good name, and refusing it would mean the app dictating
#: what a place may be called. Only the filename degrades; the name itself is
#: stored and displayed in full, and de-duplication makes the second one
#: ``place-2``.
FALLBACK_SLUG = "place"


# ----------------------------------------------------------------------
# Text
# ----------------------------------------------------------------------


def fold(text: str) -> str:
    """The comparison key: case and accents removed, whitespace collapsed.

    Used for "is this the same word" and for matching what has been typed so
    far. Not stored anywhere — it is a question asked of two strings, and
    keeping a folded copy in the schema would be a second thing to keep in step.
    """
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(without_marks.split())


def normalise_name(name: str) -> str:
    """What actually gets stored: trimmed, with runs of whitespace collapsed.

    Punctuation and case are left exactly as typed — "St. Aldo" is how somebody
    chose to write it and this layer has no business correcting it. Only the
    slug flattens those, and the slug is only a filename.
    """
    if not isinstance(name, str):
        raise VocabularyError(f"A name must be text, got {type(name).__name__}")
    cleaned = " ".join(name.split())
    if not cleaned:
        raise VocabularyError("A name cannot be empty or only whitespace")
    return cleaned


def slugify(name: str) -> str:
    """``'St. Aldo'`` → ``'st-aldo'``. The filename half of D-7.

    Lower-cased, accents flattened to their base letters, and every run of
    anything else turned into a single hyphen. Returns ``''`` when nothing
    survives — the caller decides what to do about that, because only
    :func:`slug_for` knows what else is already taken.
    """
    # Apostrophes are dropped rather than hyphenated, because they sit inside a
    # word rather than between two: "Queen's Park" is queens-park, not
    # queen-s-park. Every other punctuation mark separates, so it becomes one.
    folded = re.sub(r"['’ʼ]", "", fold(name))
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    if len(slug) <= MAX_SLUG_LENGTH:
        return slug

    cut = slug[:MAX_SLUG_LENGTH]
    # Prefer a whole-word cut, but not one so aggressive it throws the name
    # away: 'llanfairpwllgwyngyll' has no hyphen to cut at and is still a
    # better slug than nothing.
    if "-" in cut[MAX_SLUG_LENGTH // 2 :]:
        cut = cut.rpartition("-")[0]
    return cut.strip("-")


def slug_for(catalog: Catalog, name: str, *, exclude_id: int | None = None) -> str:
    """A slug for *name* that no other location has claimed.

    ``location.slug`` is UNIQUE because a slug's only job is to name files
    (D-7), and one filename meaning two places on disk is the failure that
    constraint exists to prevent. But two names can legitimately want the same
    slug — "St Aldo" and "St. Aldo" may be different
    towns — so the answer is a suffix, not a refusal and not a merge.

    *exclude_id* lets a location keep its own slug while being renamed.
    """
    base = slugify(name) or FALLBACK_SLUG
    for attempt in range(1, 1000):
        candidate = base if attempt == 1 else f"{base}-{attempt}"
        existing = catalog.locations.by_slug(candidate)
        if existing is None or existing.id == exclude_id:
            return candidate
    raise VocabularyError(f"Could not find a free slug for {name!r}")


# ----------------------------------------------------------------------
# Finding and creating (FR-5.1: no management screen)
# ----------------------------------------------------------------------


def _existing(rows: Sequence[Location | Person | Tag], name: str):  # type: ignore[no-untyped-def]
    """The row whose name means the same thing, ignoring case and accents."""
    wanted = fold(name)
    for row in rows:
        if fold(row.name) == wanted:
            return row
    return None


def resolve_location(catalog: Catalog, name: str) -> Location:
    """Find the location called *name*, or create it. Never a blank screen.

    Matching is by folded name, so typing "st aldo" when "St Aldo" exists
    returns St Aldo rather than making a second row for the same town — the
    duplicate autocomplete exists to prevent.

    **Resolving is not using.** ``use_count`` is untouched here: a name typed
    into a box and then abandoned is not a use of it, and the functions that
    actually apply a location are the ones that count.
    """
    cleaned = normalise_name(name)
    found = _existing(catalog.locations.all(), cleaned)
    if found is not None:
        return found
    return catalog.locations.add(
        Location(name=cleaned, slug=slug_for(catalog, cleaned))
    )


def resolve_person(catalog: Catalog, name: str) -> Person:
    """Find the person called *name*, or create them (FR-5.2)."""
    cleaned = normalise_name(name)
    found = _existing(catalog.people.all(), cleaned)
    return found if found is not None else catalog.people.add(Person(name=cleaned))


def resolve_tag(catalog: Catalog, name: str) -> Tag:
    """Find the tag called *name*, or create it (FR-5.4)."""
    cleaned = normalise_name(name)
    found = _existing(catalog.tags.all(), cleaned)
    return found if found is not None else catalog.tags.add(Tag(name=cleaned))


# ----------------------------------------------------------------------
# Autocomplete (FR-5.1, FR-5.2, FR-5.4)
# ----------------------------------------------------------------------


def _rank(rows: Sequence[Location | Person | Tag], query: str, limit: int):  # type: ignore[no-untyped-def]
    """Filter by what has been typed, then order by how it is likely meant.

    An empty query is the useful case rather than a degenerate one: an empty
    box should already be showing the places you go, so the first keystroke is
    often the last one.

    Where something has been typed, **how well it matches beats how often it is
    used**, in three tiers: the name starts with it, a word in the name starts
    with it, or it appears anywhere. Typing "st" and being offered "Bristol"
    first because Bristol is used more would be a ranking that fights the
    person using it. Within a tier, ``use_count`` decides, and the name breaks
    ties so the order never wobbles between identical calls.

    The empty query goes through the same sort rather than short-cutting to the
    repository's order, so both paths agree. They do not agree for free:
    SQLite's default collation sorts on UTF-8 bytes, which puts "Álava" after
    "Zennor" because its first byte is 0xC3, and an accent is not a reason to
    be last in a list.

    The rows are ranked in Python rather than in SQL because matching has to
    ignore accents, which SQLite's LIKE does not. The cost is one query over a
    vocabulary measured in tens of rows; NFR-1's 200 ms budget is about
    filtering 10,000 photographs, not about this.
    """
    wanted = fold(query)

    scored = []
    for row in rows:
        folded = fold(row.name)
        if not wanted:
            tier = 0
        elif folded.startswith(wanted):
            tier = 0
        elif any(word.startswith(wanted) for word in folded.split()):
            tier = 1
        elif wanted in folded:
            tier = 2
        else:
            continue
        scored.append((tier, -row.use_count, folded, row))
    scored.sort(key=lambda item: item[:3])
    return [row for *_, row in scored[:limit]]


def suggest_locations(catalog: Catalog, query: str = "", *, limit: int = 10) -> list[Location]:
    """Locations to offer for what has been typed so far (FR-5.1)."""
    return _rank(catalog.locations.all(), query, limit)


def suggest_people(catalog: Catalog, query: str = "", *, limit: int = 10) -> list[Person]:
    """People to offer for what has been typed so far (FR-5.2)."""
    return _rank(catalog.people.all(), query, limit)


def suggest_tags(catalog: Catalog, query: str = "", *, limit: int = 10) -> list[Tag]:
    """Tags to offer for what has been typed so far (FR-5.4)."""
    return _rank(catalog.tags.all(), query, limit)


# ----------------------------------------------------------------------
# Applying a location (FR-3.3, FR-3.4)
# ----------------------------------------------------------------------


def set_session_location(
    catalog: Catalog, session_id: int, name: str
) -> tuple[Location, list[int]]:
    """Put a location on a session. Returns it and the photographs it reaches.

    This is FR-3.3's primary path in its smallest form: one row changes and
    every photograph in the session inherits it, because inheritance is what
    :meth:`PhotoRepository.location` does rather than something copied onto
    each row. A frame that disagrees keeps its override (FR-3.4).
    """
    session = catalog.sessions.get(session_id)
    if session is None:
        raise VocabularyError(f"No session with id {session_id}")

    with catalog.transaction():
        location = resolve_location(catalog, name)
        # Setting the location a session already has is a no-op rather than a
        # second use. The count would survive either way — moving a use from a
        # row to itself nets to nothing — so this guard is about not issuing a
        # pointless UPDATE, and about the code saying what it means.
        if session.location_id != location.id:
            _move_use(catalog.locations, session.location_id, location.id)
            catalog.sessions.update(_with_location(session, location.id))
    return location, _session_photo_ids(catalog, session_id)


def clear_session_location(catalog: Catalog, session_id: int) -> list[int]:
    """Take the location off a session, and the use back off its count."""
    session = catalog.sessions.get(session_id)
    if session is None:
        raise VocabularyError(f"No session with id {session_id}")

    with catalog.transaction():
        if session.location_id is not None:
            _move_use(catalog.locations, session.location_id, None)
            catalog.sessions.update(_with_location(session, None))
    return _session_photo_ids(catalog, session_id)


def set_photo_location(
    catalog: Catalog, photo_id: int, name: str
) -> tuple[Location, list[int]]:
    """Override one photograph's session location (FR-3.4).

    The exception rather than the path — one frame of an outing shot somewhere
    else — which is why it is keyed on the photograph alone (§5.1) and why
    clearing it restores the session's answer rather than leaving a hole.
    """
    _require_photo(catalog, photo_id)
    with catalog.transaction():
        location = resolve_location(catalog, name)
        current = catalog.photos.location_override(photo_id)
        if current is None or current.id != location.id:
            _move_use(catalog.locations, None if current is None else current.id, location.id)
            assert location.id is not None
            catalog.photos.set_location(photo_id, location.id)
    return location, [photo_id]


def clear_photo_location(catalog: Catalog, photo_id: int) -> list[int]:
    """Drop the override so the session's location applies again (FR-3.4)."""
    _require_photo(catalog, photo_id)
    with catalog.transaction():
        current = catalog.photos.location_override(photo_id)
        if current is not None:
            _move_use(catalog.locations, current.id, None)
            catalog.photos.clear_location(photo_id)
    return [photo_id]


# ----------------------------------------------------------------------
# Applying people and tags (FR-5.2, FR-5.4)
# ----------------------------------------------------------------------


def add_person_to_photo(
    catalog: Catalog, photo_id: int, name: str
) -> tuple[Person, list[int]]:
    """Put a person on a photograph, creating them if this is the first time."""
    _require_photo(catalog, photo_id)
    with catalog.transaction():
        person = resolve_person(catalog, name)
        assert person.id is not None
        if not _already(catalog.photos.people(photo_id), person.id):
            catalog.photos.add_person(photo_id, person.id)
            catalog.people.increment_use(person.id)
    return person, [photo_id]


def remove_person_from_photo(catalog: Catalog, photo_id: int, person_id: int) -> list[int]:
    """Take a person off a photograph, and the use back off their count."""
    _require_photo(catalog, photo_id)
    with catalog.transaction():
        if _already(catalog.photos.people(photo_id), person_id):
            catalog.photos.remove_person(photo_id, person_id)
            _decrement(catalog.people, person_id)
    return [photo_id]


def add_tag_to_photo(catalog: Catalog, photo_id: int, name: str) -> tuple[Tag, list[int]]:
    """Put a freeform tag on a photograph, creating it if it is new (D-6)."""
    _require_photo(catalog, photo_id)
    with catalog.transaction():
        tag = resolve_tag(catalog, name)
        assert tag.id is not None
        if not _already(catalog.photos.tags(photo_id), tag.id):
            catalog.photos.add_tag(photo_id, tag.id)
            catalog.tags.increment_use(tag.id)
    return tag, [photo_id]


def remove_tag_from_photo(catalog: Catalog, photo_id: int, tag_id: int) -> list[int]:
    """Take a tag off a photograph, and the use back off its count."""
    _require_photo(catalog, photo_id)
    with catalog.transaction():
        if _already(catalog.photos.tags(photo_id), tag_id):
            catalog.photos.remove_tag(photo_id, tag_id)
            _decrement(catalog.tags, tag_id)
    return [photo_id]


# ----------------------------------------------------------------------
# Keeping the count honest
# ----------------------------------------------------------------------


def recount_uses(catalog: Catalog) -> dict[str, int]:
    """Rebuild every ``use_count`` from the rows that justify it.

    Everything the column counts is a link that still exists — a person or tag
    on a photograph, a location on a session or overriding one photograph — so
    the whole column is derivable, and a derived number that cannot be
    re-derived is one nobody can check. Returns how many rows were wrong, which
    is expected to be zero and is asserted to be in the test suite after long
    sequences of tagging and untagging.

    The same instinct as NFR-2's rebuildable catalog, one column down.
    """
    corrected = {"location": 0, "person": 0, "tag": 0}
    truth = {
        "location": (
            "SELECT id, (SELECT count(*) FROM session   WHERE location_id = location.id) "
            "         + (SELECT count(*) FROM photo_location WHERE location_id = location.id) "
            "FROM location"
        ),
        "person": (
            "SELECT id, (SELECT count(*) FROM photo_person WHERE person_id = person.id) "
            "FROM person"
        ),
        "tag": (
            "SELECT id, (SELECT count(*) FROM photo_tag WHERE tag_id = tag.id) FROM tag"
        ),
    }
    with catalog.transaction():
        for table, sql in truth.items():
            for row_id, actual in list(catalog.connection.execute(sql)):
                cursor = catalog.connection.execute(
                    f"UPDATE {table} SET use_count = ? WHERE id = ? AND use_count <> ?",
                    (actual, row_id, actual),
                )
                corrected[table] += cursor.rowcount
    return corrected


# ----------------------------------------------------------------------
# Small shared pieces
# ----------------------------------------------------------------------


def _require_photo(catalog: Catalog, photo_id: int) -> None:
    if catalog.photos.get(photo_id) is None:
        raise VocabularyError(f"No photograph with id {photo_id}")


def _already(rows: Iterable[Person | Tag], row_id: int) -> bool:
    return any(row.id == row_id for row in rows)


def _with_location(session, location_id: int | None):  # type: ignore[no-untyped-def]
    return replace(session, location_id=location_id)


def _session_photo_ids(catalog: Catalog, session_id: int) -> list[int]:
    return [p.id for p in catalog.photos.in_session(session_id) if p.id is not None]


def _move_use(repository, from_id: int | None, to_id: int | None) -> None:  # type: ignore[no-untyped-def]
    """One use moves from one row to another; either end may be nothing.

    Changing a session's location from St Aldo to Penzance is one act, and
    leaving the credit on St Aldo would rank a place by trips that were taken
    back.
    """
    if from_id is not None:
        _decrement(repository, from_id)
    if to_id is not None:
        repository.increment_use(to_id)


def _decrement(repository, row_id: int) -> None:  # type: ignore[no-untyped-def]
    """Take a use back, never below zero.

    The column is CHECKed non-negative (§5.1), so a floor here is the
    difference between an undo and a constraint failure in front of somebody
    mid-tagging. A count already at zero means something else has already
    corrected it — :func:`recount_uses` is how that is found, not a crash.
    """
    current = repository.get(row_id)
    if current is None:
        raise VocabularyError(f"Nothing to decrement: no row {row_id}")
    if current.use_count > 0:
        repository.increment_use(row_id, -1)
