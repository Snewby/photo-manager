"""Backups: refusals, verification, and incremental snapshots that look complete.

Everything is on the test machine's one disk, so "another drive" is a folder
the tests declare to be one by giving it a different device number — the same
question :func:`photo_manager.backup.device` asks of a real path.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from photo_manager import backup
from photo_manager.backup import BackupError, latest_snapshot, take_snapshot, verify_snapshot
from photo_manager.catalog import Catalog
from photo_manager.config import Settings

FIRST = dt.datetime(2026, 1, 10, 9, 0, 0)
LATER = dt.datetime(2026, 1, 10, 18, 30, 0)


@pytest.fixture
def drive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A folder that counts as another physical drive."""
    root = tmp_path / "Volumes" / "Backup"
    root.mkdir(parents=True)
    real = backup.device

    def device(path: Path) -> int | None:
        if path == root or root in path.parents:
            return -1
        return real(path)

    monkeypatch.setattr(backup, "device", device)
    return root


@pytest.fixture
def private(tmp_path: Path) -> Path:
    folder = tmp_path / "private"
    folder.mkdir()
    (folder / "PLAN.md").write_text("the plan", encoding="utf-8")
    return folder


@pytest.fixture
def setup(library_root: Path, tmp_path: Path, drive: Path, private: Path):
    """A library with two photographs and a sidecar, a live catalog, a
    thumbnail cache, a config file, and backup_root on the other drive."""
    month = library_root / "2026" / "2026-01"
    month.mkdir(parents=True)
    (month / "a.jpg").write_bytes(b"first photograph")
    (month / "b.jpg").write_bytes(b"second photograph")
    (month / "b.jpg.xmp").write_text("<x>rating 3</x>", encoding="utf-8")

    config = tmp_path / "config.toml"
    config.write_text("# a config\n", encoding="utf-8")
    settings = replace(
        Settings(library_root=library_root, state_dir=tmp_path / "state"),
        backup_root=drive / "PhotoManagerBackup", source_path=config,
    )
    with Catalog.open(settings) as catalog:  # creates a live WAL catalog
        assert catalog.photos.count() == 0
    (settings.state_dir / "cull").mkdir()
    (settings.state_dir / "cull" / "volume-0000ffff.jsonl").write_text("{}\n", encoding="utf-8")
    (settings.thumbnail_cache_dir / "ab").mkdir(parents=True)
    (settings.thumbnail_cache_dir / "ab" / "abc-400.jpg").write_bytes(b"thumb")
    return settings, month


def snap(settings: Settings, private: Path, **kwargs):
    kwargs.setdefault("now", FIRST)
    return take_snapshot(settings, private_dir=private, **kwargs)


# ----------------------------------------------------------------------
# Refusals
# ----------------------------------------------------------------------


def test_the_library_disk_is_refused_and_nothing_is_created(
    setup, private: Path, tmp_path: Path
) -> None:
    settings, _ = setup
    same_disk = tmp_path / "backups"
    with pytest.raises(BackupError, match="not attached"):
        take_snapshot(settings, same_disk, private_dir=private)
    same_disk.mkdir()
    with pytest.raises(BackupError, match="same disk"):
        take_snapshot(settings, same_disk, private_dir=private)
    assert list(same_disk.iterdir()) == []


def test_another_drive_is_accepted_without_a_drive_letter(setup, drive: Path) -> None:
    """The macOS case: no path has a drive, so only the device number can say."""
    settings, _ = setup
    assert (drive / "x").drive == settings.library_root.drive
    backup.check_destination(settings.library_root, drive / "PhotoManagerBackup")


def test_a_destination_inside_the_library_is_refused(setup, private: Path) -> None:
    settings, _ = setup
    with pytest.raises(BackupError, match="inside the library"):
        take_snapshot(settings, settings.library_root / "backup", private_dir=private)


def test_without_backup_root_there_is_nowhere_to_go(setup, private: Path) -> None:
    settings, _ = setup
    with pytest.raises(BackupError, match="backup_root"):
        take_snapshot(replace(settings, backup_root=None), private_dir=private)


def test_a_destination_that_does_not_exist_yet_is_on_its_parents_volume(tmp_path: Path) -> None:
    assert backup.device(tmp_path / "not" / "yet" / "made") == tmp_path.stat().st_dev


def test_not_enough_room_is_refused_before_anything_is_written(
    setup, private: Path, drive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _ = setup
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(1, 1, 10))
    with pytest.raises(BackupError, match="Not enough room"):
        snap(settings, private)
    assert not (drive / "PhotoManagerBackup").exists()


# ----------------------------------------------------------------------
# A snapshot
# ----------------------------------------------------------------------


