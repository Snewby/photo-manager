"""Filtering the library, and counting what a filter would find (WU-4.1).

Four things carry the weight here.

**A location is not a column.** FR-3.4 makes a photograph's location its
session's unless it overrides it, so every location test has an overriding
frame in it and asserts from both sides — the frame is missing from the
session's place *and* present in its own.

**AND and OR are a claim about the data.** Two locations can only mean
"either", because nothing has two; two people can only usefully mean "both".
Both readings are tested, and so is the one that makes the distinction matter:
asking for two locations must not return nothing.

**Facet counts are checked against a library counted by hand**, written out at
the top of this file so the expected numbers are readable rather than derived
by the code under test. Then the general rule — *a facet count is the size of
the result set you get by clicking it* — is asserted over every value of every
dimension, and again over every value of every dimension of the query that
clicking produced. That is 200-odd assertions the fixture's authors never had
to think of.

**NFR-1's 200 ms is measured, not asserted.** 10,000 synthetic photographs with
locations, sessions, people, tags and ratings on them, and the timing taken as
a *minimum* over repeated runs, because EXECUTION_PLAN.md §8 records two
occasions on this machine where a single reading meant nothing.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Iterator
from dataclasses import replace

import pytest

from photo_manager.catalog import Catalog
from photo_manager.config import Settings
from photo_manager.models import Location, Person, Photo, Session, Tag
from photo_manager.queries import (
    DIMENSIONS,
    Query,
    QueryError,
    backlog,
    count,
    facets,
    neighbours,
    places_for,
    search,
)

# The FROM clause is an optimisation with a correctness claim attached, so it
# is tested directly rather than only through the answers it happens to give.
from photo_manager.queries import (  # noqa: E402 — grouped with its siblings
    _FROM_PHOTOS,
    _FROM_WITH_PLACE,
    _from,
    _where,
)

# ----------------------------------------------------------------------
# A library small enough to count by hand
# ----------------------------------------------------------------------
#
#   St Aldo, 'Saturday market', 2026-09-06 ........... 6 frames  (p1..p6)
#       p6 overridden to Bristol, so the session is 5 of them
#   Bristol, 'Harbour walk',    2026-09-07 ........... 4 frames  (p7..p10)
#   St Aldo, no event,          2026-07-13 ........... 2 frames  (p11, p12)
#   no session at all,          2025-12-24 ........... 3 frames  (p13..p15)
#
#   effective location   St Aldo 7   Bristol 5   none 3
#   event                Saturday market 6   Harbour walk 4   none 5
#   people               Alice 6   Bob 4   both 3   nobody 8
#   tags                 market 6   france 3   both 2   none 8
#   ratings              5:2  4:2  3:1  2:1  1:1  0:8
#   RAW kept             2 (p1, p7)
#   years                2026: 12   2025: 3

#: (id, day, hour, rating, raw, people, tags) for every frame in the fixture.
FRAMES = [
    # p1..p6 — the St Aldo market, 2026-09-06
    (1, "2026-09-06", 10, 5, True, ("Alice", "Bob"), ("market",)),
    (2, "2026-09-06", 11, 4, False, ("Alice", "Bob"), ("market",)),
    (3, "2026-09-06", 12, 4, False, ("Alice",), ("market", "france")),
    (4, "2026-09-06", 13, 3, False, ("Alice",), ("market", "france")),
    (5, "2026-09-06", 14, 2, False, (), ("market",)),
    (6, "2026-09-06", 15, 1, False, (), ("market",)),  # overridden to Bristol
    # p7..p10 — the Bristol harbour walk, 2026-09-07
    (7, "2026-09-07", 10, 5, True, ("Alice",), ("france",)),
    (8, "2026-09-07", 11, 0, False, ("Alice", "Bob"), ()),
    (9, "2026-09-07", 12, 0, False, ("Bob",), ()),
    (10, "2026-09-07", 13, 0, False, (), ()),
    # p11, p12 — St Aldo again, no event
    (11, "2026-07-13", 9, 0, False, (), ()),
    (12, "2026-07-13", 10, 0, False, (), ()),
    # p13..p15 — never looked at: no session, so no location and no event
    (13, "2025-12-24", 9, 0, False, (), ()),
    (14, "2025-12-24", 10, 0, False, (), ()),
    (15, "2025-12-24", 11, 0, False, (), ()),
]

TOTAL = len(FRAMES)


class Fixture:
    """The hand-counted library, plus the ids needed to ask about it."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.locations: dict[str, int] = {}
        self.people: dict[str, int] = {}
        self.tags: dict[str, int] = {}
        self.sessions: dict[str, int] = {}
        self.photos: dict[int, int] = {}  # fixture number -> catalog id

    def location(self, name: str) -> int:
        return self.locations[name]

    def person(self, name: str) -> int:
        return self.people[name]

    def tag(self, name: str) -> int:
        return self.tags[name]

    def photo(self, number: int) -> int:
        return self.photos[number]


@pytest.fixture
def library(catalog: Catalog) -> Fixture:
    """The library described in the table above, in a throwaway catalog."""
    fixture = Fixture(catalog)

    for name, slug in (("St Aldo", "st-aldo"), ("Bristol", "bristol")):
        added = catalog.locations.add(Location(name=name, slug=slug))
        assert added.id is not None
        fixture.locations[name] = added.id
    for name in ("Alice", "Bob"):
        person = catalog.people.add(Person(name=name))
        assert person.id is not None
        fixture.people[name] = person.id
    for name in ("market", "france"):
        tag = catalog.tags.add(Tag(name=name))
        assert tag.id is not None
        fixture.tags[name] = tag.id

    for key, day, location, event in (
        ("market", "2026-09-06", "St Aldo", "Saturday market"),
        ("harbour", "2026-09-07", "Bristol", "Harbour walk"),
        ("july", "2026-07-13", "St Aldo", None),
    ):
        session = catalog.sessions.add(
            Session(
                started_at=dt.datetime.fromisoformat(f"{day}T09:00"),
                ended_at=dt.datetime.fromisoformat(f"{day}T18:00"),
                location_id=fixture.locations[location],
                event=event,
            )
        )
        assert session.id is not None
        fixture.sessions[key] = session.id

    session_of = {
        "2026-09-06": "market",
        "2026-09-07": "harbour",
        "2026-07-13": "july",
    }
    for number, day, hour, rating, raw, people, tags in FRAMES:
        photo = catalog.photos.add(
            Photo(
                content_hash=f"{number:064x}",
                library_path=f"{day[:4]}/{day}/{day}_DSCF{number:04d}.jpg",
                original_name=f"DSCF{number:04d}.JPG",
                captured_at=dt.datetime.fromisoformat(f"{day}T{hour:02d}:00"),
                imported_at=dt.datetime(2026, 9, 21, 12, 0),
                rating=rating,
                raw_path=f"{day[:4]}/{day}/{day}_DSCF{number:04d}.raf" if raw else None,
                session_id=(
                    fixture.sessions[session_of[day]] if day in session_of else None
                ),
            )
        )
        assert photo.id is not None
        fixture.photos[number] = photo.id
        for name in people:
            catalog.photos.add_person(photo.id, fixture.people[name])
        for name in tags:
            catalog.photos.add_tag(photo.id, fixture.tags[name])

    # FR-3.4: one frame of the market was taken somewhere else.
    catalog.photos.set_location(fixture.photos[6], fixture.locations["Bristol"])
    return fixture


def numbers(library: Fixture, query: Query) -> set[int]:
    """The fixture numbers a query returns, which is what the table above lists."""
    by_id = {photo_id: number for number, photo_id in library.photos.items()}
    return {by_id[photo.id] for photo in search(library.catalog, query) if photo.id}


def labelled(values: tuple) -> dict[str, int]:  # type: ignore[type-arg]
    return {value.label: value.count for value in values}


# ----------------------------------------------------------------------
# Nothing filtered
# ----------------------------------------------------------------------


def test_an_empty_query_is_the_whole_library_newest_first(library: Fixture) -> None:
    """FR-7.1. The default is not 'nothing' — it is everything, in grid order."""
    results = search(library.catalog, Query())

    assert results.total == TOTAL
    assert len(results.photos) == TOTAL
    assert [photo.original_name for photo in results][:2] == ["DSCF0010.JPG", "DSCF0009.JPG"]
    assert Query().is_empty


def test_oldest_first_is_available_and_is_the_exact_reverse(library: Fixture) -> None:
    newest = [photo.id for photo in search(library.catalog, Query())]
    oldest = [photo.id for photo in search(library.catalog, Query(), newest_first=False)]

    assert oldest == list(reversed(newest))


