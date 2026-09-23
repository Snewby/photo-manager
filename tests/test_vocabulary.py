"""Locations, people and tags, and the cost of typing one (WU-3.2).

The camera records no GPS and there is no face recognition, so every word in
this library arrives through a keyboard. That makes four things worth testing
hard.

**Autocomplete has to rank the way a person reaches for a word.** Not
alphabetically, and not purely by use either: typing "st" and being offered
Bristol first because Bristol is used more would be a ranking fighting the
person using it. So there are tests for the tiers, for ``use_count`` deciding
within a tier, and for the count going up and *down* with the tagging that
justifies it.

**A second row for the same place is the failure.** The whole point of
autocomplete is to stop one place being typed two ways, so "st aldo" must find
"St Aldo" and "malaga" must find "Málaga".

**A slug is only ever a filename**, and ``location.slug`` is UNIQUE, so two
names that want one slug must both get a usable one rather than the second
blowing up in front of somebody mid-tagging.

**The override is FR-3.4 and it is precedence, not replacement.** Setting one,
clearing one, and both ends being empty are separate answers and are tested as
such.

The last section takes the count apart on purpose: long runs of tagging and
untagging, then :func:`recount_uses` to prove nothing drifted.
"""

from __future__ import annotations

import datetime as dt

import pytest

from photo_manager.catalog import Catalog
from photo_manager.models import Location, Photo, Session
from photo_manager.vocabulary import (
    FALLBACK_SLUG,
    MAX_SLUG_LENGTH,
    VocabularyError,
    add_person_to_photo,
    add_tag_to_photo,
    clear_photo_location,
    clear_session_location,
    fold,
    normalise_name,
    recount_uses,
    remove_person_from_photo,
    remove_tag_from_photo,
    resolve_location,
    resolve_person,
    resolve_tag,
    set_photo_location,
    set_session_location,
    slug_for,
    slugify,
    suggest_locations,
    suggest_people,
    suggest_tags,
)

CAPTURED = dt.datetime(2026, 9, 6, 10, 6, 45)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def stored(catalog: Catalog, *, captured_at: dt.datetime = CAPTURED) -> Photo:
    index = catalog.photos.count() + 1
    return catalog.photos.add(
        Photo(
            content_hash=f"{index:064x}",
            library_path=f"2026/2026-09-06/DSCF{index:04d}.jpg",
            original_name=f"DSCF{index:04d}.JPG",
            captured_at=captured_at,
            imported_at=dt.datetime(2026, 9, 21, 12, 0),
        )
    )


def in_session(catalog: Catalog, *photos: Photo) -> Session:
    session = catalog.sessions.add(Session(started_at=CAPTURED, ended_at=CAPTURED))
    assert session.id is not None
    catalog.sessions.assign(session.id, [p.id for p in photos])
    return session


def names(rows) -> list[str]:  # type: ignore[no-untyped-def]
    return [row.name for row in rows]


# ----------------------------------------------------------------------
# Folding and normalising
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("St Aldo", "st aldo"),
        ("ST ALDO", "st aldo"),
        ("  St   Aldo  ", "st aldo"),
        ("Málaga", "malaga"),
        ("ZÜRICH", "zurich"),
        ("Ceará", "ceara"),
    ],
)
def test_folding_removes_case_accents_and_extra_space(text: str, expected: str) -> None:
    assert fold(text) == expected


def test_a_stored_name_keeps_its_case_and_punctuation() -> None:
    """Only the slug flattens those. "St. Aldo" is how somebody chose to write
    it, and correcting it is not this layer's business."""
    assert normalise_name("  St.   Aldo  ") == "St. Aldo"
    assert normalise_name("O'Neill's") == "O'Neill's"


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_an_empty_name_is_refused(bad: str) -> None:
    with pytest.raises(VocabularyError, match="cannot be empty"):
        normalise_name(bad)


