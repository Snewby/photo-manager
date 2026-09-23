"""Sessions: grouping photographs into the outings that tagging operates on.

**Location is a property of an outing, not of a frame** (§6.3). The camera
records no GPS (C-1), so every location in this library is typed by a person,
and typing "St Aldo" fifty-four times is the design failure FR-3.3 exists to
prevent. A session is the unit that makes it once.

Three ideas, and the third is the one that took the thinking:

* **A day is the default grouping** (FR-3.1). Not a cluster, not a guess — the
  calendar day the shutter fired, which is also the folder the file is in (D-4).
* **A long gap inside a day is a suggestion** (FR-3.2). Frames more than
  ``session_gap_hours`` apart are probably two outings, and the threshold is a
  setting because it is a fact about how somebody shoots, not about this app.
* **A suggestion that cannot be declined for good is a nag.** So the shape of a
  day is recorded by *materialising* it: a day whose photographs are all in
  sessions is resolved and is never proposed again, whatever shape was chosen.
  Accepting the split writes one session per block; ignoring it writes one
  session for the whole day. Both are decisions, and the second is the default
  made explicit rather than the absence of one.

That last point is why nothing here creates a session at import time. An
unassigned photograph means *nobody has looked at this day yet*, which is a
state worth being able to see — it is FR-7.6's triage backlog in its earliest
form. Auto-creating day sessions on import would erase the difference between
a day that was considered and a day that was never seen.

**Proposing writes nothing.** :func:`propose` is a read, start to finish;
splits are offered and never applied. The verbs that write are
:func:`apply_proposal` and :func:`merge_sessions`, and both take a whole
transaction so a half-grouped day cannot exist.

What this module deliberately does *not* do is decide anything about a
photograph. Sessions carry location and event (WU-3.2, WU-3.4); grouping is
only about when the shutter fired.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from .catalog import Catalog
from .models import Photo, Session

__all__ = [
    "Frame",
    "Block",
    "DayProposal",
    "SessionError",
    "StaleProposal",
    "cluster",
    "propose",
    "apply_proposal",
    "merge_sessions",
]


class SessionError(Exception):
    """A grouping operation was asked for something it cannot do."""


class StaleProposal(SessionError):
    """The day changed after the proposal was made.

    The same habit as FR-8.6's stale-plan refusal, for the same reason: a shape
    reviewed against yesterday's day is not a shape anyone approved of today's.
    """


# ----------------------------------------------------------------------
# What a proposal is made of
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Frame:
    """One photograph, reduced to the only two things grouping cares about."""

    photo_id: int
    captured_at: dt.datetime

    #: True when this time came from mtime rather than EXIF (FR-8.2a). A
    #: session built around one is a guess about an outing, so the count is
    #: carried all the way up to :class:`Block` rather than quietly averaged in.
    inferred: bool = False

    @classmethod
    def of(cls, photo: Photo) -> Frame:
        if photo.id is None:
            raise SessionError("Photo has no id; it is not in the catalog yet")
        return cls(photo.id, photo.captured_at, photo.captured_at_inferred)

    @property
    def _order(self) -> tuple[dt.datetime, int]:
        """Sort key. The id breaks ties so clustering is stable — a burst can
        put two frames on the same second and a proposal must not reshuffle
        between runs."""
        return (self.captured_at, self.photo_id)


@dataclass(frozen=True, slots=True)
class Block:
    """A run of frames with no internal gap longer than the threshold.

    One of these becomes one session if the proposal is applied as it stands.
    """

    frames: tuple[Frame, ...]

    #: Time since the end of the block before it, or None for the day's first.
    #: This is the evidence for the suggestion — the thing a UI shows to
    #: justify offering a split at all.
    gap_before: dt.timedelta | None = None

    def __post_init__(self) -> None:
        if not self.frames:
            raise SessionError("A block with no frames is not a block")

    @property
    def started_at(self) -> dt.datetime:
        return self.frames[0].captured_at

    @property
    def ended_at(self) -> dt.datetime:
        return self.frames[-1].captured_at

    @property
    def duration(self) -> dt.timedelta:
        return self.ended_at - self.started_at

    @property
    def count(self) -> int:
        return len(self.frames)

    @property
    def photo_ids(self) -> tuple[int, ...]:
        return tuple(frame.photo_id for frame in self.frames)

    @property
    def inferred_count(self) -> int:
        """How many of these times are mtime guesses (FR-8.2a)."""
        return sum(1 for frame in self.frames if frame.inferred)

    @property
    def largest_internal_gap(self) -> dt.timedelta:
        """The biggest step between consecutive frames inside this block.

        Below the threshold for a freshly clustered block, and *above* it for
        one that has been merged — which is the point: a merged block can say
        what it swallowed instead of hiding it.
        """
        if len(self.frames) < 2:
            return dt.timedelta(0)
        return max(
            b.captured_at - a.captured_at
            for a, b in zip(self.frames, self.frames[1:])
        )


@dataclass(frozen=True, slots=True)
class DayProposal:
    """One calendar day, and the shape this module suggests for it.

    A proposal is inert. It is a description of photographs that are not yet in
    any session, and nothing about it reaches the catalog until it is handed to
    :func:`apply_proposal`.
    """

    day: dt.date
    blocks: tuple[Block, ...]

    #: The threshold the blocks were cut at, carried so a proposal can explain
    #: itself and so applying one can be reported in the terms it was made in.
    gap: dt.timedelta

    #: Sessions that already hold photographs from this day. Non-empty means
    #: the day is *partly* resolved — a later import added frames to a day that
    #: had already been grouped — and the blocks below cover only the new ones.
    existing_session_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.blocks:
            raise SessionError(f"A proposal for {self.day} with no blocks says nothing")

    @property
    def suggests_split(self) -> bool:
        """Whether this day is being offered as more than one outing."""
        return len(self.blocks) > 1

    @property
    def partly_resolved(self) -> bool:
        return bool(self.existing_session_ids)

    @property
    def frames(self) -> tuple[Frame, ...]:
        return tuple(frame for block in self.blocks for frame in block.frames)

    @property
    def frame_count(self) -> int:
        return sum(block.count for block in self.blocks)

    @property
    def photo_ids(self) -> tuple[int, ...]:
        return tuple(frame.photo_id for frame in self.frames)

    @property
    def started_at(self) -> dt.datetime:
        return self.blocks[0].started_at

    @property
    def ended_at(self) -> dt.datetime:
        return self.blocks[-1].ended_at

    @property
    def inferred_count(self) -> int:
        return sum(block.inferred_count for block in self.blocks)

    # -- reshaping, before anything is written -------------------------

    def merged(self, *indices: int) -> DayProposal:
        """A copy with a contiguous run of blocks combined into one.

        FR-3.2's *merge*, exercised on the suggestion rather than on the
        catalog — nothing has been written yet, so changing your mind here
        costs nothing at all.

        The run must be contiguous. Blocks within a day are ordered and do not
        overlap, so merging the first and the third would produce a session
        whose span contains a second session's photographs, and a span that
        contains frames it does not own is not a description of an outing.
        """
        if len(indices) < 2:
            raise SessionError("Merging needs at least two blocks")
        wanted = sorted(set(indices))
        if len(wanted) != len(indices):
            raise SessionError(f"Repeated block index in {indices}")
        for index in wanted:
            if not 0 <= index < len(self.blocks):
                raise SessionError(
                    f"No block {index} on {self.day}; there are {len(self.blocks)}"
                )
        if wanted != list(range(wanted[0], wanted[-1] + 1)):
            raise SessionError(
                f"Blocks {wanted} are not contiguous. Merging across a block that is "
                f"left out would give one session a span containing another's frames."
            )

        first, last = wanted[0], wanted[-1]
        combined = Block(
            frames=tuple(
                frame
                for block in self.blocks[first : last + 1]
                for frame in block.frames
            ),
            gap_before=self.blocks[first].gap_before,
        )
        return replace(
            self,
            blocks=self.blocks[:first] + (combined,) + self.blocks[last + 1 :],
        )

    def as_one_block(self) -> DayProposal:
        """The whole day as a single outing — FR-3.2's *ignore*.

        Declining a suggestion and merging every block are the same act, and
        saying so once here is better than two code paths that must agree. The
        result is FR-3.1's default written down, which is what stops the
        suggestion coming back tomorrow.
        """
        if len(self.blocks) == 1:
            return self
        return self.merged(*range(len(self.blocks)))


# ----------------------------------------------------------------------
# Clustering
# ----------------------------------------------------------------------


def cluster(
    frames: Iterable[Frame | Photo],
    *,
    gap: dt.timedelta,
) -> list[DayProposal]:
    """Group frames into days, and days into blocks. Pure; touches nothing.

    *gap* is the threshold a step between consecutive frames must **exceed**
    to suggest a split — FR-3.2 says "exceeds", so a gap of exactly three hours
    does not split. Frames are sorted here rather than assumed sorted, because
    a caller reading the catalog newest-first should not get a different answer
    from one reading it oldest-first.

    The day boundary always splits, even when two frames are minutes apart
    across midnight, because FR-3.1's grouping *is* the calendar day and D-4
    has already filed them in different folders. Where that is wrong — a night
    out that ran past twelve — :func:`merge_sessions` is the way back, and it
    is deliberately the only way, so the correction is a decision rather than a
    rule that guesses.
    """
    if gap <= dt.timedelta(0):
        raise SessionError(f"The gap threshold must be positive, got {gap}")

    ordered = sorted(
        (frame if isinstance(frame, Frame) else Frame.of(frame) for frame in frames),
        key=lambda frame: frame._order,
    )

    by_day: dict[dt.date, list[Frame]] = {}
    for frame in ordered:
        by_day.setdefault(frame.captured_at.date(), []).append(frame)

    proposals: list[DayProposal] = []
    for day, day_frames in sorted(by_day.items()):
        proposals.append(
            DayProposal(day=day, blocks=_split(day_frames, gap), gap=gap)
        )
    return proposals


def _split(frames: Sequence[Frame], gap: dt.timedelta) -> tuple[Block, ...]:
    """One day's frames, cut wherever the step between two exceeds *gap*."""
    blocks: list[Block] = []
    current: list[Frame] = [frames[0]]
    gap_before: dt.timedelta | None = None

    for previous, frame in zip(frames, frames[1:]):
        step = frame.captured_at - previous.captured_at
        if step > gap:
            blocks.append(Block(tuple(current), gap_before=gap_before))
            current = [frame]
            gap_before = step
        else:
            current.append(frame)

    blocks.append(Block(tuple(current), gap_before=gap_before))
    return tuple(blocks)


