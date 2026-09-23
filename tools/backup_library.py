"""Copy the library to other physical media, and verify every byte of it.

WU-1.5's gate asks for "a verified backup on separate physical media". Both
halves matter: a copy nobody checked is a belief rather than a backup, and a
copy on the same disk does not survive the thing it exists to survive.

    .venv\\Scripts\\python.exe tools/backup_library.py --to E:\\PhotoManagerBackup
    .venv\\Scripts\\python.exe tools/backup_library.py --verify-only E:\\...\\2026-09-20

Each run writes a **dated snapshot** — ``<destination>/<library>/<date>/`` —
mirroring the library's own structure, so restoring is a plain copy back.
Snapshots are dated rather than mirrored in place because the next thing to
happen to this library is a mass move (WU-1.5), and a before and an after that
sit side by side are worth far more than one folder tracking whatever state the
library happens to be in today.

**This tool never deletes or overwrites anything.** It refuses a destination
that already holds files rather than merging into it, and nothing here removes
a stale snapshot; that is a decision for a human with a full drive.

Verification is end to end. Each file is hashed as it is read, then the copy is
re-read from the destination and hashed again, so a truncated write or a bad
sector on arrival is caught rather than assumed away. Where the WU-1.4
migration manifest is available the source hashes are cross-checked against it
too, which also proves the manifest still describes the library it was made
from.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from photo_manager.config import ConfigError, Settings  # noqa: E402
from photo_manager.hashing import DEFAULT_CHUNK_SIZE, content_hash  # noqa: E402

OK = 0
FAILED = 1
ERROR = 2

MANIFEST_NAME = "backup-manifest.json"
READ_ME_NAME = "READ-ME-FIRST.txt"

READ_ME = """\
Photo library backup - {label}

Source   : {source}
Taken    : {taken}
Files    : {count}
Bytes    : {size:,}
Verified : every file was re-read from this drive and its SHA-256 matched the
           source it was copied from.

This folder is a plain copy of the photo library with its directory structure
intact. To restore, copy the contents of `library/` back over the library root.
Nothing here is compressed, encrypted or in a proprietary format - the files
are the photographs.

{manifest} lists every file with its SHA-256, so this backup can be re-verified
at any time:

    python tools/backup_library.py --verify-only "<this folder>"
