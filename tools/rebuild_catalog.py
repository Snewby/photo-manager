"""Rebuild the catalog from the library and its XMP sidecars, and compare.

WU-3.5. The recovery path this whole design leans on: NFR-2 calls the catalog
an index rather than the truth, and success criterion 4 is "delete the SQLite
file, rebuild, lose no tags". This is the thing that does it.

    .venv\\Scripts\\python.exe tools/rebuild_catalog.py
    .venv\\Scripts\\python.exe tools/rebuild_catalog.py --execute

**Dry by default.** Without ``--execute`` it builds a new catalog beside the
old one, compares the two photograph by photograph, prints what would change,
and leaves the real catalog exactly where it was. That is the same two-step as
the migration and the import: something that rewrites the index of an
irreplaceable library should not do it as a side effect of being run.

``--execute`` swaps it in, keeping the old one as ``.sqlite3.replaced-<stamp>``
so a rebuild that turns out to be worse than what it replaced can be undone by
renaming one file.

**Nothing here writes inside the library.** It reads every photograph to hash
it and parses each sidecar, and the library is snapshotted before and after to
prove it — the same check FR-8.3 makes of the migration's dry run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from photo_manager.catalog import Catalog  # noqa: E402
from photo_manager.config import ConfigError, Settings  # noqa: E402
from photo_manager.migration import directory_snapshot  # noqa: E402
from photo_manager.rebuild import Comparison, RebuildError, compare, rebuild  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.toml.")
    parser.add_argument(
        "--execute", action="store_true",
        help="Replace the real catalog with the rebuilt one. Dry without it.",
    )
    parser.add_argument("--quiet", action="store_true", help="No per-photograph progress.")
    args = parser.parse_args(argv)

    try:
        settings = Settings.load(args.config)
        root = settings.verify_library_root()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    assert settings.database_path is not None
    current = Path(settings.database_path)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = current.with_name(f"{current.stem}.rebuilt-{stamp}{current.suffix}")

    print(f"Library:  {root}")
    print(f"Catalog:  {current}{'' if current.exists() else '  (none yet)'}")
    print(f"Building: {target}")
    print("Dry run - the real catalog will not be touched.\n" if not args.execute
          else "Will REPLACE the catalog once the comparison is printed.\n")

    before_snapshot = directory_snapshot(root)
    try:
        result = rebuild(settings, target, progress=None if args.quiet else _progress)
    except RebuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not args.quiet:
        print()

    if directory_snapshot(root) != before_snapshot:
        print("ERROR: the library changed during a rebuild. This should be impossible.",
              file=sys.stderr)
        return 1

    print(f"Rebuilt {result.photographs} photographs into {result.sessions} outings "
          f"in {result.seconds:.1f} s "
          f"({result.bytes_hashed / 1e9:.2f} GB hashed).")
    print(f"  {result.sidecars_read} sidecar(s) read, "
          f"{result.locations} location(s), {result.people} person/people, "
          f"{result.tags} tag(s), {result.inferred_dates} inferred date(s).")

    for note in result.notes:
        print(f"  note: {note}")
    for entry in result.unreadable:
        print(f"  not a photograph: {entry.path.name} - {entry.reason}")
    for entry in result.duplicates:
        print(f"  duplicate: {entry.path} - {entry.reason}")

    if not current.exists():
        print("\nThere was no catalog to compare against.")
        return _finish(args, target, current)

    rebuilt_settings = replace(settings, database_path=target)
    with Catalog.open(settings) as old, Catalog.open(rebuilt_settings, verify_root=False) as new:
        comparison = compare(old, new)
    _report(comparison)

    if not comparison.lossless and args.execute:
        print("\nRefusing to replace a catalog with one that loses tags.", file=sys.stderr)
        print(f"The rebuilt catalog is at {target} if you want to look at it.",
              file=sys.stderr)
        return 1

    return _finish(args, target, current)


def _report(comparison: Comparison) -> None:
    print(f"\nAgainst the existing catalog: {comparison.summary()}")
    if comparison.lossless:
        # FR-6.6's argument about zeroes: "nothing was lost" is the fact the
        # whole exercise exists to establish, so it is said out loud.
        print("  No tag was lost. Success criterion 4 holds for this library.")
    for digest in comparison.missing[:20]:
        print(f"  LOST: {digest[:12]}... has no file in the library any more")
    for difference in comparison.changed[:20]:
        print(f"  CHANGED: {difference.library_path} - {', '.join(difference.fields())}")
        print(f"     was {difference.before}")
        print(f"     now {difference.after}")
    if comparison.added:
        print(f"  {len(comparison.added)} photograph(s) on disk had no row before; "
              f"the index had drifted.")


def _finish(args: argparse.Namespace, target: Path, current: Path) -> int:
    if not args.execute:
        print(f"\nNothing replaced. The rebuilt catalog is at {target}.")
        print("Re-run with --execute to put it in place.")
        return 0

    if current.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        kept = current.with_name(f"{current.name}.replaced-{stamp}")
        current.replace(kept)
        print(f"\nOld catalog kept at {kept}")
    # WAL and shared-memory files belong to the old database; leaving them
    # beside a different one would be at best confusing and at worst a
    # half-applied transaction from a catalog that no longer exists.
    for suffix in ("-wal", "-shm"):
        stale = current.with_name(current.name + suffix)
        stale.unlink(missing_ok=True)
    target.replace(current)
    print(f"Catalog replaced: {current}")
    return 0


def _progress(done: int, total: int, path: Path) -> None:
    print(f"\r  {done:>5}/{total}  {path.name[:40]:<40}", end="", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