# ----------------------------------------------------------------------
# One dimension at a time
# ----------------------------------------------------------------------


def test_location_filtering_follows_the_override_not_the_session(library: Fixture) -> None:
    """FR-3.4 from both sides: p6 leaves St Aldo *and* arrives in Bristol.

    This is the test that would go green on a query reading `session.location_id`
    alone, if it only asserted the St Aldo count — the frame would still be
    missing from Bristol and nobody would notice.
    """
    st_aldo = numbers(library, Query(locations=(library.location("St Aldo"),)))
    bristol = numbers(library, Query(locations=(library.location("Bristol"),)))

    assert st_aldo == {1, 2, 3, 4, 5, 11, 12}
    assert bristol == {6, 7, 8, 9, 10}
    assert 6 not in st_aldo


def test_photographs_with_no_location_are_askable_for(library: Fixture) -> None:
    """The untagged pile is a value, not the absence of one — FR-7.6's backlog."""
    assert numbers(library, Query(locations=(None,))) == {13, 14, 15}


def test_two_locations_mean_either_because_nothing_has_two(library: Fixture) -> None:
    both = Query(locations=(library.location("St Aldo"), library.location("Bristol")))

    assert count(library.catalog, both) == 12
    # And the reading that would be wrong: intersecting them returns nothing at
    # all, which is why this dimension cannot be AND.
    assert numbers(library, both) == numbers(
        library, Query(locations=(library.location("St Aldo"),))
    ) | numbers(library, Query(locations=(library.location("Bristol"),)))


def test_a_location_can_be_asked_for_together_with_having_none(library: Fixture) -> None:
    query = Query(locations=(library.location("Bristol"), None))

    assert numbers(query=query, library=library) == {6, 7, 8, 9, 10, 13, 14, 15}


def test_event_filtering_reaches_every_frame_of_the_session(library: Fixture) -> None:
    assert numbers(library, Query(events=("Saturday market",))) == {1, 2, 3, 4, 5, 6}
    assert numbers(library, Query(events=("Harbour walk",))) == {7, 8, 9, 10}


def test_no_event_covers_both_a_session_without_one_and_no_session(
    library: Fixture,
) -> None:
    """Two different absences that look the same to somebody filtering."""
    assert numbers(library, Query(events=(None,))) == {11, 12, 13, 14, 15}


def test_two_people_mean_both_of_them(library: Fixture) -> None:
    """A photograph can hold several, so the useful question is the intersection."""
    alice = numbers(library, Query(people=(library.person("Alice"),)))
    bob = numbers(library, Query(people=(library.person("Bob"),)))
    together = numbers(
        library, Query(people=(library.person("Alice"), library.person("Bob")))
    )

    assert alice == {1, 2, 3, 4, 7, 8}
    assert bob == {1, 2, 8, 9}
    assert together == alice & bob == {1, 2, 8}


def test_nobody_tagged_is_a_value_too(library: Fixture) -> None:
    assert numbers(library, Query(people=(None,))) == {5, 6, 10, 11, 12, 13, 14, 15}


def test_two_tags_mean_both_of_them(library: Fixture) -> None:
    assert numbers(library, Query(tags=(library.tag("market"),))) == {1, 2, 3, 4, 5, 6}
    assert numbers(library, Query(tags=(library.tag("france"),))) == {3, 4, 7}
    assert numbers(
        library, Query(tags=(library.tag("market"), library.tag("france")))
    ) == {3, 4}
    assert numbers(library, Query(tags=(None,))) == {8, 9, 10, 11, 12, 13, 14, 15}


def test_rating_is_a_threshold_not_an_exact_match(library: Fixture) -> None:
    """FR-7.3 asks for 'rated 4+', which is the question a rating gets asked."""
    assert numbers(library, Query(rating_min=4)) == {1, 2, 3, 7}
    assert numbers(library, Query(rating_min=5)) == {1, 7}
    assert count(library.catalog, Query(rating_min=1)) == 7
    assert count(library.catalog, Query(rating_min=0)) == TOTAL


def test_a_date_range_takes_whole_days_at_both_ends(library: Fixture) -> None:
    """The stored value is a timestamp; the filter is a date, and both ends count."""
    one_day = Query(since=dt.date(2026, 9, 6), until=dt.date(2026, 9, 6))
    two_days = Query(since=dt.date(2026, 9, 6), until=dt.date(2026, 9, 7))

    assert numbers(library, one_day) == {1, 2, 3, 4, 5, 6}
    assert numbers(library, two_days) == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10}
    # An open end is open: everything from that date on, or up to it.
    assert count(library.catalog, Query(since=dt.date(2026, 1, 1))) == 12
    assert count(library.catalog, Query(until=dt.date(2025, 12, 31))) == 3


def test_the_last_frame_of_a_day_is_inside_the_range(library: Fixture) -> None:
    """The bug this guards is an upper bound compared against the bare date.

    `'2026-09-06T15:00' < '2026-09-06'` is false, so a naive `<=` on the day
    drops every frame after midnight — which is all of them.
    """
    single = Query(since=dt.date(2026, 9, 6), until=dt.date(2026, 9, 6))
    latest = max(search(library.catalog, single), key=lambda photo: photo.captured_at)

    assert latest.captured_at.hour == 15


def test_has_raw_filters_both_ways(library: Fixture) -> None:
    assert numbers(library, Query(has_raw=True)) == {1, 7}
    assert count(library.catalog, Query(has_raw=False)) == TOTAL - 2


# ----------------------------------------------------------------------
# Combining — FR-7.3
# ----------------------------------------------------------------------


def test_show_me_everything_from_st_aldo_rated_four_plus(library: Fixture) -> None:
    """Success criterion 3, word for word.

    p6 is the interesting exclusion: it is in the St Aldo session but overrides
    its location, so a query that read the session would wrongly include it —
    except that it is rated 1 and would be filtered out anyway. Which is why
    the second assertion drops the rating and checks the same thing without a
    second reason to pass.
    """
    st_aldo = library.location("St Aldo")

    assert numbers(library, Query(locations=(st_aldo,), rating_min=4)) == {1, 2, 3}
    assert 6 not in numbers(library, Query(locations=(st_aldo,)))


def test_filters_from_different_dimensions_all_have_to_hold(library: Fixture) -> None:
    query = Query(
        locations=(library.location("St Aldo"),),
        people=(library.person("Alice"),),
        tags=(library.tag("france"),),
        rating_min=3,
        since=dt.date(2026, 1, 1),
        until=dt.date(2026, 12, 31),
        has_raw=False,
    )

    assert numbers(library, query) == {3, 4}


def test_a_combination_across_dimensions_can_narrow_to_one_or_to_none(
    library: Fixture,
) -> None:
    query = Query(locations=(library.location("Bristol"),), tags=(library.tag("market"),))

    assert count(library.catalog, query) == 1  # p6: overridden to Bristol, tagged market
    assert (
        count(library.catalog, Query(locations=(None,), rating_min=5)) == 0
    )


def test_adding_a_filter_never_grows_the_result(library: Fixture) -> None:
    """The property that makes combining trustworthy, checked over every facet."""
    base = Query(since=dt.date(2026, 1, 1))
    baseline = count(library.catalog, base)

    for dimension in DIMENSIONS:
        for value in facets(library.catalog, base).of(dimension):
            if dimension in ("year", "month"):
                continue  # a year or month *replaces* the range it was computed without
            assert count(library.catalog, value.query) <= baseline


# ----------------------------------------------------------------------
# Paging
# ----------------------------------------------------------------------


def test_a_page_carries_the_total_of_the_whole_set_not_of_the_page(
    library: Fixture,
) -> None:
    """A grid showing 5 of 15 still has to be able to say 15."""
    page = search(library.catalog, Query(), limit=5)

    assert len(page.photos) == 5
    assert page.total == TOTAL
    assert page.limit == 5 and page.offset == 0


def test_paging_through_visits_every_photograph_exactly_once(library: Fixture) -> None:
    seen: list[int] = []
    for offset in range(0, TOTAL, 4):
        seen.extend(photo.id for photo in search(library.catalog, Query(), limit=4, offset=offset) if photo.id)

    assert seen == [photo.id for photo in search(library.catalog, Query())]
    assert len(set(seen)) == TOTAL


def test_an_offset_with_no_limit_still_skips(library: Fixture) -> None:
    """SQLite has no OFFSET without a LIMIT, so this is a real thing to get wrong."""
    rest = search(library.catalog, Query(), offset=13)

    assert len(rest.photos) == 2
    assert rest.total == TOTAL


# ----------------------------------------------------------------------
# Facet counts, against the table at the top of this file
# ----------------------------------------------------------------------