# ----------------------------------------------------------------------
# Against the catalog
# ----------------------------------------------------------------------


def propose(
    catalog: Catalog,
    *,
    gap: dt.timedelta | None = None,
) -> list[DayProposal]:
    """The days that still need grouping, oldest first. **Writes nothing.**

    Only photographs with no session are considered: a day already resolved
    into sessions has had its answer given and is not asked about again, which
    is what makes "ignore" stick (FR-3.2). A day holding both — a later import
    landing on a day that was grouped weeks ago — is proposed for its new
    frames only, and names the sessions already there so a UI can offer them.

    *gap* defaults to the configured ``session_gap_hours``.
    """
    if gap is None:
        gap = dt.timedelta(hours=catalog.settings.session_gap_hours)

    unassigned = catalog.photos.unassigned()
    proposals = cluster(unassigned, gap=gap)
    if not proposals:
        return []

    existing = _sessions_by_day(catalog)
    return [
        replace(proposal, existing_session_ids=existing.get(proposal.day, ()))
        for proposal in proposals
    ]


def _sessions_by_day(catalog: Catalog) -> dict[dt.date, tuple[int, ...]]:
    """Which sessions already hold photographs from which calendar day.

    Keyed off the photographs rather than off ``session.started_at``, because a
    merged session can span midnight and it is the frames that decide which
    days it has a claim on.
    """
    rows = catalog.connection.execute(
        "SELECT DISTINCT session_id, substr(captured_at, 1, 10) AS day FROM photo "
        "WHERE session_id IS NOT NULL ORDER BY day, session_id"
    )
    by_day: dict[dt.date, list[int]] = {}
    for row in rows:
        by_day.setdefault(dt.date.fromisoformat(row["day"]), []).append(row["session_id"])
    return {day: tuple(ids) for day, ids in by_day.items()}


