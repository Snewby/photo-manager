"""Grouping photographs into outings (WU-3.1).

Three things carry the weight here.

**A suggestion is never an instruction.** FR-3.2 offers a split; it does not
apply one. So there are tests that proposing writes nothing at all, on the
throwaway catalog and on the owner's real one, and tests that every shape a day can
end up in — accepted, merged, or declined whole — is reachable through the same
apply path with the module having no preference between them.

**A day is resolved once.** The shape of a day is recorded by materialising it,
which is what stops a declined suggestion coming back tomorrow. That only works
if a resolved day is never proposed again and a day that has *gained* frames
since is not quietly resolved around them, so both are tested, as is refusing a
proposal that went stale between making it and applying it.

**Merging must not lose anything.** A merge that dropped a location, or that
produced a session whose span contained another session's frames, would be this
unit quietly damaging work done in WU-3.4. Both are refusals with names in
them, and both are tested from the wrong side as well as the right one.

The real-library tests at the bottom are the acceptance criteria: 2026-09-06
and 2026-09-07, measured rather than remembered.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest

from photo_manager.catalog import Catalog
from photo_manager.config import Settings
from photo_manager.models import Location, Photo, Session
from photo_manager.sessions import (
    Block,
    DayProposal,
    Frame,
    SessionError,
    StaleProposal,
    apply_proposal,
    cluster,
    merge_sessions,
    propose,
)

THREE_HOURS = dt.timedelta(hours=3)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def at(spec: str, *, photo_id: int = 0, inferred: bool = False) -> Frame:
    """A frame at '2026-09-06 10:06' or '2026-09-06T10:06:45'."""
    return Frame(photo_id, dt.datetime.fromisoformat(spec), inferred)


def frames(*specs: str) -> list[Frame]:
    """Frames with ids 1, 2, 3... in the order given."""
    return [at(spec, photo_id=index) for index, spec in enumerate(specs, start=1)]


def shape(proposals: list[DayProposal]) -> list[tuple[dt.date, list[int]]]:
    """The bit worth asserting on: which day, and how many frames per block."""
    return [(p.day, [b.count for b in p.blocks]) for p in proposals]


def stored(catalog: Catalog, captured_at: dt.datetime, *, inferred: bool = False) -> Photo:
    """A real catalog row, with just enough filled in to be one."""
    index = catalog.photos.count() + 1
    return catalog.photos.add(
        Photo(
            content_hash=f"{index:064x}",
            library_path=f"{captured_at:%Y/%Y-%m-%d}/{captured_at:%H%M%S}_{index}.jpg",
            original_name=f"DSCF{index:04d}.JPG",
            captured_at=captured_at,
            captured_at_inferred=inferred,
            imported_at=dt.datetime(2026, 9, 21, 12, 0, 0),
        )
    )


def store_all(catalog: Catalog, *specs: str) -> list[Photo]:
    return [stored(catalog, dt.datetime.fromisoformat(spec)) for spec in specs]


# ----------------------------------------------------------------------
# Clustering: the calendar day (FR-3.1)
# ----------------------------------------------------------------------


def test_nothing_to_group_is_not_an_error() -> None:
    assert cluster([], gap=THREE_HOURS) == []


def test_one_frame_is_one_day_of_one_block() -> None:
    proposals = cluster(frames("2026-09-06 10:06"), gap=THREE_HOURS)

    assert shape(proposals) == [(dt.date(2026, 9, 6), [1])]
    assert not proposals[0].suggests_split


def test_a_day_with_no_long_gap_stays_one_block() -> None:
    proposals = cluster(
        frames("2026-09-07 16:47", "2026-09-07 16:50", "2026-09-07 16:57"),
        gap=THREE_HOURS,
    )

    assert shape(proposals) == [(dt.date(2026, 9, 7), [3])]
    assert not proposals[0].suggests_split


def test_separate_days_are_separate_proposals() -> None:
    proposals = cluster(
        frames("2026-09-06 10:06", "2026-09-07 16:47"), gap=THREE_HOURS
    )

    assert shape(proposals) == [
        (dt.date(2026, 9, 6), [1]),
        (dt.date(2026, 9, 7), [1]),
    ]


def test_days_come_back_oldest_first_whatever_order_they_arrive_in() -> None:
    """A caller reading the catalog newest-first must not get a different
    answer from one reading it oldest-first, so sorting happens here."""
    given = frames("2026-09-07 16:47", "2026-04-02 17:27", "2026-09-06 10:06")

    assert [p.day for p in cluster(given, gap=THREE_HOURS)] == [
        dt.date(2026, 4, 2),
        dt.date(2026, 9, 6),
        dt.date(2026, 9, 7),
    ]


def test_frames_out_of_order_within_a_day_still_cluster_correctly() -> None:
    given = [
        at("2026-09-06 13:02", photo_id=3),
        at("2026-09-06 10:06", photo_id=1),
        at("2026-09-06 10:18", photo_id=2),
    ]

    blocks = cluster(given, gap=dt.timedelta(hours=1))[0].blocks

    assert [b.photo_ids for b in blocks] == [(1, 2), (3,)]


def test_frames_on_the_same_second_are_ordered_by_id() -> None:
    """A burst puts two frames on one second, and a proposal that reshuffled
    between runs would make 'the same shape twice' untestable."""
    given = [
        at("2026-09-06 10:06:45", photo_id=9),
        at("2026-09-06 10:06:45", photo_id=4),
    ]

    assert cluster(given, gap=THREE_HOURS)[0].blocks[0].photo_ids == (4, 9)


def test_midnight_splits_even_when_the_frames_are_minutes_apart() -> None:
    """FR-3.1's grouping *is* the calendar day, and D-4 has already filed these
    in different folders. merge_sessions is the way back, deliberately."""
    proposals = cluster(
        frames("2026-09-06 23:55", "2026-09-07 00:05"), gap=THREE_HOURS
    )

    assert shape(proposals) == [
        (dt.date(2026, 9, 6), [1]),
        (dt.date(2026, 9, 7), [1]),
    ]


def test_a_photo_without_an_id_is_refused() -> None:
    """A frame that is not in the catalog cannot be assigned to a session, and
    finding that out at INSERT time would be far too late."""
    unsaved = Photo(
        content_hash="a" * 64,
        library_path="2026/2026-09-06/x.jpg",
        original_name="DSCF0001.JPG",
        captured_at=dt.datetime(2026, 9, 6, 10, 6),
        imported_at=dt.datetime(2026, 9, 21, 12, 0),
    )

    with pytest.raises(SessionError, match="no id"):
        cluster([unsaved], gap=THREE_HOURS)


# ----------------------------------------------------------------------
# Clustering: the gap (FR-3.2)
# ----------------------------------------------------------------------


def test_a_gap_over_the_threshold_suggests_a_split() -> None:
    proposals = cluster(
        frames("2026-06-30 14:15", "2026-06-30 18:00"), gap=THREE_HOURS
    )

    assert shape(proposals) == [(dt.date(2026, 6, 30), [1, 1])]
    assert proposals[0].suggests_split


def test_a_gap_of_exactly_the_threshold_does_not_split() -> None:
    """FR-3.2 says *exceeds*. A boundary that splits and a boundary that does
    not are one line apart, so the line is pinned here."""
    proposals = cluster(
        frames("2026-06-30 14:00", "2026-06-30 17:00"), gap=THREE_HOURS
    )

    assert shape(proposals) == [(dt.date(2026, 6, 30), [2])]


def test_a_gap_one_second_over_the_threshold_does_split() -> None:
    proposals = cluster(
        frames("2026-06-30 14:00:00", "2026-06-30 17:00:01"), gap=THREE_HOURS
    )

    assert shape(proposals) == [(dt.date(2026, 6, 30), [1, 1])]


def test_the_threshold_is_a_knob_and_the_same_day_answers_differently() -> None:
    """The real 2026-09-06 is one block at three hours and three blocks at
    fifty minutes. Both are correct answers to different questions, which is
    why session_gap_hours is a setting and not a constant."""
    day = frames(
        "2026-09-06 10:06", "2026-09-06 11:09",  # 63 min apart
        "2026-09-06 12:03", "2026-09-06 13:02",  # 54 and 59 min
    )

    assert shape(cluster(day, gap=THREE_HOURS)) == [(dt.date(2026, 9, 6), [4])]
    assert shape(cluster(day, gap=dt.timedelta(minutes=50))) == [
        (dt.date(2026, 9, 6), [1, 1, 1, 1])
    ]


def test_a_block_carries_the_gap_that_justified_it() -> None:
    """The evidence for the suggestion, so a UI can show why it is offering
    one rather than just that it is."""
    blocks = cluster(
        frames("2026-06-30 14:15", "2026-06-30 18:00"), gap=THREE_HOURS
    )[0].blocks

    assert blocks[0].gap_before is None
    assert blocks[1].gap_before == dt.timedelta(hours=3, minutes=45)


def test_a_freshly_cut_block_has_no_internal_gap_over_the_threshold() -> None:
    block = cluster(
        frames("2026-09-06 10:06", "2026-09-06 12:00", "2026-09-06 13:34"),
        gap=THREE_HOURS,
    )[0].blocks[0]

    assert block.largest_internal_gap == dt.timedelta(hours=1, minutes=54)
    assert block.largest_internal_gap <= THREE_HOURS


def test_a_block_reports_its_span_and_its_inferred_frames() -> None:
    """FR-8.2a's flag reaches all the way up here: a session built around an
    mtime guess is a guess about an outing, and should be able to say so."""
    block = cluster(
        [
            at("2026-04-02 17:27", photo_id=1),
            at("2026-04-02 17:29", photo_id=2, inferred=True),
        ],
        gap=THREE_HOURS,
    )[0].blocks[0]

    assert block.started_at == dt.datetime(2026, 4, 2, 17, 27)
    assert block.ended_at == dt.datetime(2026, 4, 2, 17, 29)
    assert block.duration == dt.timedelta(minutes=2)
    assert block.count == 2
    assert block.inferred_count == 1


def test_a_negative_or_zero_threshold_is_refused() -> None:
    """Zero would split every frame into its own session, which is a bug that
    looks like a lot of successful work."""
    for bad in (dt.timedelta(0), dt.timedelta(hours=-1)):
        with pytest.raises(SessionError, match="positive"):
            cluster(frames("2026-09-06 10:06"), gap=bad)


def test_an_empty_block_or_proposal_cannot_be_built() -> None:
    with pytest.raises(SessionError):
        Block(frames=())
    with pytest.raises(SessionError):
        DayProposal(day=dt.date(2026, 9, 6), blocks=(), gap=THREE_HOURS)


# ----------------------------------------------------------------------
# Reshaping a suggestion before it is applied (FR-3.2's merge and ignore)
# ----------------------------------------------------------------------


def three_blocks() -> DayProposal:
    return cluster(
        frames("2026-09-06 08:00", "2026-09-06 13:00", "2026-09-06 18:00"),
        gap=THREE_HOURS,
    )[0]


def test_merging_two_adjacent_blocks_combines_them() -> None:
    merged = three_blocks().merged(0, 1)

    assert [b.count for b in merged.blocks] == [2, 1]
    assert merged.blocks[0].photo_ids == (1, 2)


def test_a_merged_block_says_what_gap_it_swallowed() -> None:
    """Below the threshold when cut, above it once merged — which is the point:
    a merged block reports the gap rather than hiding it."""
    merged = three_blocks().merged(0, 1)

    assert merged.blocks[0].largest_internal_gap == dt.timedelta(hours=5)
    assert merged.blocks[0].gap_before is None
    assert merged.blocks[1].gap_before == dt.timedelta(hours=5)


def test_merging_non_contiguous_blocks_is_refused() -> None:
    """The result would be a session whose span contains another session's
    frames, which is not a description of an outing."""
    with pytest.raises(SessionError, match="not contiguous"):
        three_blocks().merged(0, 2)


def test_merging_needs_two_blocks_that_exist() -> None:
    with pytest.raises(SessionError, match="at least two"):
        three_blocks().merged(0)
    with pytest.raises(SessionError, match="No block 7"):
        three_blocks().merged(0, 7)
    with pytest.raises(SessionError, match="Repeated"):
        three_blocks().merged(1, 1)


def test_ignoring_a_suggestion_is_the_whole_day_as_one_block() -> None:
    """FR-3.2's third verb. Declining and merging everything are the same act,
    and the result is FR-3.1's default written down rather than left implied."""
    declined = three_blocks().as_one_block()

    assert [b.count for b in declined.blocks] == [3]
    assert not declined.suggests_split
    assert declined.blocks[0].largest_internal_gap == dt.timedelta(hours=5)