def test_a_snapshot_holds_the_library_and_what_it_cannot_rebuild(setup, private: Path) -> None:
    settings, _ = setup
    result = snap(settings, private)

    assert result.ok and result.linked == 0
    assert result.snapshot.name == "2026-01-10"
    manifest = json.loads((result.snapshot / backup.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert sorted(e["path"] for e in manifest["files"]) == [
        "2026/2026-01/a.jpg", "2026/2026-01/b.jpg", "2026/2026-01/b.jpg.xmp",
    ]
    app = sorted(e["path"] for e in manifest["app_files"])
    assert "config.toml" in app and "private/PLAN.md" in app
    assert "state/catalog.sqlite3" in app and "state/cull/volume-0000ffff.jsonl" in app
    assert not any("thumbnails" in p or p.endswith(("-wal", "-shm")) for p in app)
    assert (result.snapshot / backup.READ_ME_NAME).is_file()
    assert verify_snapshot(result.snapshot) == (len(manifest["files"]) + len(app), [])


def test_the_catalog_copy_is_consistent_and_leaves_no_journal_behind(
    setup, private: Path
) -> None:
    """A live WAL database copied as bytes may not be one; the backup API's is."""
    settings, _ = setup
    result = snap(settings, private)
    copy = result.snapshot / "state" / "catalog.sqlite3"

    connection = sqlite3.connect(f"file:{copy.as_posix()}?mode=ro", uri=True)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("PRAGMA user_version").fetchone()[0] >= 1
    finally:
        connection.close()
    assert sorted(p.name for p in copy.parent.iterdir() if p.is_file()) == ["catalog.sqlite3"]


def test_library_only_leaves_the_app_files_out(setup, private: Path) -> None:
    settings, _ = setup
    result = snap(settings, private, include_app_state=False)
    manifest = json.loads((result.snapshot / backup.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["app_files"] == []


# ----------------------------------------------------------------------
# Incremental
# ----------------------------------------------------------------------


def test_a_second_snapshot_links_what_has_not_changed_and_copies_what_has(
    setup, private: Path
) -> None:
    settings, month = setup
    first = snap(settings, private)
    (month / "c.jpg").write_bytes(b"a new import")
    (month / "b.jpg.xmp").write_text("<x>rating 5</x>", encoding="utf-8")

    second = snap(settings, private, now=LATER)

    assert second.ok
    assert second.snapshot.name == "2026-01-10-183000"  # the date was taken
    assert second.incremental_from == "2026-01-10"
    inode = lambda root, name: (root / "library" / "2026" / "2026-01" / name).stat().st_ino
    assert inode(second.snapshot, "a.jpg") == inode(first.snapshot, "a.jpg")
    assert inode(second.snapshot, "b.jpg") == inode(first.snapshot, "b.jpg")
    assert inode(second.snapshot, "b.jpg.xmp") != inode(first.snapshot, "b.jpg.xmp")
    assert (second.snapshot / "library/2026/2026-01/b.jpg.xmp").read_text() == "<x>rating 5</x>"
    assert (first.snapshot / "library/2026/2026-01/b.jpg.xmp").read_text() == "<x>rating 3</x>"
    assert verify_snapshot(second.snapshot)[1] == []


def test_deleting_an_old_snapshot_loses_nothing_a_newer_one_holds(
    setup, private: Path
) -> None:
    settings, _ = setup
    first = snap(settings, private)
    second = snap(settings, private, now=LATER)
    assert second.linked > 0

    shutil.rmtree(first.snapshot)

    assert verify_snapshot(second.snapshot)[1] == []


def test_full_copies_everything_again(setup, private: Path) -> None:
    settings, _ = setup
    snap(settings, private)
    second = snap(settings, private, now=LATER, incremental=False)
    assert second.ok and second.linked == 0 and second.incremental_from is None


def test_a_drive_that_cannot_link_gets_copies_instead(
    setup, private: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exFAT has no hard links; the snapshot is the same, only bigger."""
    settings, _ = setup
    snap(settings, private)

    def no_links(source, target):
        raise OSError("Operation not supported")

    monkeypatch.setattr(backup.os, "link", no_links)
    second = snap(settings, private, now=LATER)
    assert second.ok and second.linked == 0 and second.copied == second.files


def test_a_copy_that_does_not_read_back_leaves_a_partial_snapshot(
    setup, private: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _ = setup
    real = backup._copy_and_hash

    def corrupting(source: Path, target: Path) -> str:
        digest = real(source, target)
        if source.name == "a.jpg":
            target.write_bytes(b"not what was read")
        return digest

    monkeypatch.setattr(backup, "_copy_and_hash", corrupting)
    result = snap(settings, private)

    assert not result.ok and any("a.jpg" in line for line in result.failures)
    assert result.snapshot.name.endswith(".partial")
    assert not (result.snapshot / backup.MANIFEST_NAME).exists()
    assert latest_snapshot(settings.backup_root, settings.library_root) is None


def test_verification_catches_a_snapshot_that_has_decayed(setup, private: Path) -> None:
    settings, _ = setup
    result = snap(settings, private)
    (result.snapshot / "library/2026/2026-01/a.jpg").write_bytes(b"bit rot")
    assert verify_snapshot(result.snapshot)[1] == ["library/2026/2026-01/a.jpg: does not match the manifest"]


# ----------------------------------------------------------------------
# Status
# ----------------------------------------------------------------------


def test_status_without_backup_root_says_how_to_set_it_up(setup) -> None:
    settings, _ = setup
    state = backup.status(replace(settings, backup_root=None))
    assert not state.configured and "backup_root" in state.message


def test_status_says_when_the_drive_is_not_attached(setup, tmp_path: Path) -> None:
    settings, _ = setup
    state = backup.status(replace(settings, backup_root=tmp_path / "unplugged" / "PMB"))
    assert state.configured and not state.attached and "not attached" in state.message


def test_status_counts_what_the_last_snapshot_does_not_hold(setup, private: Path) -> None:
    settings, month = setup
    snap(settings, private)
    import hashlib
    held = hashlib.sha256(b"first photograph").hexdigest()

    assert backup.status(settings, [held]).photos_not_backed_up == 0
    assert backup.status(settings, [held, "f" * 64]).photos_not_backed_up == 1
    assert backup.status(settings).sidecars_changed == 0

    (month / "b.jpg.xmp").write_text("<x>rating 1</x>", encoding="utf-8")
    (month / "a.jpg.xmp").write_text("<x>new</x>", encoding="utf-8")
    state = backup.status(settings)
    assert state.attached and state.last_snapshot == "2026-01-10"
    assert state.sidecars_changed == 2


def test_status_remembers_the_last_snapshot_while_the_drive_is_away(
    setup, private: Path, tmp_path: Path
) -> None:
    settings, _ = setup
    snap(settings, private)
    away = backup.status(replace(settings, backup_root=tmp_path / "unplugged" / "PMB"))
    assert not away.attached
    assert away.last_snapshot == "2026-01-10" and away.photos_not_backed_up == 0


# ----------------------------------------------------------------------
# The command line
# ----------------------------------------------------------------------

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "backup_library.py"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("backup_library", _TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules[spec.name]


def test_the_tool_backs_up_and_verifies(
    tool, library_root: Path, tmp_path: Path, drive: Path
) -> None:
    (library_root / "a.jpg").write_bytes(b"one")
    config = tmp_path / "config.toml"
    config.write_text(
        f"library_root = '{library_root.as_posix()}'\n"
        f"state_dir = '{(tmp_path / 'state').as_posix()}'\n"
        f"backup_root = '{(drive / 'PMB').as_posix()}'\n",
        encoding="utf-8",
    )

    assert tool.main(["--config", str(config), "--library-only"]) == tool.OK

    [snapshot] = (drive / "PMB" / library_root.name).iterdir()
    assert tool.main(["--verify-only", str(snapshot)]) == tool.OK
    (snapshot / "library" / "a.jpg").write_bytes(b"two")
    assert tool.main(["--verify-only", str(snapshot)]) == tool.FAILED


def test_the_tool_reports_a_refusal_as_an_error(tool, library_root: Path, tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f"library_root = '{library_root.as_posix()}'\n", encoding="utf-8")
    assert tool.main(["--config", str(config)]) == tool.ERROR


def test_an_unfinished_snapshot_is_never_the_one_to_link_from(setup, private: Path) -> None:
    """Even one whose manifest got written before its rename failed."""
    settings, _ = setup
    first = snap(settings, private)
    unfinished = first.snapshot.with_name("2026-01-11.partial")
    shutil.copytree(first.snapshot, unfinished)
    manifest = json.loads((unfinished / backup.MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest["taken"] = "2026-01-11T00:00:00"
    (unfinished / backup.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")

    assert latest_snapshot(settings.backup_root, settings.library_root) == first.snapshot


def test_a_live_databases_journal_files_are_not_copied_as_bytes(setup, private: Path) -> None:
    settings, _ = setup
    (settings.state_dir / "catalog.sqlite3-wal").write_bytes(b"")
    (settings.state_dir / "catalog.sqlite3-shm").write_bytes(b"")
    result = snap(settings, private)
    manifest = json.loads((result.snapshot / backup.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert not any(e["path"].endswith(("-wal", "-shm")) for e in manifest["app_files"])


def test_with_the_drive_attached_status_reads_the_drive(setup, private: Path) -> None:
    """The drive is the truth; the record beside the catalog is for when it is away."""
    settings, _ = setup
    snap(settings, private)
    shutil.rmtree(settings.state_dir / "backup")
    assert backup.status(settings).last_snapshot == "2026-01-10"