def test_a_name_that_is_not_text_is_refused() -> None:
    with pytest.raises(VocabularyError, match="must be text"):
        normalise_name(None)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Slugs (D-7)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("St Aldo", "st-aldo"),                      # spaces
        ("ST ALDO", "st-aldo"),                      # case
        ("St. Aldo", "st-aldo"),                     # punctuation
        ("  The   Old   Anchor  ", "the-old-anchor"),
        ("Málaga", "malaga"),                        # accents
        ("Stoke-on-Trent", "stoke-on-trent"),        # hyphens survive
        ("Cafe & Bar", "cafe-bar"),                  # a run becomes one hyphen
        ("Queen's Park", "queens-park"),             # apostrophes are dropped
        ("Ynys Môn 2", "ynys-mon-2"),                # digits survive
        ("---St Aldo---", "st-aldo"),                # no leading or trailing
    ],
)
def test_slugify_handles_spaces_case_and_punctuation(name: str, expected: str) -> None:
    assert slugify(name) == expected


def test_a_slug_is_capped_at_a_length_a_filename_can_carry() -> None:
    """The name may be as long as its owner likes; `YYYY-MM-DD_<slug>_DSCF1234.jpg`
    may not.

    The name here is chosen so the cap lands *inside* a word — the 41st
    character of its slug is the "i" of "conservation" — because a name whose
    41st character happens to be a hyphen would pass whether the cut respects
    word boundaries or not. That is exactly what the first version of this test
    did, and it was found by breaking the boundary logic on purpose and
    watching the test stay green.
    """
    slug = slugify("Kingston upon Thames Riverside Conservation Area")

    assert len(slug) <= MAX_SLUG_LENGTH
    assert slug == "kingston-upon-thames-riverside"
    assert "conservat" not in slug, "cut mid-word instead of at a hyphen"


def test_a_long_unbroken_name_is_cut_rather_than_thrown_away() -> None:
    """There is no hyphen to cut at, and a truncated slug beats no slug."""
    slug = slugify("Llanfairpwllgwyngyllgogerychwyrndrobwllllantysiliogogogoch")

    assert len(slug) == MAX_SLUG_LENGTH
    assert slug.startswith("llanfairpwllgwyngyll")


@pytest.mark.parametrize("name", ["!!!", "日本", "…", "-"])
def test_a_name_with_nothing_sluggable_gives_an_empty_slug(name: str) -> None:
    """slugify says 'nothing here'; only slug_for knows what to do about it,
    because only it knows what is already taken."""
    assert slugify(name) == ""


def test_a_new_slug_is_used_as_is_when_it_is_free(catalog: Catalog) -> None:
    assert slug_for(catalog, "St Aldo") == "st-aldo"


def test_two_names_wanting_one_slug_both_get_one(catalog: Catalog) -> None:
    """§5.1 makes the slug UNIQUE so one filename cannot mean two places. "St
    Aldo" and "St. Aldo" may well be two different places, so the answer is a
    suffix rather than a refusal or a silent merge."""
    first = resolve_location(catalog, "St Aldo")
    second = resolve_location(catalog, "St. Aldo")

    assert first.slug == "st-aldo"
    assert second.slug == "st-aldo-2"
    assert first.id != second.id


def test_an_unsluggable_name_falls_back_and_still_de_duplicates(
    catalog: Catalog,
) -> None:
    """Refusing the name would be the app dictating what a place may be called.
    Only the filename degrades — the name is stored and shown in full."""
    first = resolve_location(catalog, "日本")
    second = resolve_location(catalog, "!!!")

    assert first.slug == FALLBACK_SLUG
    assert second.slug == f"{FALLBACK_SLUG}-2"
    assert first.name == "日本"


def test_a_location_can_keep_its_own_slug_while_being_renamed(
    catalog: Catalog,
) -> None:
    place = resolve_location(catalog, "St Aldo")

    assert slug_for(catalog, "St Aldo", exclude_id=place.id) == "st-aldo"
    assert slug_for(catalog, "St Aldo") == "st-aldo-2"