def apply_proposal(catalog: Catalog, proposal: DayProposal) -> list[Session]:
    """Write a proposal's shape to the catalog: one session per block.

    Whatever shape the proposal is in when it arrives is the shape that is
    written — accepted as suggested, merged first, or flattened by
    :meth:`DayProposal.as_one_block`. This function has no opinion, which is
    what "splits are suggestions, never forced" means at the data layer.

    The whole day lands in one transaction. A day half-grouped is worse than
    one not grouped at all: it looks finished from the resolved end and would
    never be proposed again in full.

    Refuses a stale proposal — see :class:`StaleProposal`.
    """
    _check_fresh(catalog, proposal)

    created: list[Session] = []
    with catalog.transaction():
        for block in proposal.blocks:
            session = catalog.sessions.add(
                Session(started_at=block.started_at, ended_at=block.ended_at)
            )
            assert session.id is not None
            catalog.sessions.assign(session.id, block.photo_ids)
            created.append(session)
    return created


def _check_fresh(catalog: Catalog, proposal: DayProposal) -> None:
    """Refuse to apply a shape decided against a day that has since moved.

    Two ways it can go stale, and they need saying apart: a photograph in the
    proposal has been grouped by something else since, or the day has gained
    frames the proposal never saw. The second is the quiet one — applying
    anyway would resolve the day with the new frames left loose in it, and
    ``propose`` would then offer the remainder as if it were a day of its own.
    """
    proposed = set(proposal.photo_ids)

    current = {
        photo.id
        for photo in catalog.photos.unassigned()
        if photo.captured_at.date() == proposal.day and photo.id is not None
    }

    taken = sorted(proposed - current)
    if taken:
        raise StaleProposal(
            f"{len(taken)} photograph(s) proposed for {proposal.day} are no longer "
            f"unassigned (ids {taken[:5]}{'...' if len(taken) > 5 else ''}). "
            f"Re-propose before applying."
        )

    appeared = sorted(current - proposed)
    if appeared:
        raise StaleProposal(
            f"{proposal.day} has gained {len(appeared)} ungrouped photograph(s) since "
            f"this was proposed (ids {appeared[:5]}{'...' if len(appeared) > 5 else ''}). "
            f"Applying now would resolve the day around frames nobody looked at. "
            f"Re-propose before applying."
        )