def test_facet_counts_on_a_library_counted_by_hand(library: Fixture) -> None:
    """FR-7.4. Every number here is read off the table, not computed."""
    all_facets = facets(library.catalog)

    assert labelled(all_facets.locations) == {"St Aldo": 7, "Bristol": 5, "No location": 3}
    assert labelled(all_facets.events) == {
        "Saturday market": 6,
        "No event": 5,
        "Harbour walk": 4,
    }
    assert labelled(all_facets.people) == {"Nobody tagged": 8, "Alice": 6, "Bob": 4}
    assert labelled(all_facets.tags) == {"No tags": 8, "market": 6, "france": 3}
    assert labelled(all_facets.ratings) == {
        "5 stars": 2,
        "4+ stars": 4,
        "3+ stars": 5,
        "2+ stars": 6,
        "1+ stars": 7,
    }
    assert labelled(all_facets.years) == {"2026": 12, "2025": 3}
    assert labelled(all_facets.raw) == {"RAW kept": 2, "JPEG only": 13}


def test_every_facet_dimension_adds_up_to_the_library(library: Fixture) -> None:
    """The single-valued ones partition it; the multi-valued ones cannot."""
    all_facets = facets(library.catalog)

    for dimension in ("locations", "events", "years", "raw"):
        assert sum(v.count for v in getattr(all_facets, dimension)) == TOTAL

    # People and tags overlap by design — a photograph holds several — so their
    # counts sum to more than the library, and that is the correct answer.
    assert sum(v.count for v in all_facets.people) > TOTAL


def test_facets_are_ranked_by_weight_so_the_big_piles_are_visible(
    library: Fixture,
) -> None:
    """FR-7.4's actual purpose: seeing where the library's weight sits."""
    locations = facets(library.catalog).locations

    assert [value.label for value in locations] == ["St Aldo", "Bristol", "No location"]
    assert [value.count for value in locations] == sorted(
        (value.count for value in locations), reverse=True
    )


def test_ratings_are_thresholds_in_descending_order_not_star_counts(
    library: Fixture,
) -> None:
    """'4+' is 4, not 2, because clicking it filters `rating >= 4`."""
    ratings = facets(library.catalog).ratings

    assert [value.value for value in ratings] == [5, 4, 3, 2, 1]
    assert [value.count for value in ratings] == [2, 4, 5, 6, 7]


def test_a_facet_value_nothing_matches_is_left_out_rather_than_shown_as_zero(
    library: Fixture,
) -> None:
    """A row that returns nothing when clicked is a dead link, not information."""
    bristol_only = Query(locations=(library.location("Bristol"),))
    events = facets(library.catalog, bristol_only).events

    assert labelled(events) == {"Harbour walk": 4, "Saturday market": 1}
    assert all(value.count > 0 for value in events)


# ----------------------------------------------------------------------
# What a facet count means
# ----------------------------------------------------------------------


def test_a_single_valued_facet_is_counted_with_its_own_dimension_lifted(
    library: Fixture,
) -> None:
    """Otherwise a location panel shows the selected place and a row of zeroes.

    With St Aldo selected, Bristol's count is what you would get by *switching*
    to Bristol — which is the only number worth putting next to it.
    """
    selected = Query(locations=(library.location("St Aldo"),))

    assert labelled(facets(library.catalog, selected).locations) == {
        "St Aldo": 7,
        "Bristol": 5,
        "No location": 3,
    }


def test_a_multi_valued_facet_is_counted_inside_the_current_filter(
    library: Fixture,
) -> None:
    """Because clicking a second person narrows rather than switches.

    Alice is on 6 photographs; with Alice already selected, Bob's row reads 3
    — the number you get by adding him — and not his library-wide 4.
    """
    with_mina = Query(people=(library.person("Alice"),))
    people = labelled(facets(library.catalog, with_mina).people)

    assert people["Alice"] == 6
    assert people["Bob"] == 3
    assert "Nobody tagged" not in people  # nobody has Alice and nobody


def test_every_facet_count_is_the_size_of_what_clicking_it_gives(
    library: Fixture,
) -> None:
    """The one rule the whole module is built on, asserted two clicks deep.

    The counts come from GROUP BY aggregates; the totals come from running the
    query. They are different code paths, and there are about two hundred pairs
    of them here that nobody had to think of individually.
    """
    checked = 0

    def check(query: Query, depth: int) -> None:
        nonlocal checked
        current = facets(library.catalog, query)
        for dimension in DIMENSIONS:
            for value in current.of(dimension):
                assert count(library.catalog, value.query) == value.count, (
                    f"{dimension} {value.label!r} at depth {depth}"
                )
                checked += 1
                if depth == 0:
                    check(value.query, depth + 1)

    check(Query(), 0)
    assert checked > 150


def test_a_filter_that_matches_nothing_is_still_shown_as_the_filter_that_is_on(
    library: Fixture,
) -> None:
    """Otherwise an empty screen does not say why it is empty.

    A zero is normally left out, because a row that returns nothing when
    clicked is a dead link. A *selected* zero is not an offer — it is the
    answer to "why am I seeing nothing", and the only thing carrying the way
    to take it off. Reachable by typing a filter into the address bar, or by
    following a saved link to a rating nothing has any more.
    """
    # Nothing without a location is rated at all, so this matches nothing —
    # and both halves of it are a selected value with no rows behind it.
    query = Query(locations=(None,), rating_min=1)
    panel = facets(library.catalog, query)

    assert count(library.catalog, query) == 0
    on = [value for value in panel.ratings if query.selects("rating", value.value)]
    assert [(value.label, value.count) for value in on] == [("1+ stars", 0)]
    # The other half: the unset location is still shown as the filter it is.
    assert [(v.label, v.count) for v in panel.locations if v.count == 0] == [
        ("No location", 0)
    ]
    # And the ordering still reads top-down, rather than putting the odd one
    # out at the bottom.
    assert [value.value for value in panel.ratings] == sorted(
        (value.value for value in panel.ratings), reverse=True
    )


def test_a_zero_that_is_selected_still_obeys_the_clicking_rule(
    library: Fixture,
) -> None:
    """The row is an exception to "leave zeroes out", not to FR-7.4a."""
    for query in (
        Query(locations=(None,), rating_min=1),
        Query(locations=(library.location("Bristol"),), rating_min=5),
        Query(people=(library.person("Bob"),), tags=(library.tag("france"),)),
        Query(has_raw=True, rating_min=5),
        Query(events=("never happened",)),
    ):
        panel = facets(library.catalog, query)
        for dimension in DIMENSIONS:
            for value in panel.of(dimension):
                assert count(library.catalog, value.query) == value.count, (
                    f"{dimension} {value.label!r} under {query}"
                )


def test_a_filter_on_a_vocabulary_row_that_no_longer_exists_says_so(
    library: Fixture,
) -> None:
    """A saved link can outlive the thing it names, and a blank chip with no
    label is worse than one that admits it does not know."""
    panel = facets(library.catalog, Query(locations=(9999,)))
    labels = [value.label for value in panel.locations if value.count == 0]

    assert labels == ["Unknown place"]


def test_clicking_a_person_twice_is_not_a_second_filter(library: Fixture) -> None:
    alice = library.person("Alice")
    once = Query().selecting("person", alice)

    assert once.selecting("person", alice) == once
    assert once.people == (alice,)


def test_clicking_a_location_replaces_rather_than_adds(library: Fixture) -> None:
    """Single-valued, so a click is a switch — which is what its count promised."""
    query = Query().selecting("location", library.location("St Aldo"))

    assert query.selecting("location", library.location("Bristol")).locations == (
        library.location("Bristol"),
    )


def test_clicking_a_year_sets_the_whole_year(library: Fixture) -> None:
    query = Query().selecting("year", 2026)

    assert query.since == dt.date(2026, 1, 1)
    assert query.until == dt.date(2026, 12, 31)
    assert count(library.catalog, query) == 12


# ----------------------------------------------------------------------
# Refusals
# ----------------------------------------------------------------------


def test_an_impossible_rating_is_refused_rather_than_returning_nothing() -> None:
    """A filter that can never match is a mistake, and silence hides it."""
    with pytest.raises(QueryError, match="rating_min must be 0-5"):
        Query(rating_min=9)


def test_a_backwards_date_range_is_refused() -> None:
    with pytest.raises(QueryError, match="can never match"):
        Query(since=dt.date(2026, 9, 7), until=dt.date(2026, 9, 6))


