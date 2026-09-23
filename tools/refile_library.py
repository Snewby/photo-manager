"""Re-file the library from day folders into month folders, or undo it.

    .venv\\Scripts\\python.exe tools/refile_library.py                 # dry, always
    .venv\\Scripts\\python.exe tools/refile_library.py --execute --backup E:\\PhotoManagerBackup\\XT30\\2026-09-23
    .venv\\Scripts\\python.exe tools/refile_library.py --reverse state/refile/reversal-<stamp>.jsonl --execute

Dry by default (REQUIREMENTS.md §4.1): a dry run plans every move, prints it,
and touches nothing. ``--execute`` also needs ``--backup``, a verified snapshot
from ``backup_library.py``, and refuses unless every file it is about to move is
in that snapshot's manifest with the same hash — a mass move of the library
does not happen without a copy of the library somewhere else.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from photo_manager.catalog import Catalog  # noqa: E402
from photo_manager.config import ConfigError, Settings  # noqa: E402
from photo_manager.refile import (  # noqa: E402
    RefileError,
    RefilePlan,
    execute_refile,
    plan_refile,
    reverse_refile,
)

OK = 0
FAILED = 1
ERROR = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.toml.")
    parser.add_argument("--execute", action="store_true",
                        help="Actually move files. Without this, nothing is touched.")
    parser.add_argument("--backup", metavar="SNAPSHOT",
                        help="A verified backup snapshot folder; required with --execute.")
    parser.add_argument("--reverse", metavar="REVERSAL_LOG",
                        help="Undo a re-file using its reversal log.")
    parser.add_argument("--fix-timestamps", action="store_true",
                        help="Set the modified time of any photograph whose capture day "
                             "only its folder records to its catalog capture time.")
    parser.add_argument("--accept-date-loss", action="store_true",
                        help="Proceed although some capture days are recorded only by "
                             "their day folder.")
    args = parser.parse_args(argv)

    try:
        settings = Settings.load(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return ERROR

    with Catalog.open(settings) as catalog:
        if args.reverse:
            return _reverse(Path(args.reverse), catalog, args.execute)

        plan = plan_refile(catalog)
        print(_summary(plan))
        if not args.execute:
            print("\nDry run - nothing was moved. Add --execute --backup <snapshot> to move.")
            return OK if not plan.strays else FAILED

        if plan.date_only_in_folder and not (args.fix_timestamps or args.accept_date_loss):
            print("\nRefusing: the photographs above keep their capture day only in "
                  "their folder name. Pass --fix-timestamps to record it in their "
                  "modified time, or --accept-date-loss.", file=sys.stderr)
            return FAILED
        if not args.backup:
            print("\nRefusing: --execute needs --backup <snapshot>.", file=sys.stderr)
            return FAILED
        missing = _not_in_backup(plan, Path(args.backup))
        if missing:
            print(f"\nRefusing: {len(missing)} file(s) are not in that backup with the "
                  f"same bytes, e.g. {missing[0]}.", file=sys.stderr)
            return FAILED
        print(f"\nBackup {args.backup} holds every file about to move.")

        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        reversal = settings.state_dir / "refile" / f"reversal-{stamp}.jsonl"
        try:
            outcome = execute_refile(
                plan, catalog, reversal_path=reversal, fix_timestamps=args.fix_timestamps
            )
        except RefileError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return FAILED

    print(f"\nMoved {len(outcome.moves)} files for {outcome.photos_refiled} photographs.")
    print(f"Reversal log: {outcome.reversal_path}")
    if not outcome.complete:
        print(f"STOPPED: {outcome.failure}", file=sys.stderr)
        return FAILED
    return OK


def _summary(plan: RefilePlan) -> str:
    lines = [
        f"Library    : {plan.library_root}",
        f"To re-file : {len(plan.photos)} photographs, {len(plan.files)} files",
        f"In place   : {plan.already_filed} photographs already in their month folder",
        "",
    ]
    for folder, count in plan.month_counts().items():
        lines.append(f"  {folder:<12} {count:>4} photographs")
    renamed = [f for f in plan.files
               if Path(f.source).name.casefold() != Path(f.target).name.casefold()]
    lines.append(f"\nRenamed to avoid a clash: {len(renamed)}")
    for f in renamed:
        lines.append(f"  {f.source} -> {f.target}")
    lines.append(f"Strays (owned by no photograph): {len(plan.strays)}")
    for s in plan.strays[:20]:
        lines.append(f"  {s}")
    lines.append(f"Capture day recorded only by the folder: {len(plan.date_only_in_folder)}")
    for photo in plan.date_only_in_folder:
        lines.append(f"  {photo.library_path} - catalog says {photo.captured_at:%Y-%m-%d}, "
                     f"the file's own timestamp says otherwise")
    return "\n".join(lines)


def _not_in_backup(plan: RefilePlan, snapshot: Path) -> list[str]:
    manifest = json.loads((snapshot / "backup-manifest.json").read_text(encoding="utf-8"))
    held = {entry["sha256"] for entry in manifest["files"]}
    return [f.source for f in plan.files if f.content_hash not in held]


def _reverse(log: Path, catalog: Catalog, execute: bool) -> int:
    if not execute:
        print(f"Would reverse the re-file recorded in {log}. Add --execute to do it.")
        return OK
    outcome = reverse_refile(log, catalog)
    print(f"Restored {outcome.restored} files; skipped {len(outcome.skipped)}.")
    for failure in outcome.failures:
        print(f"  FAILED {failure}", file=sys.stderr)
    return OK if outcome.complete else FAILED


if __name__ == "__main__":
    sys.exit(main())
