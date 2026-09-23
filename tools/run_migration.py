"""Execute the reviewed migration, or reverse one.

WU-1.5. This is the tool that moves hundreds of irreplaceable photographs, so it does
nothing at all unless told twice:

    .venv\\Scripts\\python.exe tools/run_migration.py              # dry, always
    .venv\\Scripts\\python.exe tools/run_migration.py --execute    # the real thing
    .venv\\Scripts\\python.exe tools/run_migration.py --reverse state/migration/reversal.jsonl --execute

Dry by default is a standing rule (REQUIREMENTS.md §4.1), not a convenience. A
dry run re-plans the library, prints what would happen, and touches nothing.

``--execute`` re-plans from scratch rather than replaying the stored manifest,
then refuses to move anything unless every source is present with the hash it
was planned with and every target is free. The manifest on disk is the thing a
human reviewed; the plan made here has to agree with it, or the review is stale
and the run stops.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from photo_manager.catalog import Catalog  # noqa: E402
from photo_manager.config import ConfigError, Settings  # noqa: E402
from photo_manager.migration import (  # noqa: E402
    MigrationError,
    MigrationPlan,
    Move,
    OverrideError,
    PlannedPhoto,
    execute_migration,
    load_overrides,
    plan_migration,
    read_reversal_log,
    reverse_migration,
    summarise,
)

OK = 0
FAILED = 1
ERROR = 2

OVERRIDES = REPO_ROOT / "private" / "migration-overrides.toml"
REVERSAL_NAME = "reversal.jsonl"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.toml.")
    parser.add_argument("--execute", action="store_true",
                        help="Actually move files. Without this, nothing is touched.")
    parser.add_argument("--reverse", metavar="REVERSAL_LOG",
                        help="Undo a migration using its reversal manifest.")
    parser.add_argument("--overrides", default=str(OVERRIDES),
                        help="Review decisions to apply. Defaults to the committed file.")
    args = parser.parse_args(argv)

    try:
        settings = Settings.load(args.config)
        settings.verify_library_root()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return ERROR

    if args.reverse:
        return _reverse(Path(args.reverse), execute=args.execute)
    return _forward(settings, Path(args.overrides), execute=args.execute)


def _forward(settings: Settings, overrides_path: Path, *, execute: bool) -> int:
    try:
        overrides = load_overrides(overrides_path)
    except OverrideError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return ERROR

    print(f"Library   : {settings.library_root}")
    print(f"Overrides : {overrides_path if overrides else 'none'}")
    for override in overrides.values():
        print(f"            {override.name} -> {override.captured_at}")
    print(f"Mode      : {'EXECUTE - files will move' if execute else 'dry run'}\n")

    print("Planning ...")
    try:
        plan = plan_migration(settings, overrides=overrides, progress=_planning)
    except OverrideError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return ERROR
    _clear()
    print(summarise(plan))

    if not execute:
        print("\nDry run. Nothing moved. Re-run with --execute to do it for real.")
        return OK

    reversal_path = settings.state_dir / "migration" / REVERSAL_NAME
    print(f"\nMoving {plan.file_count} files. Reversal manifest: {reversal_path}")

    started = time.perf_counter()
    with Catalog.open(settings) as catalog:
        try:
            outcome = execute_migration(
                plan, catalog, reversal_path=reversal_path, progress=_moving
            )
        except MigrationError as exc:
            print(f"\nerror: {exc}", file=sys.stderr)
            return ERROR
    _clear()
    elapsed = time.perf_counter() - started

    print(f"Moved {outcome.files_moved} files, catalogued "
          f"{outcome.photos_catalogued} photographs, in {elapsed:.1f} s.")
    if not outcome.complete:
        print(f"\nSTOPPED: {outcome.failure}", file=sys.stderr)
        print(f"What did move is recorded in {outcome.reversal_path} and can be "
              f"put back with --reverse.", file=sys.stderr)
        return FAILED

    print(f"Reversal manifest: {outcome.reversal_path}")
    print(f"Catalog: {settings.database_path}")
    return OK


def _reverse(log_path: Path, *, execute: bool) -> int:
    if not log_path.is_file():
        print(f"error: no reversal manifest at {log_path}", file=sys.stderr)
        return ERROR

    try:
        header, moves = read_reversal_log(log_path)
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return ERROR

    print(f"Reversal manifest : {log_path}")
    print(f"Library root      : {header.get('library_root')}")
    print(f"Files to put back : {len(moves)}")
    print(f"Mode              : {'EXECUTE' if execute else 'dry run'}\n")

    if not execute:
        for move in moves[:10]:
            print(f"  {move.target}  ->  {move.source}")
        if len(moves) > 10:
            print(f"  ... and {len(moves) - 10} more")
        print("\nDry run. Nothing moved. Re-run with --execute to do it for real.")
        return OK

    try:
        outcome = reverse_migration(log_path, progress=_reversing)
    except MigrationError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return ERROR
    _clear()

    print(f"Restored {outcome.restored} files.")
    for note in outcome.skipped:
        print(f"  skipped: {note}")
    if outcome.failures:
        print(f"\n{len(outcome.failures)} FAILED:", file=sys.stderr)
        for note in outcome.failures:
            print(f"  {note}", file=sys.stderr)
        return FAILED
    print("The catalog was left alone; it is rebuildable from the library (NFR-2).")
    return OK


def _planning(done: int, total: int, path: Path) -> None:
    _line(f"  planning {done:>4}/{total}  {path.name}")


def _moving(done: int, total: int, photo: PlannedPhoto) -> None:
    _line(f"  moving {done:>4}/{total}  {photo.source.name}")


def _reversing(done: int, total: int, move: Move) -> None:
    _line(f"  restoring {done:>4}/{total}  {move.source}")


def _line(text: str) -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\r" + text[:78].ljust(78))
        sys.stdout.flush()


def _clear() -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\r" + " " * 78 + "\r")
        sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