def test_flattening_a_single_block_day_changes_nothing() -> None:
    single = cluster(frames("2026-09-07 16:47"), gap=THREE_HOURS)[0]

    assert single.as_one_block() is single


def test_reshaping_leaves_the_original_proposal_alone() -> None:
    """Proposals are frozen and reshaping returns a new one, so a UI can offer
    'accept' and 'merge' side by side without one spoiling the other."""
    original = three_blocks()

    original.merged(0, 1)
    original.as_one_block()

    assert [b.count for b in original.blocks] == [1, 1, 1]


# ----------------------------------------------------------------------
# Against the catalog: proposing
# ----------------------------------------------------------------------


def test_proposing_reads_the_catalog_and_uses_the_configured_gap(
    catalog: Catalog,
) -> None:
    store_all(catalog, "2026-06-30 14:15", "2026-06-30 18:00", "2026-09-07 16:47")

    proposals = propose(catalog)

    assert catalog.settings.session_gap_hours == 3.0
    assert shape(proposals) == [
        (dt.date(2026, 6, 30), [1, 1]),
        (dt.date(2026, 9, 7), [1]),
    ]
    assert proposals[0].gap == THREE_HOURS


def test_the_gap_can_be_overridden_per_call(catalog: Catalog) -> None:
    store_all(catalog, "2026-06-30 14:15", "2026-06-30 18:00")

    assert shape(propose(catalog, gap=dt.timedelta(hours=4))) == [
        (dt.date(2026, 6, 30), [2])
    ]