# ----------------------------------------------------------------------
# Created on first use (FR-5.1)
# ----------------------------------------------------------------------


def test_a_location_is_created_the_first_time_it_is_typed(catalog: Catalog) -> None:
    """FR-5.1: no management screen to visit first."""
    assert catalog.locations.all() == []

    place = resolve_location(catalog, "St Aldo")

    assert place.id is not None
    assert catalog.locations.get(place.id).name == "St Aldo"


def test_typing_it_again_finds_it_rather_than_making_a_second(
    catalog: Catalog,
) -> None:
    first = resolve_location(catalog, "St Aldo")
    second = resolve_location(catalog, "St Aldo")

    assert first.id == second.id
    assert len(catalog.locations.all()) == 1


@pytest.mark.parametrize("typed", ["st aldo", "ST ALDO", "  St    Aldo "])
def test_case_and_spacing_find_the_existing_one(catalog: Catalog, typed: str) -> None:
    """A second row for the same place is exactly the failure autocomplete
    exists to prevent, so it must not be one keystroke away."""
    original = resolve_location(catalog, "St Aldo")

    assert resolve_location(catalog, typed).id == original.id
    assert len(catalog.locations.all()) == 1


def test_accents_find_the_existing_one(catalog: Catalog) -> None:
    original = resolve_location(catalog, "Málaga")

    assert resolve_location(catalog, "malaga").id == original.id


def test_people_and_tags_are_created_on_first_use_too(catalog: Catalog) -> None:
    person = resolve_person(catalog, "Alice")
    tag = resolve_tag(catalog, "Beach")

    assert resolve_person(catalog, "alice").id == person.id
    assert resolve_tag(catalog, "BEACH").id == tag.id
    assert len(catalog.people.all()) == 1
    assert len(catalog.tags.all()) == 1


def test_resolving_is_not_using(catalog: Catalog) -> None:
    """A name typed into a box and then abandoned is not a use of it. Only
    applying one counts, or the ranking measures hesitation."""
    place = resolve_location(catalog, "St Aldo")
    resolve_location(catalog, "St Aldo")
    resolve_location(catalog, "St Aldo")

    assert catalog.locations.get(place.id).use_count == 0


def test_an_empty_name_is_refused_before_a_row_is_made(catalog: Catalog) -> None:
    with pytest.raises(VocabularyError):
        resolve_location(catalog, "   ")

    assert catalog.locations.all() == []


# ----------------------------------------------------------------------
# Autocomplete (FR-5.1, FR-5.2, FR-5.4)
# ----------------------------------------------------------------------


def vocabulary(catalog: Catalog) -> None:
    """Four places with deliberately different use counts and shapes."""
    for name, uses in [
        ("Bristol", 9),
        ("St Aldo", 5),
        ("Stoke-on-Trent", 2),
        ("Rinaldo Cafe", 7),
    ]:
        place = resolve_location(catalog, name)
        if uses:
            catalog.locations.increment_use(place.id, uses)


def test_an_empty_query_offers_the_most_used_first(catalog: Catalog) -> None:
    """An empty box should already be showing the places you go, so the first
    keystroke is often the last one."""
    vocabulary(catalog)

    assert names(suggest_locations(catalog)) == [
        "Bristol", "Rinaldo Cafe", "St Aldo", "Stoke-on-Trent"
    ]


def test_how_well_it_matches_beats_how_often_it_is_used(catalog: Catalog) -> None:
    """Typing "st" and being offered Bristol first because Bristol is used more
    would be a ranking that fights the person using it."""
    vocabulary(catalog)

    assert names(suggest_locations(catalog, "st")) == [
        "St Aldo",         # the name starts with it
        "Stoke-on-Trent",  # so does this, and it is used less
        "Bristol",         # only contains it
    ]


def test_a_word_inside_the_name_outranks_a_match_mid_word(catalog: Catalog) -> None:
    vocabulary(catalog)

    assert names(suggest_locations(catalog, "aldo")) == ["St Aldo", "Rinaldo Cafe"]


