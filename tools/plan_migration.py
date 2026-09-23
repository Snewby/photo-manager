"""Dry-run the library migration and write the manifest for review.

WU-1.4. Reads the flat library, works out where every file should go under
D-4's ``YYYY/YYYY-MM-DD/``, and writes two things outside the library:

* ``migration-manifest.json`` — FR-8.4's old-to-new mapping, and the input
  WU-1.5 will consume. Hand-named files carry empty ``suggested_location`` and
  ``suggested_event`` fields to fill in.
* ``migration-report.md`` — the readable version, with everything that needs a
  human's judgement collected at the top.

    .venv\\Scripts\\python.exe tools/plan_migration.py

**This moves nothing.** It snapshots the library before and after itself and
refuses to report success if a single byte, size or timestamp changed — the
claim is about hundreds of irreplaceable photos, so it is checked rather than asserted.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from photo_manager.config import ConfigError, Settings  # noqa: E402
from photo_manager.migration import (  # noqa: E402
    directory_snapshot,
    plan_migration,
    summarise,
    write_manifest,
    write_report,
)

MANIFEST_NAME = "migration-manifest.json"
REPORT_NAME = "migration-report.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.toml. Defaults to the usual search.")
    parser.add_argument("--library-root", help="Override the configured library root.")
    parser.add_argument(
        "--out-dir",
        help="Where to write the manifest and report. Defaults to <state_dir>/migration. "
             "Never inside the library root.",
    )
    parser.add_argument("--quiet", action="store_true", help="No per-file progress.")
    args = parser.parse_args(argv)

    try:
        settings = Settings.load(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.library_root:
        settings = replace(settings, library_root=Path(args.library_root).expanduser())

    try:
        root = settings.verify_library_root()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    out_dir = (Path(args.out_dir).expanduser() if args.out_dir
               else (settings.state_dir / "migration"))

    print(f"Library:  {root}")
    print(f"Output:   {out_dir}")
    print("Dry run - nothing will be moved.\n")

    before = directory_snapshot(root)

    started = time.perf_counter()
    plan = plan_migration(settings, progress=None if args.quiet else _progress)
    elapsed = time.perf_counter() - started
    if not args.quiet:
        print()

    after = directory_snapshot(root)
    if not _report_unchanged(before, after):
        return 1

    manifest_path = write_manifest(plan, out_dir / MANIFEST_NAME)
    report_path = write_report(plan, out_dir / REPORT_NAME)

    print(summarise(plan))
    print(f"\nPlanned in {elapsed:.1f} s.")
    print(f"Manifest: {manifest_path}")
    print(f"Report:   {report_path}")
    if plan.needs_review:
        print("\nSome photos are flagged. Read the report before approving WU-1.5.")
    return 0


def _progress(done: int, total: int, path: Path) -> None:
    print(f"\r  {done:>4}/{total}  {path.name:<28}", end="", flush=True)


def _report_unchanged(
    before: dict[str, tuple[int, int] | None],
    after: dict[str, tuple[int, int] | None],
) -> bool:
    """The dry-run guarantee, checked rather than claimed."""
    if before == after:
        print(f"Library unchanged: {len(before)} entries, identical sizes and timestamps.")
        return True

    print("ERROR: the library changed during a dry run. This should be impossible.",
          file=sys.stderr)
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            print(f"  {key}: {before.get(key)!r} -> {after.get(key)!r}", file=sys.stderr)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