def test_proposing_writes_nothing(catalog: Catalog) -> None:
    """The whole claim of 'suggestions, never forced', asserted rather than
    described: after proposing, no session exists and no photo is in one."""
    photos = store_all(catalog, "2026-06-30 14:15", "2026-06-30 18:00")

    proposals = propose(catalog)

    assert proposals[0].suggests_split
    assert catalog.sessions.all() == []
    assert [p.id for p in catalog.photos.unassigned()] == [p.id for p in photos]


def test_an_empty_catalog_proposes_nothing(catalog: Catalog) -> None:
    assert propose(catalog) == []


def test_photos_already_in_a_session_are_not_proposed_again(
    catalog: Catalog,
) -> None:
    """What makes a declined suggestion stay declined."""
    photos = store_all(catalog, "2026-09-06 10:06", "2026-09-07 16:47")
    session = catalog.sessions.add(Session(started_at=photos[0].captured_at))
    assert session.id is not None
    catalog.sessions.assign(session.id, [photos[0].id])

    assert [p.day for p in propose(catalog)] == [dt.date(2026, 9, 7)]


def test_a_day_that_is_partly_grouped_says_so_and_names_its_sessions(
    catalog: Catalog,
) -> None:
    """A later import landing on a day grouped weeks ago. The new frames are
    proposed; the existing session is named so a UI can offer it instead."""
    old, new = store_all(catalog, "2026-09-06 10:06", "2026-09-06 18:00")
    session = catalog.sessions.add(Session(started_at=old.captured_at))
    assert session.id is not None
    catalog.sessions.assign(session.id, [old.id])

    proposal = propose(catalog)[0]

    assert proposal.partly_resolved
    assert proposal.existing_session_ids == (session.id,)
    assert proposal.photo_ids == (new.id,)