def test_within_a_tier_use_count_decides(catalog: Catalog) -> None:
    """Which is the half of the ranking FR-5.1 actually names."""
    for name, uses in [("Padstow", 1), ("Penzance", 8), ("Perranporth", 4)]:
        catalog.locations.increment_use(resolve_location(catalog, name).id, uses)

    assert names(suggest_locations(catalog, "p")) == [
        "Penzance", "Perranporth", "Padstow"
    ]


def test_ties_are_broken_by_the_folded_name_not_the_raw_one(
    catalog: Catalog,
) -> None:
    """Equally-used names come back in the order a person reads them in, which
    is not the order their bytes sort in: SQLite's default collation puts
    "Álava" after "Zennor", because its first byte is 0xC3. An accent is not a
    reason to be last in a list.

    This also pins that the ranking does not depend on the order rows arrive
    in — the repository already sorts, and a ranking that quietly relied on
    that would break the day the query changed.
    """
    for name in ["Zennor", "Marazion", "Álava"]:
        resolve_location(catalog, name)

    assert names(catalog.locations.all()) == ["Marazion", "Zennor", "Álava"]
    assert names(suggest_locations(catalog)) == ["Álava", "Marazion", "Zennor"]


def test_matching_ignores_case_and_accents(catalog: Catalog) -> None:
    catalog.locations.increment_use(resolve_location(catalog, "Málaga").id, 3)

    assert names(suggest_locations(catalog, "MAL")) == ["Málaga"]
    assert names(suggest_locations(catalog, "málag")) == ["Málaga"]


def test_nothing_matching_is_an_empty_list_not_an_error(catalog: Catalog) -> None:
    vocabulary(catalog)

    assert suggest_locations(catalog, "qqq") == []


def test_the_limit_is_honoured_and_takes_the_best(catalog: Catalog) -> None:
    vocabulary(catalog)

    assert names(suggest_locations(catalog, limit=2)) == ["Bristol", "Rinaldo Cafe"]
    assert names(suggest_locations(catalog, "st", limit=1)) == ["St Aldo"]


def test_people_and_tags_rank_the_same_way(catalog: Catalog) -> None:
    """FR-5.2 and FR-5.4 say "same autocomplete behaviour", so it is the same
    ranking rather than a second one that will drift."""
    catalog.people.increment_use(resolve_person(catalog, "Alice").id, 3)
    resolve_person(catalog, "Alan")
    catalog.tags.increment_use(resolve_tag(catalog, "beach").id, 4)
    resolve_tag(catalog, "birthday")

    assert names(suggest_people(catalog, "al")) == ["Alice", "Alan"]
    assert names(suggest_tags(catalog, "b")) == ["beach", "birthday"]


# ----------------------------------------------------------------------
# use_count follows the tagging (FR-5.1)
# ----------------------------------------------------------------------


def test_tagging_a_session_counts_as_one_use(catalog: Catalog) -> None:
    """One reach for the word, whether it lands on one frame or fifty-four.
    Counting photographs would let one big day outrank five separate trips."""
    session = in_session(catalog, stored(catalog), stored(catalog), stored(catalog))

    place, affected = set_session_location(catalog, session.id, "St Aldo")

    assert catalog.locations.get(place.id).use_count == 1
    assert len(affected) == 3


def test_setting_the_same_location_again_does_not_count_twice(
    catalog: Catalog,
) -> None:
    """Clicking the same suggestion twice is not two visits."""
    session = in_session(catalog, stored(catalog))

    place, _ = set_session_location(catalog, session.id, "St Aldo")
    set_session_location(catalog, session.id, "St Aldo")
    set_session_location(catalog, session.id, "st aldo")

    assert catalog.locations.get(place.id).use_count == 1