def test_a_negative_page_is_refused(library: Fixture) -> None:
    with pytest.raises(QueryError, match="offset"):
        search(library.catalog, Query(), offset=-1)
    with pytest.raises(QueryError, match="limit"):
        search(library.catalog, Query(), limit=-5)


def test_an_unknown_facet_dimension_is_named(library: Fixture) -> None:
    with pytest.raises(QueryError, match="colour"):
        Query().selecting("colour", 1)
    with pytest.raises(QueryError, match="colour"):
        facets(library.catalog).of("colour")


def test_nothing_here_writes_to_the_catalog(library: Fixture) -> None:
    """A read layer that left a row behind would be a surprise worth catching."""
    before = {
        table: library.catalog.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("photo", "session", "location", "person", "tag", "photo_person", "photo_tag", "photo_location")
    }

    search(library.catalog, Query(rating_min=3))
    facets(library.catalog, Query(people=(library.person("Alice"),)))

    after = {
        table: library.catalog.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in before
    }
    assert after == before


# ----------------------------------------------------------------------
# The joins to place, which are an optimisation and therefore a liability
# ----------------------------------------------------------------------


def test_the_place_joins_are_left_out_of_a_query_that_does_not_read_them(
    library: Fixture,
) -> None:
    """The optimisation, pinned — otherwise it can vanish without anything going red.

    It is worth about 40 ms a facet at 10,000 photographs (see the module
    docstring of `queries`), and nothing else in the suite would notice its
    absence, because leaving the joins in is *correct* and only slow.
    """
    assert "photo_location" not in _from(Query(rating_min=4))
    assert "photo_location" not in _from(Query(people=(1,), has_raw=True))

    # Needed when something reads them: a filter on either, or a facet that
    # selects one whether or not it filters on it.
    assert "photo_location" in _from(Query(locations=(1,)))
    assert "session" in _from(Query(events=("Saturday market",)))
    assert "photo_location" in _from(Query(rating_min=4), place=True)
    # And when the dimension is lifted for its own facet, it is not needed —
    # which is why that facet asks for it explicitly.
    assert "photo_location" not in _from(Query(locations=(1,)), without=["location"])


def test_the_place_joins_never_change_which_photographs_come_back(
    library: Fixture,
) -> None:
    """The claim that makes leaving them out safe, tested rather than asserted.

    Both are lookups on a primary key, so they cannot add, drop or duplicate a
    row. If that were ever untrue — a second override row per photograph, say —
    every count in this module would quietly depend on which branch ran.
    """
    connection = library.catalog.connection
    for query in (
        Query(),
        Query(rating_min=3),
        Query(has_raw=True),
        Query(people=(library.person("Alice"),)),
        Query(since=dt.date(2026, 1, 1)),
    ):
        where, parameters = _where(query)
        both = [
            [
                row[0]
                for row in connection.execute(
                    f"SELECT p.id {source} {where} ORDER BY p.id", parameters
                )
            ]
            for source in (_FROM_PHOTOS, _FROM_WITH_PLACE)
        ]
        assert both[0] == both[1], query


# ----------------------------------------------------------------------
# Taking a filter off, and saying which are on (WU-4.2)
# ----------------------------------------------------------------------


def test_selecting_a_value_makes_the_query_say_it_is_selected(library: Fixture) -> None:
    """The invariant that lets a panel draw a chosen row differently.

    Asserted over every dimension rather than the two that were convenient,
    because `selects` is a second reading of everything `selecting` does and
    the two are only useful while they agree.
    """
    for dimension, value in (
        ("location", library.location("St Aldo")),
        ("location", None),
        ("event", "Saturday market"),
        ("event", None),
        ("person", library.person("Alice")),
        ("tag", library.tag("market")),
        ("rating", 4),
        ("year", 2026),
        ("raw", True),
        ("raw", False),
    ):
        chosen = Query().selecting(dimension, value)
        assert chosen.selects(dimension, value), (dimension, value)
        assert not Query().selects(dimension, value), (dimension, value)


def test_every_facet_the_panel_offers_reports_itself_as_unselected_first(
    library: Fixture,
) -> None:
    """Nothing is on before anything is clicked — and then exactly one is."""
    for dimension in DIMENSIONS:
        for value in facets(library.catalog).of(dimension):
            assert not Query().selects(dimension, value.value)
            assert value.query.selects(dimension, value.value)


def test_a_year_is_only_selected_when_the_range_is_exactly_that_year(
    library: Fixture,
) -> None:
    """Found by breaking it: checking only where the range starts stayed green.

    Every query this suite built by *clicking* a year sets both ends, so a
    version that looked at one of them passed everything. A range somebody
    typed — the first half of 2026 — is where it shows: the panel would draw
    "2026" as the filter that is on, and taking it off would silently widen
    the range to the whole year.
    """
    half = Query(since=dt.date(2026, 1, 1), until=dt.date(2026, 6, 30))
    whole = Query().selecting("year", 2026)

    assert whole.selects("year", 2026)
    assert not half.selects("year", 2026)
    assert not Query(since=dt.date(2026, 1, 1)).selects("year", 2026)
    assert not Query(until=dt.date(2026, 12, 31)).selects("year", 2026)


def test_a_filter_can_always_be_taken_off_again(library: Fixture) -> None:
    """Every filter that can go on comes off, one value or the whole dimension."""
    for dimension, value in (
        ("location", library.location("St Aldo")),
        ("event", "Saturday market"),
        ("person", library.person("Alice")),
        ("tag", library.tag("france")),
        ("rating", 3),
        ("year", 2026),
        ("raw", True),
    ):
        on = Query().selecting(dimension, value)
        assert on.clearing(dimension, value).is_empty, (dimension, value)
        assert on.clearing(dimension).is_empty, (dimension, value)
        assert count(library.catalog, on.clearing(dimension)) == TOTAL


def test_taking_one_person_off_leaves_the_other_on(library: Fixture) -> None:
    """The dimension where it matters: people accumulate, so clearing has to
    be able to mean one of them rather than all of them."""
    both = Query().selecting("person", library.person("Alice")).selecting(
        "person", library.person("Bob")
    )

    assert numbers(library, both) == {1, 2, 8}
    assert numbers(library, both.clearing("person", library.person("Bob"))) == {
        1, 2, 3, 4, 7, 8
    }
    assert both.clearing("person").is_empty


def test_clearing_no_location_does_not_clear_a_location(library: Fixture) -> None:
    """``None`` is a value here, not "no value" — so the sentinel for "all of
    them" cannot be ``None`` and the two acts have to stay distinguishable."""
    mixed = Query(locations=(library.location("St Aldo"), None))

    assert mixed.clearing("location", None).locations == (library.location("St Aldo"),)
    assert mixed.clearing("location", library.location("St Aldo")).locations == (None,)
    assert mixed.clearing("location").locations == ()


def test_an_unknown_dimension_is_named_by_both_of_them(library: Fixture) -> None:
    with pytest.raises(QueryError, match="colour"):
        Query().selects("colour", 1)
    with pytest.raises(QueryError, match="colour"):
        Query().clearing("colour")


# ----------------------------------------------------------------------
# Where a page of photographs was taken
# ----------------------------------------------------------------------


def test_places_for_answers_a_whole_page_in_one_go(library: Fixture) -> None:
    """A grid of tiles wants a place per tile, and asking one at a time is
    three joins per photograph."""
    ids = [library.photo(n) for n in range(1, 16)]
    places = places_for(library.catalog, ids)

    assert places[library.photo(1)] == "St Aldo"
    assert places[library.photo(7)] == "Bristol"
    # FR-3.4: the frame that disagreed with its outing says so here too.
    assert places[library.photo(6)] == "Bristol"


def test_places_for_leaves_out_what_has_no_place(library: Fixture) -> None:
    """Absent rather than mapped to None: "no location" is drawn differently,
    and a key that is there but empty invites a caller not to."""
    places = places_for(library.catalog, [library.photo(n) for n in (1, 13, 14, 15)])

    assert set(places) == {library.photo(1)}


def test_places_for_agrees_with_the_filter_it_will_sit_next_to(
    library: Fixture,
) -> None:
    """The one that would matter. A grid whose tiles said "St Aldo" while the
    St Aldo filter did not return them would be two readings of FR-3.4 in one
    screen — which is exactly what happens if either grows its own coalesce.
    """
    for name in ("St Aldo", "Bristol"):
        by_filter = numbers(library, Query(locations=(library.location(name),)))
        everything = [library.photo(n) for n in range(1, 16)]
        places = places_for(library.catalog, everything)
        by_place = {
            n for n in range(1, 16) if places.get(library.photo(n)) == name
        }
        assert by_filter == by_place, name