def test_a_session_spanning_midnight_is_listed_against_both_its_days(
    catalog: Catalog,
) -> None:
    """Existing sessions are found through their photographs rather than their
    start time, because a merged session can cross midnight and has a claim on
    both days."""
    late, early, loose = store_all(
        catalog, "2026-09-06 23:55", "2026-09-07 00:05", "2026-09-07 18:00"
    )
    session = catalog.sessions.add(
        Session(started_at=late.captured_at, ended_at=early.captured_at)
    )
    assert session.id is not None
    catalog.sessions.assign(session.id, [late.id, early.id])

    proposal = propose(catalog)[0]

    assert proposal.day == dt.date(2026, 9, 7)
    assert proposal.existing_session_ids == (session.id,)
    assert proposal.photo_ids == (loose.id,)


def test_inferred_dates_are_grouped_and_counted(catalog: Catalog) -> None:
    stored(catalog, dt.datetime(2026, 4, 2, 17, 27))
    stored(catalog, dt.datetime(2026, 4, 2, 17, 29), inferred=True)

    proposal = propose(catalog)[0]

    assert proposal.frame_count == 2
    assert proposal.inferred_count == 1


# ----------------------------------------------------------------------
# Against the catalog: applying
# ----------------------------------------------------------------------


def test_accepting_a_split_makes_one_session_per_block(catalog: Catalog) -> None:
    morning, evening = store_all(catalog, "2026-06-30 14:15", "2026-06-30 18:00")

    created = apply_proposal(catalog, propose(catalog)[0])

    assert len(created) == 2
    assert [catalog.photos.get(morning.id).session_id] == [created[0].id]
    assert [catalog.photos.get(evening.id).session_id] == [created[1].id]
    assert created[0].started_at == created[0].ended_at == morning.captured_at
    assert created[1].started_at == created[1].ended_at == evening.captured_at