def test_changing_a_location_moves_the_use(catalog: Catalog) -> None:
    """Leaving the credit on the old one would rank a place by trips that were
    taken back."""
    session = in_session(catalog, stored(catalog))
    first, _ = set_session_location(catalog, session.id, "St Aldo")

    second, _ = set_session_location(catalog, session.id, "Penzance")

    assert catalog.locations.get(first.id).use_count == 0
    assert catalog.locations.get(second.id).use_count == 1
    assert catalog.sessions.get(session.id).location_id == second.id


def test_clearing_a_session_location_takes_the_use_back(catalog: Catalog) -> None:
    session = in_session(catalog, stored(catalog))
    place, _ = set_session_location(catalog, session.id, "St Aldo")

    clear_session_location(catalog, session.id)

    assert catalog.locations.get(place.id).use_count == 0
    assert catalog.sessions.get(session.id).location_id is None


def test_adding_a_person_counts_and_removing_them_takes_it_back(
    catalog: Catalog,
) -> None:
    photo = stored(catalog)

    person, affected = add_person_to_photo(catalog, photo.id, "Alice")
    assert catalog.people.get(person.id).use_count == 1
    assert affected == [photo.id]

    remove_person_from_photo(catalog, photo.id, person.id)
    assert catalog.people.get(person.id).use_count == 0
    assert catalog.photos.people(photo.id) == []


def test_adding_the_same_person_twice_counts_once(catalog: Catalog) -> None:
    photo = stored(catalog)

    person, _ = add_person_to_photo(catalog, photo.id, "Alice")
    add_person_to_photo(catalog, photo.id, "alice")

    assert catalog.people.get(person.id).use_count == 1
    assert len(catalog.photos.people(photo.id)) == 1


def test_the_same_person_on_two_photographs_counts_twice(catalog: Catalog) -> None:
    """Two separate acts of tagging, unlike one session applied to fifty."""
    first, second = stored(catalog), stored(catalog)

    person, _ = add_person_to_photo(catalog, first.id, "Alice")
    add_person_to_photo(catalog, second.id, "Alice")

    assert catalog.people.get(person.id).use_count == 2


def test_tags_count_the_same_way(catalog: Catalog) -> None:
    photo = stored(catalog)

    tag, _ = add_tag_to_photo(catalog, photo.id, "beach")
    add_tag_to_photo(catalog, photo.id, "beach")
    assert catalog.tags.get(tag.id).use_count == 1

    remove_tag_from_photo(catalog, photo.id, tag.id)
    assert catalog.tags.get(tag.id).use_count == 0


def test_removing_something_that_is_not_there_changes_nothing(
    catalog: Catalog,
) -> None:
    photo = stored(catalog)
    person = resolve_person(catalog, "Alice")

    remove_person_from_photo(catalog, photo.id, person.id)

    assert catalog.people.get(person.id).use_count == 0


def test_a_count_never_goes_below_zero(catalog: Catalog) -> None:
    """The column is CHECKed non-negative (§5.1), so a floor here is the
    difference between an undo and a constraint failure mid-tagging."""
    photo = stored(catalog)
    person, _ = add_person_to_photo(catalog, photo.id, "Alice")
    catalog.people.increment_use(person.id, -1)  # something else already undid it

    remove_person_from_photo(catalog, photo.id, person.id)

    assert catalog.people.get(person.id).use_count == 0


def test_tagging_something_that_does_not_exist_is_refused(catalog: Catalog) -> None:
    with pytest.raises(VocabularyError, match="No photograph with id 999"):
        add_person_to_photo(catalog, 999, "Alice")
    with pytest.raises(VocabularyError, match="No session with id 999"):
        set_session_location(catalog, 999, "St Aldo")
    assert catalog.people.all() == []
    assert catalog.locations.all() == []


# ----------------------------------------------------------------------
# The override, and its precedence (FR-3.4)
# ----------------------------------------------------------------------


def test_with_no_session_and_no_override_there_is_no_location(
    catalog: Catalog,
) -> None:
    photo = stored(catalog)

    assert catalog.photos.location(photo.id) is None