def test_places_for_asked_about_nothing_answers_nothing(library: Fixture) -> None:
    """An empty page is an ordinary thing, and `IN ()` is not valid SQL."""
    assert places_for(library.catalog, []) == {}


# ----------------------------------------------------------------------
# The backlog (WU-4.4)
# ----------------------------------------------------------------------
#
# FR-7.6 needed a definition before it could have a screen, and four things
# were already filterable without any of them being the answer. "Untagged"
# here means **nothing said about it at all**: no effective location, no
# event, nobody in it, no tags. The tests below are mostly about the edges of
# that sentence, because the sentence is the unit.


def untagged(library: Fixture) -> set[int]:
    """The fixture numbers in the backlog."""
    by_id = {photo_id: number for number, photo_id in library.photos.items()}
    return {
        by_id[photo.id]
        for photo in search(library.catalog, Query(untagged=True))
        if photo.id
    }


def test_untagged_is_the_photographs_nothing_has_been_said_about(
    library: Fixture,
) -> None:
    """Against the library counted by hand at the top of this file.

    Twelve of the fifteen belong to an outing with a place on it, which is
    something said; the three from 2025-12-24 are in no session at all, so
    nothing has ever been said about them.
    """
    assert untagged(library) == {13, 14, 15}
    assert count(library.catalog, Query(untagged=False)) == TOTAL - 3


def test_untagged_is_the_four_unset_filters_asked_at_once(library: Fixture) -> None:
    """The point of having it at all.

    FR-7.2b made *no location*, *no event*, *nobody tagged* and *no tags*
    filterable one at a time, and nothing said which of them was the backlog.
    This asserts the new field is exactly their intersection — so it is a
    shorthand for a question you could already ask, not a fifth answer to
    "what does untagged mean".
    """
    every = Query(locations=(None,), events=(None,), people=(None,), tags=(None,))

    assert {p.id for p in search(library.catalog, Query(untagged=True))} == {
        p.id for p in search(library.catalog, every)
    }


def test_a_star_is_not_a_description(catalog: Catalog) -> None:
    """A frame rated during culling and never tagged is still in the backlog.

    Thirteen of the real library's photographs are exactly this. Counting a
    rating as "said something" would take the only frames anybody has looked
    at and hide them from the screen for finding the ones nobody has.
    """
    photo = catalog.photos.add(
        Photo(
            content_hash=f"{1:064x}",
            library_path="2026/2026-08-01/2026-08-01_DSCF0866.jpg",
            original_name="DSCF0866.JPG",
            captured_at=dt.datetime(2026, 8, 1, 13),
            imported_at=dt.datetime(2026, 9, 21, 12, 0),
            rating=5,
        )
    )

    assert [p.id for p in search(catalog, Query(untagged=True))] == [photo.id]


def test_one_person_is_enough_to_leave_the_backlog(catalog: Catalog) -> None:
    """Untagged means *nothing*, not *not finished*.

    A photograph with somebody in it and no place is partly done, and the
    other reading — in the backlog until every field is full — would keep
    almost everything in it forever, since most frames will never have an
    event. A list nobody can empty is a nag.
    """
    alice = catalog.people.add(Person(name="Alice"))
    assert alice.id is not None
    kept = []
    for index in (1, 2):
        photo = catalog.photos.add(
            Photo(
                content_hash=f"{index:064x}",
                library_path=f"2026/2026-09-06/f{index}.jpg",
                original_name=f"DSCF{index:04d}.JPG",
                captured_at=dt.datetime(2026, 9, 6, 10, index),
                imported_at=dt.datetime(2026, 9, 21, 12, 0),
            )
        )
        assert photo.id is not None
        kept.append(photo.id)
    catalog.photos.add_person(kept[0], alice.id)

    assert [p.id for p in search(catalog, Query(untagged=True))] == [kept[1]]


def test_an_override_counts_as_a_place_and_so_does_its_session(
    library: Fixture,
) -> None:
    """FR-3.4's precedence, read from the other end: both ways of having a
    location are ways of not being in the backlog."""
    assert 6 not in untagged(library)    # its own override
    assert 11 not in untagged(library)   # its session's


def test_the_backlog_combines_with_every_other_filter(library: Fixture) -> None:
    """It is a dimension like the rest, so "the untagged ones from 2025" is a
    question that can be asked (FR-7.3)."""
    in_2025 = Query(untagged=True, since=dt.date(2025, 1, 1), until=dt.date(2025, 12, 31))

    assert count(library.catalog, in_2025) == 3
    assert count(library.catalog, replace(in_2025, since=dt.date(2026, 1, 1),
                                          until=dt.date(2026, 12, 31))) == 0


def test_the_triage_facet_is_the_size_of_what_clicking_it_gives(
    library: Fixture,
) -> None:
    """FR-7.4a for the new dimension, stated as well as covered generically."""
    panel = facets(library.catalog, Query())

    assert labelled(panel.triage) == {"Nothing said yet": 3, "Something said": 12}
    for value in panel.triage:
        assert count(library.catalog, value.query) == value.count


def test_the_backlog_leads_the_panel(library: Fixture) -> None:
    """On the real library it is the only row with anything to say, so it is
    not something to scroll past the places nobody has typed yet."""
    assert DIMENSIONS[0] == "triage"


# ----------------------------------------------------------------------
# ... as the days the work is done in
# ----------------------------------------------------------------------


def test_the_backlog_is_a_list_of_things_to_name_newest_first(
    library: Fixture,
) -> None:
    """Hundreds of untagged photographs are a few dozen things to do, not hundreds (FR-3.3)."""
    units = backlog(library.catalog)

    assert [unit.day.isoformat() for unit in units] == ["2025-12-24"]
    assert units[0].photographs == 3
    assert units[0].untagged == 3
    assert units[0].grouped is False
    assert units[0].partly is False
    assert (units[0].started_at.hour, units[0].ended_at.hour) == (9, 11)


def test_a_day_with_nothing_left_to_do_is_not_in_the_backlog(
    library: Fixture,
) -> None:
    """The list *is* the work, so a finished day is absent rather than shown
    at zero — which is the same rule the facet panel keeps (FR-7.4a)."""
    assert {day.day.isoformat() for day in backlog(library.catalog)} == {"2025-12-24"}
    # Those are the three days that are fully tagged.
    assert {"2026-09-06", "2026-09-07", "2026-07-13"}.isdisjoint(
        day.day.isoformat() for day in backlog(library.catalog)
    )


def test_a_half_grouped_day_is_two_rows_and_the_finished_one_is_absent(
    catalog: Catalog,
) -> None:
    """What the old day-keyed list had to call "part of it is grouped and part
    is not" and hand elsewhere. Keyed on the outing, it is simply two units —
    and the one with nothing left to say drops out like any other."""
    place = catalog.locations.add(Location(name="St Aldo", slug="st-aldo"))
    assert place.id is not None
    session = catalog.sessions.add(
        Session(started_at=dt.datetime(2026, 9, 6, 9), location_id=place.id)
    )
    for index, session_id in ((1, session.id), (2, None), (3, None)):
        catalog.photos.add(
            Photo(
                content_hash=f"{index:064x}",
                library_path=f"2026/2026-09-06/f{index}.jpg",
                original_name=f"DSCF{index:04d}.JPG",
                captured_at=dt.datetime(2026, 9, 6, 10, index),
                imported_at=dt.datetime(2026, 9, 21, 12, 0),
                session_id=session_id,
            )
        )

    units = backlog(catalog)

    assert len(units) == 1
    assert (units[0].photographs, units[0].untagged) == (2, 2)
    assert units[0].grouped is False


def test_one_day_that_is_two_untagged_outings_is_two_rows(catalog: Catalog) -> None:
    """The case the refactor exists for. A day split into a morning and an
    evening has *two* answers, so a list that could only offer one form for it
    had to send the reader somewhere else."""
    morning = catalog.sessions.add(Session(started_at=dt.datetime(2026, 9, 6, 9)))
    evening = catalog.sessions.add(Session(started_at=dt.datetime(2026, 9, 6, 19)))
    for index, hour, session in ((1, 9, morning), (2, 10, morning), (3, 19, evening)):
        catalog.photos.add(
            Photo(
                content_hash=f"{index:064x}",
                library_path=f"2026/2026-09-06/f{index}.jpg",
                original_name=f"DSCF{index:04d}.JPG",
                captured_at=dt.datetime(2026, 9, 6, hour, index),
                imported_at=dt.datetime(2026, 9, 21, 12, 0),
                session_id=session.id,
            )
        )

    units = backlog(catalog)

    assert [unit.session_id for unit in units] == [evening.id, morning.id]
    assert [unit.photographs for unit in units] == [1, 2]
    assert all(unit.day.isoformat() == "2026-09-06" for unit in units)
    # Same date on both rows, so the time span is what tells them apart.
    assert units[0].started_at.hour == 19
    assert units[1].started_at.hour == 9


