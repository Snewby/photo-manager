"""Import the culled keepers from the card into the library.

    .venv\\Scripts\\python.exe tools/import_card.py
    .venv\\Scripts\\python.exe tools/import_card.py --execute

**Dry by default.** Without ``--execute`` this reads EXIF headers, works out
every target name, resolves every collision and prints what it would do —
copying nothing. That is the same habit as the library migration (FR-8.3), for
a weaker reason: an import cannot lose a photograph, because the card keeps the
original. It is still much easier to notice a wrong folder or an unexpected
collision in a list than in a library.

The decisions come from the cull file this card already has
(``state\\cull\\<card-key>.jsonl``). If you have not culled it, there is
nothing to import and this will say so rather than importing everything.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from photo_manager.cards import detect_card, scan_card  # noqa: E402
from photo_manager.catalog import Catalog  # noqa: E402
from photo_manager.config import ConfigError, Settings  # noqa: E402
from photo_manager.culling import CullStore, Verdict, card_key  # noqa: E402
from photo_manager.importing import (  # noqa: E402
    ImportFailed,
    PlannedImport,
    execute_import,
    plan_import,
    summarise,
)
from photo_manager.paths import directory_snapshot  # noqa: E402

OK = 0
NO_CARD = 1
ERROR = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.toml.")
    parser.add_argument("--card", help="Force a DCIM path instead of probing for one.")
    parser.add_argument("--execute", action="store_true",
                        help="Actually copy. Without this, nothing is written.")
    parser.add_argument("--location", help="Location slug for D-7 filenames. "
                                           "Omitted, names fall back to date + shutter number.")
    parser.add_argument("--allow-overrun", action="store_true",
                        help="Import even if it would not leave a gigabyte free.")
    parser.add_argument("--list", type=int, default=0, metavar="N",
                        help="Print the first N planned targets.")
    args = parser.parse_args(argv)

    try:
        settings = Settings.load(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return ERROR

    if args.card:
        settings = replace(settings, card_dcim_root=Path(args.card).expanduser())

    location = detect_card(settings)
    if not location.found:
        print(f"[{location.status}] {location.message}", file=sys.stderr)
        return NO_CARD
    assert location.dcim_root is not None

    scan = scan_card(settings, location=location)
    store = CullStore.for_card(settings, card_key(location.dcim_root))
    resumed = store.resume(scan)
    keepers = {
        index: decision for index, decision in resumed.decisions.items()
        if decision.verdict == Verdict.KEEP
    }

    print(f"{location.message}")
    print(f"Cull file: {store.path}")
    print(f"{len(resumed.decisions)} decisions, {len(keepers)} keepers.")
    if resumed.stale:
        print(f"WARNING: {len(resumed.stale)} stored decisions did not match the file "
              f"on the card and were ignored: {', '.join(resumed.stale[:8])}")
    if not keepers:
        print("Nothing to import. Cull the card first.")
        return OK

    plan = plan_import(scan, keepers, settings, location_slug=args.location)
    _describe(plan, args.list)

    if not args.execute:
        print("\nDry run. Nothing was copied. Re-run with --execute to import.")
        return OK
    if not plan.fits and not args.allow_overrun:
        print(f"\nRefusing: this needs {plan.total_bytes / 1e9:.2f} GB and "
              f"{settings.library_root} has {plan.free_bytes / 1e9:.2f} GB free. "
              f"Use --allow-overrun if you really mean it.", file=sys.stderr)
        return ERROR

    before = directory_snapshot(location.dcim_root)
    started = time.perf_counter()
    with Catalog.open(settings) as catalog:
        outcome = execute_import(
            plan, catalog, allow_overrun=args.allow_overrun, progress=_progress,
        )
    elapsed = time.perf_counter() - started

    print(f"\r{' ' * 70}\r", end="")
    print(summarise(outcome))
    print(f"\n{elapsed:.1f} s "
          f"({outcome.bytes_written / 1e6 / max(elapsed, 1e-9):.0f} MB/s).")

    if directory_snapshot(location.dcim_root) != before:
        print("ERROR: the card changed during an import that only reads it.",
              file=sys.stderr)
        return ERROR
    print(f"Card unchanged: {len(before)} entries, identical sizes and timestamps.")
    return OK if outcome.complete else ERROR


def _describe(plan, limit: int) -> None:  # type: ignore[no-untyped-def]
    print(f"\nWould import {len(plan.photos)} photographs "
          f"({plan.total_bytes / 1e9:.2f} GB, {plan.raw_count} with their RAF) "
          f"into {len(plan.target_directories)} date folders.")
    print(f"Free on {plan.library_root}: {plan.free_bytes / 1e9:.2f} GB "
          f"({'fits' if plan.fits else 'DOES NOT FIT'}).")

    if plan.collisions:
        print(f"\n{len(plan.collisions)} name collisions, suffixed:")
        for photo in plan.collisions[:10]:
            print(f"  {photo.original_name} -> {photo.target}")
    if plan.unplannable:
        print(f"\n{len(plan.unplannable)} could not be planned:")
        for entry in plan.unplannable[:10]:
            print(f"  {entry.name}: {entry.reason}")
    if limit:
        print()
        for photo in plan.photos[:limit]:
            raw = " +RAF" if photo.raw_target else "     "
            print(f"  {photo.original_name:<16} ->  {photo.target}{raw}")


def _progress(done: int, total: int, planned: PlannedImport) -> None:
    print(f"\r  {done:>4}/{total}  {planned.original_name:<20}", end="", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
