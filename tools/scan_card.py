"""Find the memory card and report what is on it.

WU-2.1. Read-only in the strongest sense: it snapshots the card before and
after itself and refuses to report success if anything changed.

    .venv\\Scripts\\python.exe tools/scan_card.py
    .venv\\Scripts\\python.exe tools/scan_card.py --confirm

Without ``--confirm`` this reads no image bytes at all — directory metadata
only — and finishes in well under a second on a full card. ``--confirm`` then
hashes the photos whose filenames the catalog already knows, turning "likely
imported" into a fact. That reads about 14 MB per candidate over a ~25 MB/s
reader, so it is the slow part by a wide margin and the reason the two are
separate commands rather than one.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from photo_manager.cards import (  # noqa: E402
    CardStatus,
    Dedup,
    CardPhoto,
    confirm_imports,
    detect_card,
    scan_card,
    summarise,
)
from photo_manager.catalog import Catalog  # noqa: E402
from photo_manager.config import ConfigError, Settings  # noqa: E402
from photo_manager.migration import directory_snapshot  # noqa: E402

#: Exit codes, so this is usable from a script as well as by eye.
OK = 0
NO_CARD = 1
ERROR = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.toml.")
    parser.add_argument("--card", help="Force a DCIM path instead of probing for one.")
    parser.add_argument(
        "--confirm", action="store_true",
        help="Hash the already-imported candidates to settle them. Slow: it reads "
             "~14 MB per candidate off the card.",
    )
    parser.add_argument("--list", type=int, default=0, metavar="N",
                        help="Also print the first N photos.")
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
        # The point of the whole unit: this is never a silent empty grid.
        print(f"[{location.status}] {location.message}", file=sys.stderr)
        return NO_CARD

    assert location.dcim_root is not None
    before = directory_snapshot(location.dcim_root)

    started = time.perf_counter()
    catalog = _open_catalog(settings)
    try:
        scan = scan_card(settings, catalog=catalog, location=location)
        elapsed = time.perf_counter() - started
        print(summarise(scan))
        print(f"\nScanned in {elapsed:.2f} s without reading an image byte.")

        if catalog is None:
            print("No catalog yet, so every photo reads as new "
                  "(the migration in WU-1.5 has not run).")
        elif args.confirm:
            scan = _confirm(scan, catalog)
        elif scan.by_dedup(Dedup.LIKELY_IMPORTED):
            print(f"{len(scan.by_dedup(Dedup.LIKELY_IMPORTED))} photos match a filename "
                  f"already in the catalog. Re-run with --confirm to hash them.")

        print(f"\nTo cull: {len(scan.to_cull)} of {scan.photo_count}.")
        if args.list:
            _list(scan.photos[:args.list])
    finally:
        if catalog is not None:
            catalog.close()

    if directory_snapshot(location.dcim_root) != before:
        print("ERROR: the card changed during a read-only scan.", file=sys.stderr)
        return ERROR
    print(f"Card unchanged: {len(before)} entries, identical sizes and timestamps.")
    return OK


def _open_catalog(settings: Settings) -> Catalog | None:
    """The catalog if there is one, else None. Its absence is not an error —
    before WU-1.5 there is nothing to deduplicate against."""
    assert settings.database_path is not None
    if not settings.database_path.exists():
        return None
    try:
        return Catalog.open(settings)
    except Exception as exc:  # noqa: BLE001 — a card scan should survive this
        print(f"warning: could not open the catalog ({exc}); "
              f"treating every photo as new.", file=sys.stderr)
        return None


def _confirm(scan, catalog: Catalog):  # type: ignore[no-untyped-def]
    candidates = [p for p in scan.photos if p.needs_confirming]
    if not candidates:
        return scan
    total_bytes = sum(p.jpeg_bytes for p in candidates)
    print(f"\nConfirming {len(candidates)} candidates ({total_bytes / 1e9:.2f} GB) ...")

    started = time.perf_counter()
    scan = confirm_imports(scan, catalog, progress=_progress)
    elapsed = time.perf_counter() - started
    print(f"\rConfirmed in {elapsed:.1f} s "
          f"({total_bytes / 1e6 / max(elapsed, 1e-9):.0f} MB/s).{' ' * 20}")
    print("Dedup:", ", ".join(f"{count} {state}"
                              for state, count in scan.dedup_counts().items()))

    rescued = scan.by_dedup(Dedup.NOT_IMPORTED)
    if rescued:
        print(f"{len(rescued)} shared a filename but not the bytes, and are back "
              f"in the cull: {', '.join(p.primary.name for p in rescued[:8])}")
    return scan


def _progress(done: int, total: int, photo: CardPhoto) -> None:
    print(f"\r  {done:>4}/{total}  {photo.primary.name:<20}", end="", flush=True)


def _list(photos) -> None:  # type: ignore[no-untyped-def]
    print()
    for photo in photos:
        raw = "+RAF" if photo.has_raw else "    "
        print(f"  {photo.primary.name:<16} {raw} "
              f"{photo.total_bytes / 1e6:7.1f} MB  {photo.dedup}")


if __name__ == "__main__":
    raise SystemExit(main())
