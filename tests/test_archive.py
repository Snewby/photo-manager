"""The archive drive: a verified, add-only mirror of the library.

As with the backup tests, "another drive" is a folder the tests declare to be
one by giving it a different device number.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest

from photo_manager import archive, backup
from photo_manager.archive import ArchiveError, archive_status, check_archive, run_archive
from photo_manager.catalog import Catalog
from photo_manager.config import Settings
from photo_manager.hashing import content_hash
from photo_manager.models import Photo

TAKEN = dt.datetime(2026, 1, 10, 9, 0, 0)


@pytest.fixture
def drive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A folder that counts as another physical drive."""
    root = tmp_path / "Volumes" / "My Passport"
    root.mkdir(parents=True)
    real = backup.device

    def device(path: Path) -> int | None:
        if path == root or root in path.parents:
            return -1
        return real(path)

    monkeypatch.setattr(backup, "device", device)
    return root


@pytest.fixture
def setup(library_root: Path, tmp_path: Path, drive: Path):
    """Two photographs (one with a RAF), a sidecar, and their catalog rows."""
    month = library_root / "2026" / "2026-01"
    month.mkdir(parents=True)
    (month / "a.jpg").write_bytes(b"first photograph")
    (month / "b.jpg").write_bytes(b"second photograph")
    (month / "b.raf").write_bytes(b"second photograph, raw")
    (month / "b.jpg.xmp").write_text("<x>rating 3</x>", encoding="utf-8")

    settings = replace(
        Settings(library_root=library_root, state_dir=tmp_path / "state"),
        archive_root=drive / "Photos",
    )
    with Catalog.open(settings) as catalog:
        for name, raw in (("a.jpg", None), ("b.jpg", "2026/2026-01/b.raf")):
            catalog.photos.add(Photo(
                content_hash=content_hash(month / name),
                library_path=f"2026/2026-01/{name}",
                original_name=name.upper(),
                captured_at=TAKEN, imported_at=TAKEN, raw_path=raw,
            ))
    return settings, month


def archived(settings: Settings) -> set[str]:
    with Catalog.open(settings) as catalog:
        return set(catalog.archive.all())


def go(settings: Settings, **kwargs) -> archive.ArchiveResult:
    with Catalog.open(settings) as catalog:
        return run_archive(settings, catalog, **kwargs)


# ----------------------------------------------------------------------
# Refusals
# ----------------------------------------------------------------------


def test_no_archive_configured_is_refused(settings: Settings) -> None:
    with pytest.raises(ArchiveError, match="archive_root"):
        check_archive(settings)


def test_an_unplugged_drive_is_refused_and_nothing_is_written(setup, tmp_path: Path) -> None:
    settings, _ = setup
    settings = replace(settings, archive_root=tmp_path / "Volumes" / "Gone" / "Photos")

    with pytest.raises(ArchiveError, match="not attached|exists"):
        go(settings)

    assert not (tmp_path / "Volumes" / "Gone").exists()
    assert archived(settings) == set()


def test_the_library_disk_is_refused(setup, tmp_path: Path) -> None:
    settings, _ = setup
    same_disk = tmp_path / "elsewhere"
    same_disk.mkdir()
    with pytest.raises(ArchiveError, match="same disk"):
        go(replace(settings, archive_root=same_disk))


# ----------------------------------------------------------------------
# Archiving
# ----------------------------------------------------------------------


def test_every_file_lands_at_the_same_path_verified(setup, drive: Path) -> None:
    settings, month = setup

    result = go(settings)

    assert result.ok and result.copied == 4 and result.failures == []
    mirror = drive / "Photos" / "2026" / "2026-01"
    for name in ("a.jpg", "b.jpg", "b.raf", "b.jpg.xmp"):
        assert (mirror / name).read_bytes() == (month / name).read_bytes()
    assert archived(settings) == {
        "2026/2026-01/a.jpg", "2026/2026-01/b.jpg",
        "2026/2026-01/b.raf", "2026/2026-01/b.jpg.xmp",
    }
    assert not list(drive.rglob("*.partial"))


def test_a_second_run_copies_nothing(setup) -> None:
    settings, _ = setup
    go(settings)

    again = go(settings)

    assert again.ok and again.files == 0 and again.copied == 0


def test_a_changed_sidecar_is_archived_again_and_photographs_are_not(setup, drive: Path) -> None:
    settings, month = setup
    go(settings, now=lambda: TAKEN)  # recorded as verified long ago

    (month / "b.jpg.xmp").write_text("<x>rating 5</x>", encoding="utf-8")
    with Catalog.open(settings) as catalog:
        assert archive_status(settings, catalog).sidecars_changed == 1
    result = go(settings)

    assert result.copied == 1
    assert (drive / "Photos" / "2026" / "2026-01" / "b.jpg.xmp").read_text() == "<x>rating 5</x>"