def merge_sessions(catalog: Catalog, session_ids: Sequence[int]) -> Session:
    """Combine sessions into one, keeping the earliest and deleting the rest.

    FR-3.2's *merge* after the fact: the way back from a split that was
    accepted and regretted, and the only route to a session that crosses
    midnight (see :func:`cluster`).

    The earliest session survives rather than a fresh row being created, so
    anything already pointing at it keeps pointing at the right outing.

    Two refusals, both about not quietly losing something:

    * **A session left inside the merged span.** The result is one outing with
      one time range, so a third session whose frames fall within that range
      would be described by it without belonging to it.
    * **A conflicting location or event.** Where one side has a value and the
      other is empty the value carries; where they disagree there is no answer
      that is not a deletion, so it is named and refused instead. Tagging is
      WU-3.4's, but a merge that silently dropped it would be this unit's fault.
    """
    wanted = list(dict.fromkeys(session_ids))
    if len(wanted) < 2:
        raise SessionError("Merging needs at least two distinct sessions")

    sessions = []
    for session_id in wanted:
        session = catalog.sessions.get(session_id)
        if session is None:
            raise SessionError(f"No session with id {session_id}")
        sessions.append(session)
    sessions.sort(key=lambda s: (s.started_at, s.id or 0))

    photos = {s.id: catalog.photos.in_session(s.id) for s in sessions if s.id is not None}

    started_at = min(s.started_at for s in sessions)
    ended_at = _merged_end(sessions, photos)

    _check_nothing_stranded(catalog, sessions, started_at, ended_at)
    location_id = _agree("location", (s.location_id for s in sessions))
    event = _agree("event", (s.event for s in sessions))

    survivor = sessions[0]
    assert survivor.id is not None
    with catalog.transaction():
        for other in sessions[1:]:
            assert other.id is not None
            catalog.sessions.assign(
                survivor.id, [p.id for p in photos[other.id] if p.id is not None]
            )
            catalog.sessions.delete(other.id)
        merged = replace(
            survivor, started_at=started_at, ended_at=ended_at,
            location_id=location_id, event=event,
        )
        catalog.sessions.update(merged)
    return merged


def _merged_end(
    sessions: Sequence[Session], photos: dict[int | None, list[Photo]]
) -> dt.datetime | None:
    """The end of the merged span, or None if every side is still open.

    ``ended_at`` is NULL while a session is being assembled (§5.1), and a NULL
    is not a zero — one open session leaves the merged session open too, rather
    than closing it at the last frame anybody happens to have imported.
    """
    if any(session.ended_at is None for session in sessions):
        return None
    ends = [session.ended_at for session in sessions if session.ended_at is not None]
    ends += [
        photo.captured_at
        for session_photos in photos.values()
        for photo in session_photos
    ]
    return max(ends) if ends else None


def _check_nothing_stranded(
    catalog: Catalog,
    merging: Sequence[Session],
    started_at: dt.datetime,
    ended_at: dt.datetime | None,
) -> None:
    merging_ids = {session.id for session in merging}
    stranded = []
    for session in catalog.sessions.all(newest_first=False):
        if session.id in merging_ids:
            continue
        if session.started_at < started_at:
            continue
        if ended_at is not None and session.started_at > ended_at:
            continue
        stranded.append(session.id)
    if stranded:
        raise SessionError(
            f"Session(s) {stranded} start inside the merged span "
            f"({started_at} to {ended_at}). Merge them too, or leave these apart — "
            f"a session cannot describe a range holding another session's frames."
        )


def _agree(field: str, values: Iterable[object]) -> object:
    """The one non-empty value among these, or None. Disagreement is an error."""
    distinct = {value for value in values if value is not None}
    if len(distinct) > 1:
        raise SessionError(
            f"The sessions disagree on {field} ({sorted(map(str, distinct))}). "
            f"Merging would delete one of them; clear the one you do not want first."
        )
    return distinct.pop() if distinct else None