def test_a_sessions_span_is_its_first_and_last_frame(catalog: Catalog) -> None:
    store_all(catalog, "2026-09-07 16:47", "2026-09-07 16:52", "2026-09-07 16:57")

    session = apply_proposal(catalog, propose(catalog)[0])[0]

    assert session.started_at == dt.datetime(2026, 9, 7, 16, 47)
    assert session.ended_at == dt.datetime(2026, 9, 7, 16, 57)


def test_ignoring_a_suggestion_makes_one_session_for_the_day(
    catalog: Catalog,
) -> None:
    photos = store_all(catalog, "2026-06-30 14:15", "2026-06-30 18:00")

    created = apply_proposal(catalog, propose(catalog)[0].as_one_block())

    assert len(created) == 1
    assert {p.id for p in catalog.photos.in_session(created[0].id)} == {
        p.id for p in photos
    }
    assert created[0].started_at == dt.datetime(2026, 6, 30, 14, 15)
    assert created[0].ended_at == dt.datetime(2026, 6, 30, 18, 0)


def test_applying_has_no_preference_between_the_three_answers(
    catalog: Catalog,
) -> None:
    """Accept, merge-then-accept and decline all run the same code with the
    same standing. That is 'never forced' at the data layer: the module has no
    way to favour its own suggestion."""
    store_all(catalog, "2026-09-06 08:00", "2026-09-06 13:00", "2026-09-06 18:00")
    offered = propose(catalog)[0]

    assert [b.count for b in offered.blocks] == [1, 1, 1]
    assert [b.count for b in offered.merged(0, 1).blocks] == [2, 1]
    assert [b.count for b in offered.as_one_block().blocks] == [3]

    assert len(apply_proposal(catalog, offered.merged(0, 1))) == 2


