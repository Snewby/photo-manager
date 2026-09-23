"""Show how the library's photographs group into outings, and optionally apply it.

WU-3.1. Reads the catalog, groups by calendar day (FR-3.1) and suggests a split
wherever consecutive frames are more than ``session_gap_hours`` apart (FR-3.2).

    .venv\\Scripts\\python.exe tools/plan_sessions.py
    .venv\\Scripts\\python.exe tools/plan_sessions.py --gap 0.85
    .venv\\Scripts\\python.exe tools/plan_sessions.py --apply 2026-09-06 --one-session

**It reports and stops.** Nothing is written without ``--apply``, because the
shape of a day is a statement about where somebody was, and FR-3.2 makes that a
suggestion the user accepts, merges or ignores. ``--gap`` is there because a
threshold is a fact about how somebody shoots: 2026-09-06 is one outing at three
hours and three at fifty minutes, and neither answer is wrong.

Applying is per day and deliberately explicit:

    --apply 2026-09-06                 one session per suggested block
    --apply 2026-09-06 --one-session   the whole day as one outing (ignore)
    --apply 2026-09-06 --merge 0,1     combine blocks 0 and 1, keep the rest
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from photo_manager.catalog import Catalog  # noqa: E402
from photo_manager.config import ConfigError, Settings  # noqa: E402
from photo_manager.sessions import (  # noqa: E402
    DayProposal,
    SessionError,
    apply_proposal,
    propose,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.toml. Defaults to the usual search.")
    parser.add_argument(
        "--gap", type=float, metavar="HOURS",
        help="Split threshold in hours. Defaults to the configured session_gap_hours.",
    )
    parser.add_argument(
        "--apply", metavar="YYYY-MM-DD",
        help="Write this day's grouping to the catalog. Without it, nothing is written.",
    )
    parser.add_argument(
        "--one-session", action="store_true",
        help="With --apply: the whole day as one outing, declining the split (FR-3.2).",
    )
    parser.add_argument(
        "--merge", metavar="i,j[,k]",
        help="With --apply: combine this contiguous run of blocks first.",
    )
    args = parser.parse_args(argv)

    try:
        settings = Settings.load(args.config)
        settings.verify_library_root()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    gap = dt.timedelta(hours=args.gap) if args.gap else None

    with Catalog.open(settings) as catalog:
        try:
            proposals = propose(catalog, gap=gap)
        except SessionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        grouped = catalog.photos.count() - len(catalog.photos.unassigned())
        print(f"Catalog:  {catalog.photos.count()} photographs, "
              f"{grouped} already in a session")
        print(f"Gap:      {(gap or dt.timedelta(hours=settings.session_gap_hours))} "
              f"between frames suggests a split (FR-3.2)")
        print()

        if not proposals:
            print("Every photograph is already in a session. Nothing to group.")
            return 0

        for proposal in proposals:
            _print_day(proposal)

        splits = sum(1 for p in proposals if p.suggests_split)
        print(f"\n{len(proposals)} day(s) to group, "
              f"{sum(p.frame_count for p in proposals)} photographs, "
              f"{splits} with a suggested split.")

        if not args.apply:
            print("\nNothing written. Re-run with --apply <day> to group one.")
            return 0

        return _apply(catalog, proposals, args)


def _print_day(proposal: DayProposal) -> None:
    marker = "*" if proposal.suggests_split else " "
    print(f"{marker} {proposal.day}  {proposal.frame_count:>4} frames  "
          f"{proposal.started_at:%H:%M}-{proposal.ended_at:%H:%M}  "
          f"{len(proposal.blocks)} block(s)")

    if proposal.partly_resolved:
        print(f"      already grouped here: session(s) "
              f"{', '.join(map(str, proposal.existing_session_ids))}")
    if proposal.inferred_count:
        print(f"      {proposal.inferred_count} of these times came from mtime, "
              f"not EXIF (FR-8.2a)")

    if not proposal.suggests_split:
        return
    for index, block in enumerate(proposal.blocks):
        gap = "" if block.gap_before is None else f"after a gap of {_hm(block.gap_before)}"
        print(f"      [{index}] {block.started_at:%H:%M}-{block.ended_at:%H:%M}  "
              f"{block.count:>4} frames  {gap}")


def _hm(delta: dt.timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _apply(catalog: Catalog, proposals: list[DayProposal], args: argparse.Namespace) -> int:
    try:
        day = dt.date.fromisoformat(args.apply)
    except ValueError:
        print(f"error: --apply wants a date like 2026-09-06, not {args.apply!r}",
              file=sys.stderr)
        return 2

    chosen = next((p for p in proposals if p.day == day), None)
    if chosen is None:
        print(f"error: {day} is not in the list above - it has no ungrouped photographs.",
              file=sys.stderr)
        return 2

    try:
        if args.merge:
            chosen = chosen.merged(*(int(part) for part in args.merge.split(",")))
        if args.one_session:
            chosen = chosen.as_one_block()
        created = apply_proposal(catalog, chosen)
    except (SessionError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\n{day}: {len(created)} session(s) created.")
    for session, block in zip(created, chosen.blocks):
        print(f"  session {session.id}: {session.started_at:%H:%M}-"
              f"{session.ended_at:%H:%M}, {block.count} photographs")
    print("Untagged for now - locations and events are WU-3.2 and WU-3.4.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
