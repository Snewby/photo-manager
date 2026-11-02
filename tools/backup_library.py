"""Copy the library to other physical media, and verify every byte of it.

The same backup as the app's Backup button — both call
:mod:`photo_manager.backup`, where what a snapshot is and how it is verified
are written down. This is that, from a terminal:

    .venv/bin/python tools/backup_library.py                 # to backup_root
    .venv/bin/python tools/backup_library.py --to /Volumes/Backup/PhotoManagerBackup
    .venv/bin/python tools/backup_library.py --verify-only <snapshot folder>

(``.venv\\Scripts\\python.exe`` on Windows.)

Each run writes a dated snapshot that looks like a full copy, with files that
have not changed since the previous one hard-linked to it, and the app's own
state and documents beside the library. ``--full`` copies everything again;
``--library-only`` leaves the app's files out. **Nothing here deletes or
overwrites anything.**
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from photo_manager.backup import (  # noqa: E402
    BackupError,
    take_snapshot,
    verify_snapshot,
)
from photo_manager.config import ConfigError, Settings  # noqa: E402

OK = 0
FAILED = 1
ERROR = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.toml.")
    parser.add_argument("--to", help="Destination root. Defaults to backup_root in config.toml.")
    parser.add_argument("--verify-only", metavar="SNAPSHOT",
                        help="Re-verify an existing snapshot instead of making one.")
    parser.add_argument("--label", default="", help="A note stored with the snapshot.")
    parser.add_argument("--full", action="store_true",
                        help="Copy every file, even ones unchanged since the last snapshot.")
    parser.add_argument("--library-only", action="store_true",
                        help="Leave state/, private/ and config.toml out.")
    args = parser.parse_args(argv)

    if args.verify_only:
        return _verify_only(Path(args.verify_only).expanduser())

    try:
        settings = Settings.load(args.config)
        result = take_snapshot(
            settings,
            Path(args.to) if args.to else None,
            label=args.label,
            incremental=not args.full,
            include_app_state=not args.library_only,
            progress=_progress,
        )
    except (ConfigError, BackupError) as exc:
        _clear()
        print(f"error: {exc}", file=sys.stderr)
        return ERROR
    _clear()

    if not result.ok:
        print(f"{len(result.failures)} FILE(S) FAILED VERIFICATION:", file=sys.stderr)
        for line in result.failures:
            print(f"  {line}", file=sys.stderr)
        print(f"The backup is NOT complete. It was left at {result.snapshot}.",
              file=sys.stderr)
        return FAILED

    print(f"Backed up and verified {result.files} files in {result.seconds:.0f} s: "
          f"{result.copied} copied ({result.bytes_copied / 1e9:.2f} GB), "
          f"{result.linked} unchanged since {result.incremental_from or '-'} "
          f"and linked ({result.bytes_linked / 1e9:.2f} GB).")
    print(f"Snapshot: {result.snapshot}")
    return OK


def _verify_only(snapshot: Path) -> int:
    print(f"Verifying {snapshot}")
    checked, problems = verify_snapshot(snapshot, progress=_progress)
    _clear()
    if problems:
        print(f"{len(problems)} PROBLEM(S):", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        return FAILED
    print(f"All {checked} files present and byte-identical to when they were taken.")
    return OK


def _progress(done: int, total: int, name: str) -> None:
    """Overwrite one line, but only on a terminal. A redirected log should get a
    clean record rather than hundreds of carriage returns."""
    if sys.stdout.isatty():
        sys.stdout.write("\r" + f"  {done:>4}/{total}  {name:<40}"[:78])
        sys.stdout.flush()


def _clear() -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\r" + " " * 78 + "\r")
        sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