"""


@dataclass
class Result:
    copied: int = 0
    bytes_copied: int = 0
    cross_checked: int = 0
    failures: list[str] = field(default_factory=list)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.toml.")
    parser.add_argument("--to", help="Destination root, e.g. E:\\PhotoManagerBackup")
    parser.add_argument("--verify-only", metavar="SNAPSHOT",
                        help="Re-verify an existing snapshot instead of making one.")
    parser.add_argument("--label", default="", help="A note stored with the snapshot.")
    args = parser.parse_args(argv)

    if args.verify_only:
        return _verify_only(Path(args.verify_only).expanduser())

    if not args.to:
        parser.error("--to is required unless --verify-only is used")

    try:
        settings = Settings.load(args.config)
        source = settings.verify_library_root()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return ERROR

    destination = Path(args.to).expanduser().resolve()
    snapshot = destination / source.name / dt.date.today().isoformat()

    try:
        _check_target(source, destination, snapshot)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return ERROR

    files = sorted(p for p in source.rglob("*") if p.is_file())
    total_bytes = sum(p.stat().st_size for p in files)
    free = shutil.disk_usage(destination.anchor).free

    print(f"Source     : {source}")
    print(f"Snapshot   : {snapshot}")
    print(f"To copy    : {len(files)} files, {total_bytes / 1e9:.2f} GB")
    print(f"Free there : {free / 1e9:.1f} GB")
    if free < total_bytes * 1.05:
        print("error: not enough free space at the destination.", file=sys.stderr)
        return ERROR
    print()

    result = _copy_all(source, snapshot / "library", files, _manifest_hashes(settings))

    if result.failures:
        print(f"\n{len(result.failures)} FILE(S) FAILED VERIFICATION:", file=sys.stderr)
        for line in result.failures:
            print(f"  {line}", file=sys.stderr)
        print("The backup is NOT complete. Do not treat WU-1.5's gate as satisfied.",
              file=sys.stderr)
        return FAILED

    if result.cross_checked:
        print(f"Cross-checked {result.cross_checked} files against the WU-1.4 manifest.")
    _write_manifest(snapshot, source, files, args.label)
    print(f"\nBacked up and verified {result.copied} files, "
          f"{result.bytes_copied / 1e9:.2f} GB.")
    print(f"Snapshot: {snapshot}")
    return OK


def _check_target(source: Path, destination: Path, snapshot: Path) -> None:
    """Validate first, create last.

    Creating the destination before checking it left an empty folder behind on
    every refused run — a tool that declines to do something should not leave
    litter proving it thought about it.
    """
    if destination.exists() and not destination.is_dir():
        raise ValueError(f"{destination} is not a directory")

    resolved = source.resolve()
    if destination == resolved or resolved in destination.parents:
        raise ValueError(
            f"{destination} is inside the library root. A copy of something, filed "
            f"inside that same something, is not a backup of it."
        )
    if destination.drive.upper() == resolved.drive.upper():
        raise ValueError(
            f"{destination} is on {destination.drive}, the same volume as the library. "
            f"WU-1.5's gate asks for separate physical media, whose entire purpose is "
            f"surviving that volume failing. Use an external drive."
        )
    if snapshot.exists() and any(snapshot.iterdir()):
        raise ValueError(
            f"{snapshot} already exists and is not empty. This tool never overwrites; "
            f"move or rename it, or wait until tomorrow."
        )

    destination.mkdir(parents=True, exist_ok=True)


def _manifest_hashes(settings: Settings) -> dict[str, str]:
    """Source hashes from the WU-1.4 migration manifest, if one is around."""
    assert settings.state_dir is not None
    path = settings.state_dir / "migration" / "migration-manifest.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

    hashes: dict[str, str] = {}
    for entry in data.get("photos", []):
        hashes[entry["source"]] = entry["content_hash"]
        if entry.get("raw_source") and entry.get("raw_content_hash"):
            hashes[entry["raw_source"]] = entry["raw_content_hash"]
    return hashes


def _copy_all(
    source: Path, into: Path, files: list[Path], expected: dict[str, str]
) -> Result:
    result = Result()
    for index, path in enumerate(files, start=1):
        relative = path.relative_to(source)
        target = into / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _progress(index, len(files), relative.as_posix())

        source_hash = _copy_and_hash(path, target)
        shutil.copystat(path, target)

        landed = content_hash(target)
        if landed != source_hash:
            result.failures.append(
                f"{relative.as_posix()}: wrote {source_hash[:12]} but read back "
                f"{landed[:12]}"
            )
            continue

        known = expected.get(relative.as_posix())
        if known is not None:
            if known != source_hash:
                result.failures.append(
                    f"{relative.as_posix()}: source is {source_hash[:12]} but the "
                    f"WU-1.4 manifest recorded {known[:12]} - the library has changed "
                    f"since the migration was planned"
                )
                continue
            result.cross_checked += 1

        result.copied += 1
        result.bytes_copied += target.stat().st_size

    _clear()
    return result


def _copy_and_hash(source: Path, target: Path) -> str:
    """Copy, hashing the bytes on the way through so the source is read once."""
    digest = hashlib.sha256()
    with source.open("rb") as reader, target.open("wb") as writer:
        while chunk := reader.read(DEFAULT_CHUNK_SIZE):
            digest.update(chunk)
            writer.write(chunk)
    return digest.hexdigest()


def _write_manifest(snapshot: Path, source: Path, files: list[Path], label: str) -> None:
    library = snapshot / "library"
    entries = []
    for path in files:
        relative = path.relative_to(source).as_posix()
        copy = library / relative
        entries.append({
            "path": relative,
            "bytes": copy.stat().st_size,
            "sha256": content_hash(copy),
        })

    taken = dt.datetime.now().isoformat(timespec="seconds")
    (snapshot / MANIFEST_NAME).write_text(
        json.dumps(
            {"source": str(source), "taken": taken, "label": label, "files": entries},
            indent=2,
        ),
        encoding="utf-8",
    )
    (snapshot / READ_ME_NAME).write_text(
        READ_ME.format(
            label=label or "routine",
            source=source,
            taken=taken,
            count=len(entries),
            size=sum(int(e["bytes"]) for e in entries),
            manifest=MANIFEST_NAME,
        ),
        encoding="utf-8",
    )


def _verify_only(snapshot: Path) -> int:
    """Re-read a snapshot from its drive and check it against its own manifest."""
    manifest_path = snapshot / MANIFEST_NAME
    if not manifest_path.is_file():
        print(f"error: no {MANIFEST_NAME} in {snapshot}", file=sys.stderr)
        return ERROR

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest["files"]
    library = snapshot / "library"
    print(f"Verifying {len(entries)} files in {snapshot}")
    print(f"Taken {manifest['taken']} from {manifest['source']}\n")

    failures: list[str] = []
    for index, entry in enumerate(entries, start=1):
        path = library / entry["path"]
        _progress(index, len(entries), entry["path"])
        if not path.is_file():
            failures.append(f"{entry['path']}: missing")
        elif content_hash(path) != entry["sha256"]:
            failures.append(f"{entry['path']}: hash does not match the manifest")

    _clear()
    if failures:
        print(f"{len(failures)} PROBLEM(S):", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return FAILED

    print(f"All {len(entries)} files present and byte-identical to when they were taken.")
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