def test_a_photograph_inherits_its_sessions_location(catalog: Catalog) -> None:
    """FR-3.3's whole point: tag the outing once, not fifty-four times. The
    photograph's own row is untouched — inheritance, not a copy."""
    photo = stored(catalog)
    session = in_session(catalog, photo)

    set_session_location(catalog, session.id, "St Aldo")

    assert catalog.photos.location(photo.id).name == "St Aldo"
    assert catalog.photos.location_override(photo.id) is None


def test_an_override_beats_the_session(catalog: Catalog) -> None:
    photo = stored(catalog)
    session = in_session(catalog, photo)
    set_session_location(catalog, session.id, "St Aldo")

    set_photo_location(catalog, photo.id, "Penzance")

    assert catalog.photos.location(photo.id).name == "Penzance"
    assert catalog.sessions.get(session.id).location_id is not None


def test_an_override_applies_with_no_session_at_all(catalog: Catalog) -> None:
    photo = stored(catalog)

    set_photo_location(catalog, photo.id, "Penzance")

    assert catalog.photos.location(photo.id).name == "Penzance"


def test_clearing_an_override_restores_the_sessions_location(
    catalog: Catalog,
) -> None:
    """Precedence, not replacement — clearing must give the session's answer
    back rather than leaving a hole."""
    photo = stored(catalog)
    session = in_session(catalog, photo)
    set_session_location(catalog, session.id, "St Aldo")
    set_photo_location(catalog, photo.id, "Penzance")

    clear_photo_location(catalog, photo.id)

    assert catalog.photos.location(photo.id).name == "St Aldo"


def test_an_override_affects_only_its_own_photograph(catalog: Catalog) -> None:
    odd, rest = stored(catalog), [stored(catalog), stored(catalog)]
    session = in_session(catalog, odd, *rest)
    set_session_location(catalog, session.id, "St Aldo")

    set_photo_location(catalog, odd.id, "Penzance")

    assert catalog.photos.location(odd.id).name == "Penzance"
    assert [catalog.photos.location(p.id).name for p in rest] == ["St Aldo", "St Aldo"]


def test_changing_an_override_moves_the_use(catalog: Catalog) -> None:
    photo = stored(catalog)
    first, _ = set_photo_location(catalog, photo.id, "Penzance")

    second, _ = set_photo_location(catalog, photo.id, "Marazion")

    assert catalog.locations.get(first.id).use_count == 0
    assert catalog.locations.get(second.id).use_count == 1


def test_overriding_with_the_sessions_own_location_is_still_an_override(
    catalog: Catalog,
) -> None:
    """Agreeing today does not mean agreeing tomorrow: the frame has said this
    for itself, so changing the session must not move it."""
    photo = stored(catalog)
    session = in_session(catalog, photo)
    set_session_location(catalog, session.id, "St Aldo")
    set_photo_location(catalog, photo.id, "St Aldo")

    set_session_location(catalog, session.id, "Penzance")

    assert catalog.photos.location(photo.id).name == "St Aldo"


def test_the_override_is_one_location_not_a_list(catalog: Catalog) -> None:
    """§5.1 keys photo_location on photo_id alone, because FR-3.4 is one
    override per photograph."""
    photo = stored(catalog)
    set_photo_location(catalog, photo.id, "Penzance")

    set_photo_location(catalog, photo.id, "Marazion")

    assert catalog.photos.location(photo.id).name == "Marazion"
    assert catalog.connection.execute(
        "SELECT count(*) FROM photo_location WHERE photo_id = ?", (photo.id,)
    ).fetchone()[0] == 1


def test_a_deleted_location_leaves_the_photographs_alone(catalog: Catalog) -> None:
    """§5.1: deleting vocabulary nulls the reference. Photographs are the
    irreplaceable thing; the words for them are not."""
    photo = stored(catalog)
    session = in_session(catalog, photo)
    place, _ = set_session_location(catalog, session.id, "St Aldo")

    catalog.locations.delete(place.id)

    assert catalog.photos.get(photo.id) is not None
    assert catalog.photos.location(photo.id) is None
    assert catalog.sessions.get(session.id).location_id is None