def test_a_resolved_day_is_never_proposed_again(catalog: Catalog) -> None:
    """The whole reason the shape is materialised rather than remembered
    elsewhere: a suggestion that cannot be declined for good is a nag."""
    store_all(catalog, "2026-06-30 14:15", "2026-06-30 18:00")

    apply_proposal(catalog, propose(catalog)[0].as_one_block())

    assert propose(catalog) == []


def test_applying_one_day_leaves_the_others_alone(catalog: Catalog) -> None:
    store_all(catalog, "2026-09-06 10:06", "2026-09-07 16:47")
    proposals = propose(catalog)

    apply_proposal(catalog, proposals[0])

    assert [p.day for p in propose(catalog)] == [dt.date(2026, 9, 7)]


def test_a_failure_part_way_through_leaves_no_half_grouped_day(
    catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A day resolved at one end looks finished from that end and would never
    be proposed in full again, so the whole day is one transaction."""
    store_all(catalog, "2026-09-06 08:00", "2026-09-06 13:00", "2026-09-06 18:00")
    proposal = propose(catalog)[0]

    calls = {"n": 0}
    real_add = catalog.sessions.add

    def fail_on_the_third(session: Session) -> Session:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("disk full")
        return real_add(session)

    monkeypatch.setattr(catalog.sessions, "add", fail_on_the_third)

    with pytest.raises(RuntimeError, match="disk full"):
        apply_proposal(catalog, proposal)

    assert catalog.sessions.all() == []
    assert len(catalog.photos.unassigned()) == 3


def test_applying_a_proposal_whose_photos_were_grouped_since_is_refused(
    catalog: Catalog,
) -> None:
    photos = store_all(catalog, "2026-09-06 08:00", "2026-09-06 18:00")
    proposal = propose(catalog)[0]

    elsewhere = catalog.sessions.add(Session(started_at=photos[0].captured_at))
    assert elsewhere.id is not None
    catalog.sessions.assign(elsewhere.id, [photos[0].id])

    with pytest.raises(StaleProposal, match="no longer unassigned"):
        apply_proposal(catalog, proposal)


def test_applying_a_proposal_that_missed_a_new_frame_is_refused(
    catalog: Catalog,
) -> None:
    """The quiet one. Applying anyway would resolve the day with the new frame
    still loose in it, and the next propose would offer the remainder as if it
    were a day of its own."""
    store_all(catalog, "2026-09-06 08:00")
    proposal = propose(catalog)[0]

    store_all(catalog, "2026-09-06 18:00")

    with pytest.raises(StaleProposal, match="gained 1 ungrouped"):
        apply_proposal(catalog, proposal)

    assert catalog.sessions.all() == []


def test_a_refused_proposal_can_be_remade_and_then_applied(
    catalog: Catalog,
) -> None:
    """The refusal is a redirect, not a dead end."""
    store_all(catalog, "2026-09-06 08:00")
    stale = propose(catalog)[0]
    store_all(catalog, "2026-09-06 18:00")

    with pytest.raises(StaleProposal):
        apply_proposal(catalog, stale)

    assert len(apply_proposal(catalog, propose(catalog)[0])) == 2


# ----------------------------------------------------------------------
# Merging sessions after the fact
# ----------------------------------------------------------------------


def two_sessions(catalog: Catalog) -> list[Session]:
    store_all(catalog, "2026-06-30 14:15", "2026-06-30 18:00")
    return apply_proposal(catalog, propose(catalog)[0])


def test_merging_combines_two_sessions_into_the_earlier_one(
    catalog: Catalog,
) -> None:
    """The earliest survives rather than a fresh row being made, so anything
    already pointing at it keeps pointing at the right outing."""
    morning, evening = two_sessions(catalog)

    merged = merge_sessions(catalog, [evening.id, morning.id])

    assert merged.id == morning.id
    assert catalog.sessions.get(evening.id) is None
    assert len(catalog.photos.in_session(merged.id)) == 2


def test_a_merged_session_spans_both(catalog: Catalog) -> None:
    morning, evening = two_sessions(catalog)

    merged = merge_sessions(catalog, [morning.id, evening.id])

    assert merged.started_at == dt.datetime(2026, 6, 30, 14, 15)
    assert merged.ended_at == dt.datetime(2026, 6, 30, 18, 0)


def test_merging_is_the_only_way_across_midnight(catalog: Catalog) -> None:
    """cluster() always splits at the day boundary (FR-3.1), so the night out
    that ran past twelve is corrected here — as a decision, not a rule that
    guesses."""
    store_all(catalog, "2026-09-06 23:55", "2026-09-07 00:05")
    before, after = [apply_proposal(catalog, p)[0] for p in propose(catalog)]

    merged = merge_sessions(catalog, [before.id, after.id])

    assert merged.started_at == dt.datetime(2026, 9, 6, 23, 55)
    assert merged.ended_at == dt.datetime(2026, 9, 7, 0, 5)
    assert len(catalog.photos.in_session(merged.id)) == 2
    assert propose(catalog) == []


def test_merging_carries_a_location_or_event_that_only_one_side_has(
    catalog: Catalog,
) -> None:
    morning, evening = two_sessions(catalog)
    place = catalog.locations.add(Location(name="St Aldo", slug="st-aldo"))
    catalog.sessions.update(replace(evening, location_id=place.id, event="Beach day"))

    merged = merge_sessions(catalog, [morning.id, evening.id])

    assert merged.location_id == place.id
    assert merged.event == "Beach day"


def test_merging_sessions_that_disagree_is_refused_by_name(
    catalog: Catalog,
) -> None:
    """There is no answer here that is not a deletion, and WU-3.4's tagging is
    the work that would be deleted."""
    morning, evening = two_sessions(catalog)
    here = catalog.locations.add(Location(name="St Aldo", slug="st-aldo"))
    there = catalog.locations.add(Location(name="Penzance", slug="penzance"))
    catalog.sessions.update(replace(morning, location_id=here.id, event="Beach day"))
    catalog.sessions.update(replace(evening, location_id=there.id, event="Pub"))

    with pytest.raises(SessionError, match="disagree on location"):
        merge_sessions(catalog, [morning.id, evening.id])

    assert catalog.sessions.get(evening.id) is not None


def test_a_refused_merge_changes_nothing(catalog: Catalog) -> None:
    morning, evening = two_sessions(catalog)
    catalog.sessions.update(replace(morning, event="Beach day"))
    catalog.sessions.update(replace(evening, event="Pub"))

    with pytest.raises(SessionError, match="disagree on event"):
        merge_sessions(catalog, [morning.id, evening.id])

    assert len(catalog.sessions.all()) == 2
    assert len(catalog.photos.in_session(morning.id)) == 1
    assert len(catalog.photos.in_session(evening.id)) == 1


def test_merging_around_a_session_left_out_is_refused(catalog: Catalog) -> None:
    """The result would be one outing with one time range, and a third session
    inside that range would be described by it without belonging to it."""
    store_all(catalog, "2026-09-06 08:00", "2026-09-06 13:00", "2026-09-06 18:00")
    first, middle, last = apply_proposal(catalog, propose(catalog)[0])

    with pytest.raises(SessionError, match=f"{middle.id}"):
        merge_sessions(catalog, [first.id, last.id])

    assert len(catalog.sessions.all()) == 3


def test_merging_all_three_is_fine(catalog: Catalog) -> None:
    store_all(catalog, "2026-09-06 08:00", "2026-09-06 13:00", "2026-09-06 18:00")
    sessions = apply_proposal(catalog, propose(catalog)[0])

    merged = merge_sessions(catalog, [s.id for s in sessions])

    assert len(catalog.sessions.all()) == 1
    assert len(catalog.photos.in_session(merged.id)) == 3


def test_merging_an_open_session_leaves_the_result_open(catalog: Catalog) -> None:
    """ended_at is NULL while a session is still being assembled (§5.1), and a
    NULL is not a zero — closing it at the last frame anybody happens to have
    imported would be inventing a fact."""
    morning, evening = two_sessions(catalog)
    catalog.sessions.update(replace(evening, ended_at=None))

    merged = merge_sessions(catalog, [morning.id, evening.id])

    assert merged.ended_at is None


def test_merging_needs_two_distinct_sessions_that_exist(catalog: Catalog) -> None:
    morning, evening = two_sessions(catalog)

    with pytest.raises(SessionError, match="at least two"):
        merge_sessions(catalog, [morning.id])
    with pytest.raises(SessionError, match="at least two"):
        merge_sessions(catalog, [morning.id, morning.id])
    with pytest.raises(SessionError, match="No session with id 9999"):
        merge_sessions(catalog, [morning.id, 9999])


def test_merging_does_not_disturb_an_unrelated_day(catalog: Catalog) -> None:
    morning, evening = two_sessions(catalog)
    store_all(catalog, "2026-09-07 16:47")
    elsewhere = apply_proposal(catalog, propose(catalog)[0])[0]

    merge_sessions(catalog, [morning.id, evening.id])

    assert catalog.sessions.get(elsewhere.id) is not None
    assert len(catalog.photos.in_session(elsewhere.id)) == 1


def test_deleting_a_session_does_not_delete_its_photographs(
    catalog: Catalog,
) -> None:
    """Merging deletes session rows, so the schema's ON DELETE SET NULL is what
    stands between a regrouping and losing photographs. Worth one test of its
    own, here, where the deleting happens."""
    morning, evening = two_sessions(catalog)

    merge_sessions(catalog, [morning.id, evening.id])

    assert catalog.photos.count() == 2


# ----------------------------------------------------------------------
# The real library — WU-3.1's acceptance criteria
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


@pytest.fixture
def real_frames(real_library: Settings) -> list[Frame]:
    """Every photograph in the real catalog, as frames. A read, and only a read."""
    with Catalog.open(real_library) as catalog:
        photos = catalog.photos.all()
    if not photos:
        pytest.skip("the real catalog is empty")
    return [Frame.of(photo) for photo in photos]


def day_of(proposals: list[DayProposal], day: str) -> DayProposal:
    wanted = dt.date.fromisoformat(day)
    for proposal in proposals:
        if proposal.day == wanted:
            return proposal
    pytest.skip(f"the real library has no photographs on {day}")


@pytest.mark.needs_library
def test_every_real_day_is_one_calendar_day_and_accounts_for_every_frame(
    real_frames: list[Frame],
) -> None:
    """Nothing is dropped and nothing is invented, across the whole library."""
    proposals = cluster(real_frames, gap=THREE_HOURS)

    assert sum(p.frame_count for p in proposals) == len(real_frames)
    assert len({frame.photo_id for p in proposals for frame in p.frames}) == len(
        real_frames
    )
    assert [p.day for p in proposals] == sorted({p.day for p in proposals})
    for proposal in proposals:
        for frame in proposal.frames:
            assert frame.captured_at.date() == proposal.day


@pytest.mark.needs_library
def test_proposing_against_the_real_catalog_writes_nothing(
    real_library: Settings,
) -> None:
    """The safety claim, on the owner's own data. Grouping their library is their
    decision to make in WU-3.4, and nothing here may make it for them."""
    with Catalog.open(real_library) as catalog:
        before_sessions = catalog.sessions.all()
        before_unassigned = [p.id for p in catalog.photos.unassigned()]

        proposals = propose(catalog)

        assert proposals, "the real catalog has nothing to group"
        assert catalog.sessions.all() == before_sessions
        assert [p.id for p in catalog.photos.unassigned()] == before_unassigned