def test_naming_one_of_two_outings_leaves_the_other(catalog: Catalog) -> None:
    """Half a day done is half a day still on the list, which a day-keyed
    backlog could not say at all."""
    morning = catalog.sessions.add(Session(started_at=dt.datetime(2026, 9, 6, 9)))
    evening = catalog.sessions.add(Session(started_at=dt.datetime(2026, 9, 6, 19)))
    for index, hour, session in ((1, 9, morning), (2, 19, evening)):
        catalog.photos.add(
            Photo(
                content_hash=f"{index:064x}",
                library_path=f"2026/2026-09-06/f{index}.jpg",
                original_name=f"DSCF{index:04d}.JPG",
                captured_at=dt.datetime(2026, 9, 6, hour),
                imported_at=dt.datetime(2026, 9, 21, 12, 0),
                session_id=session.id,
            )
        )

    catalog.sessions.update(replace(catalog.sessions.get(evening.id), event="Pub"))

    assert [unit.session_id for unit in backlog(catalog)] == [morning.id]


def test_tagging_a_day_takes_it_out_of_the_backlog(catalog: Catalog) -> None:
    """This unit's acceptance criterion, at the layer that decides it."""
    session = catalog.sessions.add(Session(started_at=dt.datetime(2026, 9, 6, 9)))
    assert session.id is not None
    for index in (1, 2):
        catalog.photos.add(
            Photo(
                content_hash=f"{index:064x}",
                library_path=f"2026/2026-09-06/f{index}.jpg",
                original_name=f"DSCF{index:04d}.JPG",
                captured_at=dt.datetime(2026, 9, 6, 10, index),
                imported_at=dt.datetime(2026, 9, 21, 12, 0),
                session_id=session.id,
            )
        )
    assert len(backlog(catalog)) == 1

    catalog.sessions.update(replace(catalog.sessions.get(session.id), event="Beach day"))

    assert backlog(catalog) == ()
    assert count(catalog, Query(untagged=True)) == 0


def test_a_row_carries_a_few_of_its_own_frames(library: Fixture) -> None:
    """So the row says what this *was* and not only when it was. Earliest
    first, because a day reads forwards."""
    unit = backlog(library.catalog, covers=2)[0]

    assert unit.covers == (library.photo(13), library.photo(14))
    assert backlog(library.catalog, covers=0)[0].covers == ()


def test_covers_belong_to_the_outing_they_are_drawn_on(catalog: Catalog) -> None:
    """Two rows on one date must not show each other's photographs — which is
    what keying the covers by day alone would do."""
    morning = catalog.sessions.add(Session(started_at=dt.datetime(2026, 9, 6, 9)))
    evening = catalog.sessions.add(Session(started_at=dt.datetime(2026, 9, 6, 19)))
    ids = {}
    for index, hour, session in ((1, 9, morning), (2, 19, evening)):
        photo = catalog.photos.add(
            Photo(
                content_hash=f"{index:064x}",
                library_path=f"2026/2026-09-06/f{index}.jpg",
                original_name=f"DSCF{index:04d}.JPG",
                captured_at=dt.datetime(2026, 9, 6, hour),
                imported_at=dt.datetime(2026, 9, 21, 12, 0),
                session_id=session.id,
            )
        )
        ids[session.id] = photo.id

    for unit in backlog(catalog):
        assert unit.covers == (ids[unit.session_id],), unit


def test_the_backlog_asks_the_database_twice_however_many_days_there_are(
    library: Fixture,
) -> None:
    """A library with two hundred untagged days must not be two hundred round
    trips to draw one screen, so the covers are one windowed query."""
    catalog = library.catalog
    statements: list[str] = []
    catalog.connection.set_trace_callback(statements.append)
    try:
        backlog(catalog)
    finally:
        catalog.connection.set_trace_callback(None)

    assert len(statements) == 2, statements


def test_the_backlog_writes_nothing(library: Fixture) -> None:
    before = library.catalog.connection.total_changes

    backlog(library.catalog)

    assert library.catalog.connection.total_changes == before


def test_a_negative_number_of_covers_is_refused(library: Fixture) -> None:
    with pytest.raises(QueryError, match="covers"):
        backlog(library.catalog, covers=-1)


# ----------------------------------------------------------------------
# Stepping from one photograph to the next (WU-4.3)
# ----------------------------------------------------------------------
#
# The detail view's arrow keys. The rule being tested throughout is that
# stepping walks *the filter*: the set the person asked for, in the order the
# grid drew it, whether or not a grid is loaded at all.


def walk(library: Fixture, query: Query, start: int) -> list[int]:
    """Fixture numbers visited by pressing "next" from *start* until it stops."""
    by_id = {photo_id: number for number, photo_id in library.photos.items()}
    visited = [start]
    photo_id = library.photo(start)
    while True:
        photo = library.catalog.photos.get(photo_id)
        assert photo is not None
        step = neighbours(library.catalog, photo, query).next
        if step is None:
            return visited
        photo_id = step
        visited.append(by_id[photo_id])
        assert len(visited) <= TOTAL + 1, "stepping went round in a circle"


def ordered(library: Fixture, query: Query) -> list[int]:
    """The fixture numbers the grid would draw, in the order it would draw them."""
    by_id = {photo_id: number for number, photo_id in library.photos.items()}
    return [by_id[photo.id] for photo in search(library.catalog, query) if photo.id]


def test_stepping_walks_the_library_in_the_order_the_grid_drew_it(
    library: Fixture,
) -> None:
    """FR-7.1's order, one keypress at a time and with nothing skipped."""
    expected = ordered(library, Query())

    assert walk(library, Query(), expected[0]) == expected


def test_previous_and_next_are_each_other_backwards(library: Fixture) -> None:
    """Otherwise arrowing right then left would land somewhere new."""
    for number in library.photos:
        photo = library.catalog.photos.get(library.photo(number))
        assert photo is not None
        around = neighbours(library.catalog, photo)
        if around.next is not None:
            below = library.catalog.photos.get(around.next)
            assert below is not None
            assert neighbours(library.catalog, below).previous == photo.id


def test_the_two_ends_have_nowhere_further_to_go(library: Fixture) -> None:
    expected = ordered(library, Query())
    newest = library.catalog.photos.get(library.photo(expected[0]))
    oldest = library.catalog.photos.get(library.photo(expected[-1]))
    assert newest is not None and oldest is not None

    assert neighbours(library.catalog, newest).previous is None
    assert neighbours(library.catalog, oldest).next is None


def test_stepping_stays_inside_the_filter(library: Fixture) -> None:
    """The point of computing this on the server at all.

    Rated 4+ is four frames scattered through fifteen, so "the next one" is
    not the next row and is not on the same page either.
    """
    rated = Query(rating_min=4)
    expected = ordered(library, rated)

    assert expected == [7, 3, 2, 1]
    assert walk(library, rated, 7) == expected


def test_a_position_is_where_the_grid_would_have_drawn_it(library: Fixture) -> None:
    """"4 of 7" has to mean the fourth tile, or it is decoration."""
    for query in (Query(), Query(rating_min=4), Query(people=(library.person("Alice"),))):
        expected = ordered(library, query)
        for index, number in enumerate(expected, 1):
            photo = library.catalog.photos.get(library.photo(number))
            assert photo is not None
            around = neighbours(library.catalog, photo, query)
            assert around.position == index
            assert around.total == len(expected)


def test_a_frame_outside_the_filter_says_so_and_still_has_somewhere_to_go(
    library: Fixture,
) -> None:
    """The state after rating a photograph out of the view it was opened in.

    It is still on screen and it is no longer in the set, so the position is
    None rather than a number it does not occupy — and next still points into
    the filter, because the arrow keys must not become dead.
    """
    p5 = library.catalog.photos.get(library.photo(5))   # rated 2, so not in 4+
    assert p5 is not None

    around = neighbours(library.catalog, p5, Query(rating_min=4))

    assert around.position is None
    assert around.total == 4
    # p5 is 14:00 on the 6th. The nearest frame the filter *does* hold above
    # it is p7, the next morning; below it is p3 at noon the same day.
    assert around.previous == library.photo(7)
    assert around.next == library.photo(3)