# ----------------------------------------------------------------------
# The count can be re-derived, so it can be checked
# ----------------------------------------------------------------------


def test_recounting_a_clean_catalog_corrects_nothing(catalog: Catalog) -> None:
    photo = stored(catalog)
    session = in_session(catalog, photo)
    set_session_location(catalog, session.id, "St Aldo")
    add_person_to_photo(catalog, photo.id, "Alice")
    add_tag_to_photo(catalog, photo.id, "beach")

    assert recount_uses(catalog) == {"location": 0, "person": 0, "tag": 0}


def test_recounting_repairs_a_count_that_was_damaged(catalog: Catalog) -> None:
    photo = stored(catalog)
    person, _ = add_person_to_photo(catalog, photo.id, "Alice")
    catalog.people.increment_use(person.id, 40)

    assert recount_uses(catalog)["person"] == 1
    assert catalog.people.get(person.id).use_count == 1


def test_a_long_run_of_tagging_and_untagging_does_not_drift(
    catalog: Catalog,
) -> None:
    """The invariant behind the whole column: everything it counts is a row
    that still exists. If this holds after abuse, the number is trustworthy —
    and if it ever stops holding, recount_uses says so instead of the ranking
    quietly going wrong."""
    photos = [stored(catalog) for _ in range(6)]
    session = in_session(catalog, *photos[:4])

    for cycle in range(5):
        set_session_location(catalog, session.id, "St Aldo")
        set_session_location(catalog, session.id, "Penzance")
        clear_session_location(catalog, session.id)
        for photo in photos:
            person, _ = add_person_to_photo(catalog, photo.id, f"Person {cycle % 3}")
            tag, _ = add_tag_to_photo(catalog, photo.id, "beach")
            set_photo_location(catalog, photo.id, "Marazion")
            if photo.id % 2:
                remove_person_from_photo(catalog, photo.id, person.id)
                remove_tag_from_photo(catalog, photo.id, tag.id)
                clear_photo_location(catalog, photo.id)

    assert recount_uses(catalog) == {"location": 0, "person": 0, "tag": 0}
    assert all(row.use_count >= 0 for row in catalog.locations.all())


def test_the_ranking_still_makes_sense_after_all_that(catalog: Catalog) -> None:
    """A count is only interesting because of the order it produces."""
    photos = [stored(catalog) for _ in range(4)]
    for photo in photos:
        add_tag_to_photo(catalog, photo.id, "beach")
    for photo in photos[:2]:
        add_tag_to_photo(catalog, photo.id, "birthday")
    add_tag_to_photo(catalog, photos[0].id, "bicycle")

    assert names(suggest_tags(catalog, "b")) == ["beach", "birthday", "bicycle"]


# ----------------------------------------------------------------------
# The other half of D-7: the slug reaches the filename
# ----------------------------------------------------------------------


def test_a_resolved_slug_is_what_an_imported_file_would_be_called(
    catalog: Catalog,
) -> None:
    """The hole in the middle of every name imported so far. `target_filename`
    has taken a slug since WU-2.4 and nothing had one to give it until now."""
    from photo_manager.importing import target_filename

    place = resolve_location(catalog, "St. Aldo")

    assert target_filename(CAPTURED, "DSCF1024.JPG", location_slug=place.slug) == (
        "2026-09-06_st-aldo_DSCF1024.jpg"
    )
    assert target_filename(CAPTURED, "DSCF1024.JPG") == "2026-09-06_DSCF1024.jpg"


def test_a_slug_is_safe_in_a_filename(catalog: Catalog) -> None:
    """Whatever is typed, what lands on disk is lower-case letters, digits and
    hyphens — nothing Windows or a URL will argue with."""
    import re

    for name in ["St. Aldo", "Café & Bar", "Queen's Park", "Ynys Môn", "日本", "a/b\\c"]:
        slug = resolve_location(catalog, name).slug
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug), (name, slug)