def test_a_different_photograph_at_the_same_path_is_never_overwritten(setup, drive: Path) -> None:
    settings, _ = setup
    clash = drive / "Photos" / "2026" / "2026-01" / "a.jpg"
    clash.parent.mkdir(parents=True)
    clash.write_bytes(b"somebody else's photograph")

    result = go(settings)

    assert not result.ok
    assert any("never overwritten" in failure for failure in result.failures)
    assert clash.read_bytes() == b"somebody else's photograph"
    assert "2026/2026-01/a.jpg" not in archived(settings)


def test_a_copy_that_reads_back_wrong_gets_no_row(setup, drive: Path, monkeypatch) -> None:
    settings, _ = setup
    real = archive.content_hash

    def lying(path: Path) -> str:
        if path.name == "a.jpg.partial":
            return "0" * 64
        return real(path)

    monkeypatch.setattr(archive, "content_hash", lying)
    result = go(settings)

    assert any("read back" in failure for failure in result.failures)
    assert "2026/2026-01/a.jpg" not in archived(settings)
    assert (drive / "Photos" / "2026" / "2026-01" / "a.jpg.partial").exists()
    assert not (drive / "Photos" / "2026" / "2026-01" / "a.jpg").exists()


def test_a_mac_copy_that_no_longer_matches_the_catalog_is_not_archived(setup) -> None:
    settings, month = setup
    (month / "a.jpg").write_bytes(b"bit rot")

    result = go(settings)

    assert any("no longer matches the catalog" in failure for failure in result.failures)
    assert "2026/2026-01/a.jpg" not in archived(settings)


def test_photographs_already_in_a_snapshot_on_the_drive_are_linked(setup, drive: Path) -> None:
    settings, _ = setup
    settings = replace(settings, backup_root=drive / "PhotoManagerBackup")
    backup.take_snapshot(settings, include_app_state=False)

    result = go(settings, snapshot=False)

    assert result.ok
    assert result.linked == 4 and result.copied == 0
    linked = drive / "Photos" / "2026" / "2026-01" / "a.jpg"
    assert linked.stat().st_nlink == 2


def test_the_apps_state_is_snapshotted_without_the_photographs(setup, drive: Path, tmp_path) -> None:
    settings, _ = setup
    private = tmp_path / "private"
    private.mkdir()
    settings = replace(settings, backup_root=drive / "PhotoManagerBackup")

    result = go(settings, private_dir=private)

    assert result.ok and result.snapshot is not None
    assert (result.snapshot / "state").is_dir()
    assert not (result.snapshot / "library").exists()


# ----------------------------------------------------------------------
# Status
# ----------------------------------------------------------------------


def test_status_counts_what_is_owed_and_survives_the_drive_going_away(setup, tmp_path) -> None:
    settings, _ = setup
    with Catalog.open(settings) as catalog:
        before = archive_status(settings, catalog)
    assert before.attached and before.photos == 2 and before.photos_not_archived == 2

    go(settings)
    unplugged = replace(settings, archive_root=tmp_path / "Volumes" / "Gone" / "Photos")
    with Catalog.open(unplugged) as catalog:
        after = archive_status(unplugged, catalog)

    assert not after.attached
    assert after.photos_archived == 2 and after.photos_not_archived == 0
    assert after.last_verified is not None


def test_a_photograph_whose_raw_is_not_archived_is_not_archived(setup) -> None:
    settings, _ = setup
    go(settings)
    with Catalog.open(settings) as catalog:
        catalog.connection.execute("DELETE FROM archive_file WHERE path LIKE '%.raf'")
        catalog.connection.commit()
        assert archive_status(settings, catalog).photos_archived == 1


# ----------------------------------------------------------------------
# Working without the Mac's copy
# ----------------------------------------------------------------------


def take_off_the_mac(settings: Settings, month: Path, name: str) -> int:
    """What *Free up Mac space* will do, by hand: the Mac's files go and the
    row says so. Only ever after an archive run has verified the drive's copy."""
    with Catalog.open(settings) as catalog:
        photo = catalog.photos.by_library_path(f"2026/2026-01/{name}")
        assert photo is not None and photo.id is not None
        catalog.photos.update(replace(photo, on_mac=False))
    for path in month.glob(f"{name}*"):
        path.unlink()
    stem = Path(name).stem
    for path in month.glob(f"{stem}.raf*"):
        path.unlink()
    return photo.id