def test_frames_sharing_a_timestamp_are_still_stepped_through_one_by_one(
    catalog: Catalog,
) -> None:
    """A burst is three frames in the same second (§11), which is exactly
    where an ordering on capture time alone would loop or skip."""
    same = dt.datetime(2026, 9, 6, 11, 6, 53)
    ids = []
    for number in range(1, 4):
        photo = catalog.photos.add(
            Photo(
                content_hash=f"{number:064x}",
                library_path=f"2026/2026-09-06/burst{number}.jpg",
                original_name=f"DSCF{number:04d}.JPG",
                captured_at=same,
                imported_at=dt.datetime(2026, 9, 21, 12, 0),
            )
        )
        assert photo.id is not None
        ids.append(photo.id)

    walked = [ids[-1]]
    while True:
        photo = catalog.photos.get(walked[-1])
        assert photo is not None
        step = neighbours(catalog, photo).next
        if step is None:
            break
        walked.append(step)
        assert len(walked) <= 3, "stepping revisited a frame"

    assert walked == list(reversed(ids))
    assert [p.id for p in search(catalog, Query())] == walked


def test_stepping_asks_nothing_of_the_disk_and_writes_nothing(
    library: Fixture,
) -> None:
    """It is a read, like everything else in this module."""
    before = library.catalog.connection.total_changes
    photo = library.catalog.photos.get(library.photo(5))
    assert photo is not None

    neighbours(library.catalog, photo)

    assert library.catalog.connection.total_changes == before


# ----------------------------------------------------------------------
# NFR-1 — 10,000 photographs, under 200 ms
# ----------------------------------------------------------------------

#: What NFR-1 asks of a dashboard filter.
BUDGET_MS = 200.0

#: The size NFR-1 names. A real library may well be smaller, so this is synthetic on
#: purpose: the target is about the library this will be in two years.
SEEDED = 10_000


@pytest.fixture(scope="session")
def large_catalog(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Catalog]:
    """10,000 synthetic photographs, seeded once for the whole suite.

    Shaped like a real library rather than uniformly: a few places used a lot
    and a long tail used once, most frames unrated, RAW on a minority. A flat
    distribution would make every facet the same size and would flatter any
    query plan that depends on selectivity.
    """
    root = tmp_path_factory.mktemp("large") / "library"
    root.mkdir()
    settings = Settings(library_root=root, state_dir=root.parent / "state")
    catalog = Catalog.open(settings)
    _seed(catalog, SEEDED)
    yield catalog
    catalog.close()


def _seed(catalog: Catalog, photographs: int) -> None:
    """Insert directly. The repositories are tested elsewhere; this is volume."""
    connection = catalog.connection
    locations = 40
    people = 12
    tags = 20
    days = 900  # roughly two and a half years, ~11 frames a day

    with catalog.transaction():
        connection.executemany(
            "INSERT INTO location (name, slug, use_count) VALUES (?, ?, ?)",
            [(f"Place {i}", f"place-{i}", 0) for i in range(1, locations + 1)],
        )
        connection.executemany(
            "INSERT INTO person (name) VALUES (?)",
            [(f"Person {i}",) for i in range(1, people + 1)],
        )
        connection.executemany(
            "INSERT INTO tag (name) VALUES (?)",
            [(f"tag-{i}",) for i in range(1, tags + 1)],
        )

        start = dt.date(2024, 1, 1)
        session_rows = []
        for day_index in range(days):
            day = start + dt.timedelta(days=day_index)
            # A Zipf-ish skew: low ids are used far more often than high ones.
            location_id = 1 + (day_index * day_index) % locations
            session_rows.append(
                (
                    f"{day}T08:00:00",
                    f"{day}T20:00:00",
                    location_id,
                    f"Outing {day_index % 60}" if day_index % 4 else None,
                )
            )
        connection.executemany(
            "INSERT INTO session (started_at, ended_at, location_id, event) "
            "VALUES (?, ?, ?, ?)",
            session_rows,
        )

        photo_rows = []
        for i in range(photographs):
            day = start + dt.timedelta(days=i % days)
            minute = (i * 7) % (60 * 11)
            captured = dt.datetime.combine(day, dt.time(8, 0)) + dt.timedelta(minutes=minute)
            has_raw = i % 9 == 0
            name = f"DSCF{i % 10000:04d}"
            photo_rows.append(
                (
                    f"{i:064x}",
                    f"{day:%Y/%Y-%m-%d}/{day}_{name}_{i}.jpg",
                    f"{name}.JPG",
                    int(has_raw),
                    f"{day:%Y/%Y-%m-%d}/{day}_{name}_{i}.raf" if has_raw else None,
                    captured.isoformat(),
                    "2026-09-21T12:00:00",
                    # Most photographs are unrated; a few are worth something.
                    0 if i % 5 else 1 + (i // 5) % 5,
                    1 + (i % days),
                )
            )
        connection.executemany(
            "INSERT INTO photo (content_hash, library_path, original_name, has_raw, "
            "raw_path, captured_at, imported_at, rating, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            photo_rows,
        )

        connection.executemany(
            "INSERT OR IGNORE INTO photo_person (photo_id, person_id) VALUES (?, ?)",
            [
                (photo_id, 1 + (photo_id * offset) % people)
                for photo_id in range(1, photographs + 1)
                for offset in (1, 3)
                if photo_id % 3
            ],
        )
        connection.executemany(
            "INSERT OR IGNORE INTO photo_tag (photo_id, tag_id) VALUES (?, ?)",
            [
                (photo_id, 1 + (photo_id * offset) % tags)
                for photo_id in range(1, photographs + 1)
                for offset in (1, 5)
            ],
        )
        # FR-3.4 overrides, on one frame in fifty.
        connection.executemany(
            "INSERT INTO photo_location (photo_id, location_id) VALUES (?, ?)",
            [
                (photo_id, 1 + photo_id % locations)
                for photo_id in range(1, photographs + 1, 50)
            ],
        )
    connection.execute("ANALYZE")
    connection.commit()


def fastest(work, repeats: int = 5) -> float:  # type: ignore[no-untyped-def]
    """Milliseconds for the quickest of *repeats* runs.

    EXECUTION_PLAN.md §8 records two wrong conclusions on this machine drawn
    from single readings taken under different load. The minimum is the one
    figure that cannot be inflated by something else running.
    """
    best = float("inf")
    for _ in range(repeats):
        started = time.perf_counter()
        work()
        best = min(best, (time.perf_counter() - started) * 1000)
    return best


def report(line: str, unit: str = "WU-4.1") -> None:
    """Print a measurement. Visible with ``pytest -s``, and quiet otherwise.

    The numbers belong in EXECUTION_PLAN.md §7, which is where they are read
    from; this is how they are got out of a run without an assertion having to
    fail to show one. The unit is on the line because this section has outlived
    the one that started it.
    """
    print(f"  [{unit}] {line}")


def test_the_seeded_library_is_the_size_nfr_1_talks_about(large_catalog: Catalog) -> None:
    assert large_catalog.photos.count() == SEEDED
    # Shaped, not uniform: the facets have to be worth counting.
    assert 1 < len(facets(large_catalog).locations) <= 41
    assert count(large_catalog, Query(has_raw=True)) > 0
    assert count(large_catalog, Query(rating_min=4)) > 0


def test_a_combined_filter_over_ten_thousand_photographs_meets_nfr_1(
    large_catalog: Catalog,
) -> None:
    """Success criterion 3's 'instantly', against a number.

    The measured query is the hard one on purpose: a place (through FR-3.4's
    coalesce), a person, a rating threshold and a year, all at once, with the
    full facet panel recomputed alongside it — which is what a filter click
    actually costs.
    """
    query = Query(
        locations=(1,),
        people=(1,),
        rating_min=3,
        since=dt.date(2025, 1, 1),
        until=dt.date(2025, 12, 31),
    )

    page = fastest(lambda: search(large_catalog, query, limit=200))
    panel = fastest(lambda: facets(large_catalog, query))
    whole = fastest(lambda: (search(large_catalog, query, limit=200), facets(large_catalog, query)))

    report(f"filtered: search {page:.1f} ms, facets {panel:.1f} ms, together {whole:.1f} ms")
    assert whole < BUDGET_MS, (
        f"{whole:.1f} ms for a filtered page plus facets at {SEEDED} photographs, "
        f"against NFR-1's {BUDGET_MS:.0f} ms (search {page:.1f} ms, facets {panel:.1f} ms)"
    )


def test_the_unfiltered_library_is_the_slow_case_and_still_meets_nfr_1(
    large_catalog: Catalog,
) -> None:
    """No filter means every facet counts all 10,000 rows — the worst panel there is."""
    whole = fastest(lambda: (search(large_catalog, Query(), limit=200), facets(large_catalog, Query())))

    report(f"unfiltered: search plus facets {whole:.1f} ms")
    assert whole < BUDGET_MS, f"{whole:.1f} ms unfiltered at {SEEDED} photographs"


def test_stepping_to_the_next_photograph_is_cheap_at_ten_thousand(
    large_catalog: Catalog,
) -> None:
    """The detail view's arrow key, against the same budget as a filter click.

    It is three small queries rather than a page of rows — two of them stop at
    the first match — so the interesting case is the *unfiltered* one, where
    they scan the widest. Measured here because "navigation between photos is
    fast" is WU-4.3's acceptance criterion, and a criterion that is not
    measured is an opinion.
    """
    middle = search(large_catalog, Query(), limit=1, offset=SEEDED // 2).photos[0]
    filtered = Query(rating_min=4)

    whole = fastest(lambda: neighbours(large_catalog, middle))
    narrowed = fastest(lambda: neighbours(large_catalog, middle, filtered))

    report(f"stepping: whole library {whole:.1f} ms, rated 4+ {narrowed:.1f} ms",
           unit="WU-4.3")
    assert whole < BUDGET_MS, (
        f"{whole:.1f} ms to find what is either side of one photograph at "
        f"{SEEDED}, against NFR-1's {BUDGET_MS:.0f} ms"
    )
    assert narrowed < BUDGET_MS


@pytest.fixture(scope="session")
def untagged_catalog(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Catalog]:
    """10,000 photographs with nothing said about any of them.

    The shape of the real library scaled up, and the worst case for FR-7.6's
    screen: every row is in the backlog, so both of its queries touch
    everything and there is a group for every day. The other seeded catalog is
    the opposite — every photograph has a session with a place on it — and
    measuring a backlog against that would be measuring an empty answer.
    """
    root = tmp_path_factory.mktemp("untagged") / "library"
    root.mkdir()
    settings = Settings(library_root=root, state_dir=root.parent / "state")
    catalog = Catalog.open(settings)
    start = dt.date(2024, 1, 1)
    with catalog.transaction():
        catalog.connection.executemany(
            "INSERT INTO photo (content_hash, library_path, original_name, "
            "captured_at, imported_at) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    f"{i:064x}",
                    f"2026/2026-01-01/DSCF{i:05d}.jpg",
                    f"DSCF{i:05d}.JPG",
                    (
                        dt.datetime.combine(start + dt.timedelta(days=i % 900),
                                            dt.time(8, 0))
                        + dt.timedelta(minutes=(i * 7) % 600)
                    ).isoformat(),
                    "2026-09-21T12:00:00",
                )
                for i in range(SEEDED)
            ],
        )
    catalog.connection.execute("ANALYZE")
    yield catalog
    catalog.close()


def test_the_backlog_is_cheap_when_everything_is_in_it(
    untagged_catalog: Catalog,
) -> None:
    """FR-7.6's screen, against the same budget as a filter click.

    Two grouped scans and no row objects, which is the point: a backlog is a
    screen full of counts, and building ten thousand photographs to show nine
    hundred numbers is the 418 ms one test down.
    """
    whole = fastest(lambda: backlog(untagged_catalog))
    days = backlog(untagged_catalog)
    empty = fastest(lambda: backlog(untagged_catalog, covers=0))

    assert len(days) == 900   # nothing is grouped, so one unit per day
    assert sum(day.untagged for day in days) == SEEDED
    report(f"backlog: {len(days)} days in {whole:.1f} ms "
           f"({empty:.1f} ms without the covers)", unit="WU-4.4")
    assert whole < BUDGET_MS, (
        f"{whole:.1f} ms to gather a backlog of {SEEDED} photographs, "
        f"against NFR-1's {BUDGET_MS:.0f} ms"
    )


def test_the_whole_library_being_untagged_is_still_one_question(
    untagged_catalog: Catalog,
) -> None:
    """The filter behind the screen, measured on the library it is for."""
    whole = fastest(lambda: count(untagged_catalog, Query(untagged=True)))
    panel = fastest(lambda: facets(untagged_catalog, Query(untagged=True)))

    report(f"untagged: count {whole:.1f} ms, panel {panel:.1f} ms", unit="WU-4.4")
    assert count(untagged_catalog, Query(untagged=True)) == SEEDED
    assert panel < BUDGET_MS


def test_a_full_unpaged_result_is_reported_rather_than_assumed_fast(
    large_catalog: Catalog,
) -> None:
    """Building 10,000 Photo objects is not a filter, and is not what NFR-1 times.

    Recorded rather than asserted: the number belongs in the session log, and
    the answer if it ever matters is the ``limit`` the grid already has.
    """
    elapsed = fastest(lambda: search(large_catalog, Query()), repeats=3)

    report(f"unpaged: {SEEDED} Photo objects built in {elapsed:.1f} ms")
    assert len(search(large_catalog, Query()).photos) == SEEDED


# ----------------------------------------------------------------------
# The real library
# ----------------------------------------------------------------------


@pytest.fixture
def real_library(machine_settings: Settings) -> Settings:
    """The configured library, or a skip. Never written to."""
    settings = machine_settings
    if not settings.library_root.is_dir():
        pytest.skip(f"library root absent: {settings.library_root}")
    return settings


@pytest.mark.needs_library
def test_the_real_catalog_facets_agree_with_its_own_rows(real_library: Settings) -> None:
    """Facet counts against known data — the known data being the owner's catalog.

    It reads the real catalog and writes nothing. The numbers are not hardcoded
    because the library grows every import; what is asserted is the invariant
    that would break if the coalesce, the unset buckets or the year grouping
    were wrong — every single-valued dimension partitions the library exactly.
    """
    with Catalog.open(real_library) as catalog:
        total = catalog.photos.count()
        if total == 0:
            pytest.skip("the real catalog is empty")
        panel = facets(catalog)

        assert count(catalog) == total
        for dimension in ("locations", "events", "years", "raw"):
            values = getattr(panel, dimension)
            assert sum(value.count for value in values) == total, dimension

        for dimension in DIMENSIONS:
            for value in panel.of(dimension):
                assert count(catalog, value.query) == value.count, (
                    f"{dimension} {value.label!r}"
                )


@pytest.mark.needs_library
def test_the_real_library_is_still_the_triage_backlog_it_was_yesterday(
    real_library: Settings,
) -> None:
    """FR-7.6 seen from here: every photograph is untagged, and the facets say so.

    This is the single most useful thing the facets currently report about the
    real library, and it is a number that should change the moment the owner tags a
    day — at which point this test says so, rather than going quietly stale.
    """
    with Catalog.open(real_library) as catalog:
        total = catalog.photos.count()
        if total == 0:
            pytest.skip("the real catalog is empty")
        untagged = count(catalog, Query(locations=(None,)))
        located = total - untagged

    assert untagged + located == total
    assert untagged > 0, (
        "no photograph in the real library is without a location any more — "
        "update this test and EXECUTION_PLAN.md §1, which still says 0 locations"
    )


# ----------------------------------------------------------------------
# Browsing by month
# ----------------------------------------------------------------------


def test_the_months_are_every_month_with_photographs_newest_first(library: Fixture) -> None:
    months = facets(library.catalog).of("month")

    assert [(m.value, m.label, m.count) for m in months] == [
        ("2026-09", "September 2026", 10),
        ("2026-07", "July 2026", 2),
        ("2025-12", "December 2025", 3),
    ]


def test_a_month_is_its_first_to_its_last_day(library: Fixture) -> None:
    september = Query().selecting("month", "2026-09")
    december = Query().selecting("month", "2025-12")

    assert (september.since, september.until) == (dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert (december.since, december.until) == (dt.date(2025, 12, 1), dt.date(2025, 12, 31))
    assert count(library.catalog, september) == 10
    assert september.selects("month", "2026-09")
    assert not september.selects("year", 2026)
    # Taking it off takes the date range off, whichever row it is taken from.
    assert september.clearing("month") == Query()
    assert Query().selecting("year", 2026).clearing("month") == Query()


def test_a_month_that_is_on_is_drawn_on(library: Fixture) -> None:
    """Under its year, the chosen month is the row with the cross beside it."""
    september = Query().selecting("month", "2026-09")
    [selected] = [m for m in facets(library.catalog, september).of("month")
                  if september.selects("month", m.value)]

    assert selected.value == "2026-09"
    # Months are counted with the range lifted, so the other months still show.
    assert len(facets(library.catalog, september).of("month")) == 3


def test_a_month_that_is_not_one_is_refused() -> None:
    with pytest.raises(QueryError):
        Query().selecting("month", "2026-13")
    with pytest.raises(QueryError):
        Query().selecting("month", "September")