def test_a_photograph_only_on_the_drive_is_still_found(setup, drive: Path) -> None:
    settings, month = setup
    go(settings)
    take_off_the_mac(settings, month, "a.jpg")

    with Catalog.open(settings) as catalog:
        found = catalog.locate("2026/2026-01/a.jpg")
        gone = catalog.locate("2026/2026-01/nothing.jpg")
    with Catalog.open(replace(settings, archive_root=None)) as catalog:
        unplugged = catalog.locate("2026/2026-01/a.jpg")

    assert found == drive / "Photos" / "2026" / "2026-01" / "a.jpg"
    assert gone is None and unplugged is None


def test_tagging_a_photograph_only_on_the_drive_writes_its_sidecar_there(setup, drive) -> None:
    from photo_manager.tagging import set_photo_tags

    settings, month = setup
    go(settings)
    photo_id = take_off_the_mac(settings, month, "a.jpg")

    with Catalog.open(settings) as catalog:
        outcome = set_photo_tags(catalog, photo_id, ["harbour"])
        record = catalog.archive.get("2026/2026-01/a.jpg.xmp")

    on_drive = drive / "Photos" / "2026" / "2026-01" / "a.jpg.xmp"
    assert outcome.sidecars == (on_drive,)
    assert "harbour" in on_drive.read_text(encoding="utf-8")
    assert not (month / "a.jpg.xmp").exists()
    assert record is not None and record.sha256 == content_hash(on_drive)


def test_tagging_with_the_drive_unplugged_changes_nothing_and_says_why(setup, tmp_path) -> None:
    from photo_manager.tagging import TagError, set_photo_tags

    settings, month = setup
    go(settings)
    photo_id = take_off_the_mac(settings, month, "a.jpg")
    unplugged = replace(settings, archive_root=tmp_path / "Volumes" / "Gone" / "Photos")

    with Catalog.open(unplugged) as catalog:
        with pytest.raises(TagError, match="only on the archive drive"):
            set_photo_tags(catalog, photo_id, ["harbour"])
        assert catalog.photos.tags(photo_id) == []


def test_an_import_never_reuses_a_name_a_drive_only_photograph_owns(setup) -> None:
    from photo_manager import importing

    settings, month = setup
    go(settings)
    take_off_the_mac(settings, month, "a.jpg")

    taken = importing._catalogued_paths(settings)

    assert "2026/2026-01/a.jpg" in taken
    assert "2026/2026-01/b.raf" in taken


def test_a_rebuild_reads_what_is_only_on_the_drive(library_root, tmp_path, drive) -> None:
    import shutil

    import fixtures
    from photo_manager.rebuild import rebuild

    month = library_root / "2026" / "2026-01"
    month.mkdir(parents=True)
    shutil.copy2(fixtures.JPEG_WITHOUT_RAW, month / "a.jpg")
    shutil.copy2(fixtures.BURST_SECOND, month / "b.jpg")
    settings = replace(
        Settings(library_root=library_root, state_dir=tmp_path / "state"),
        archive_root=drive / "Photos",
    )
    with Catalog.open(settings) as catalog:
        for name in ("a.jpg", "b.jpg"):
            catalog.photos.add(Photo(
                content_hash=content_hash(month / name), library_path=f"2026/2026-01/{name}",
                original_name=name.upper(), captured_at=TAKEN, imported_at=TAKEN,
            ))
    go(settings)
    take_off_the_mac(settings, month, "a.jpg")

    result = rebuild(settings, tmp_path / "rebuilt.sqlite3")
    with Catalog.open(replace(settings, database_path=tmp_path / "rebuilt.sqlite3")) as rebuilt:
        rows = {photo.library_path: photo for photo in rebuilt.photos.all()}
        archived_ids = rebuilt.archive.archived_photo_ids()

    assert set(rows) == {"2026/2026-01/a.jpg", "2026/2026-01/b.jpg"}
    assert rows["2026/2026-01/a.jpg"].on_mac is False
    assert rows["2026/2026-01/b.jpg"].on_mac is True
    assert rows["2026/2026-01/a.jpg"].id in archived_ids
    assert any("only on the archive drive" in note for note in result.notes)

    from photo_manager.rebuild import RebuildError
    unplugged = replace(settings, archive_root=tmp_path / "Volumes" / "Gone" / "Photos")
    with pytest.raises(RebuildError, match="only on the archive drive"):
        rebuild(unplugged, tmp_path / "again.sqlite3")
