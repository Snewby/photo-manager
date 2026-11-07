"""Free up Mac space: the rule, the last-moment check, the Trash, and back.

The Trash is faked: a function that unlinks and remembers, so no test ever
touches the real one.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

import pytest

from photo_manager import backup
from photo_manager.archive import run_archive
from photo_manager.catalog import Catalog
from photo_manager.config import Settings
from photo_manager.hashing import content_hash
from photo_manager.models import Photo
from photo_manager.offload import (
    OffloadError, execute_offload, plan_offload, restore_to_mac,
)

TODAY = dt.date(2026, 11, 7)
OLD = dt.datetime(2026, 1, 10, 9, 0, 0)
RECENT = dt.datetime(2026, 11, 1, 9, 0, 0)


@pytest.fixture
def drive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "Volumes" / "My Passport"
    root.mkdir(parents=True)
    real = backup.device
    monkeypatch.setattr(
        backup, "device",
        lambda path: -1 if path == root or root in path.parents else real(path),
    )
    return root


class FakeTrash:
    def __init__(self) -> None:
        self.moved: list[str] = []

    def __call__(self, path: str) -> None:
        Path(path).unlink()
        self.moved.append(Path(path).name)


@pytest.fixture
def setup(library_root: Path, tmp_path: Path, drive: Path):
    """Four photographs, one per reason the rule can give, all archived:

    * ``old.jpg`` — old, unrated: the one the rule lets go (with a RAF and a sidecar)
    * ``new.jpg`` — taken last week: kept as recent
    * ``best.jpg`` — old but rated 5: kept as rated
    * ``fresh.jpg`` — old and unrated but never archived: not removable at all
    """
    month = library_root / "2026" / "2026-01"
    month.mkdir(parents=True)
    settings = replace(
        Settings(library_root=library_root, state_dir=tmp_path / "state"),
        archive_root=drive / "Photos",
    )
    specs = {
        "old.jpg": dict(captured_at=OLD, raw=True),
        "new.jpg": dict(captured_at=RECENT),
        "best.jpg": dict(captured_at=OLD, rating=5),
        "fresh.jpg": dict(captured_at=OLD),
    }
    ids: dict[str, int] = {}
    with Catalog.open(settings) as catalog:
        for name, spec in specs.items():
            (month / name).write_bytes(f"photograph {name}".encode())
            raw = None
            if spec.get("raw"):
                (month / "old.raf").write_bytes(b"raw old")
                raw = "2026/2026-01/old.raf"
            row = catalog.photos.add(Photo(
                content_hash=content_hash(month / name), library_path=f"2026/2026-01/{name}",
                original_name=name.upper(), captured_at=spec["captured_at"],
                imported_at=OLD, raw_path=raw, rating=spec.get("rating", 0),
            ))
            ids[name] = row.id
        (month / "old.jpg.xmp").write_text("<x>harbour</x>", encoding="utf-8")
        # Archive everything except fresh.jpg.
        fresh = catalog.photos.get(ids["fresh.jpg"])
        catalog.photos.update(replace(fresh, on_mac=False))
        run_archive(settings, catalog, snapshot=False)
        catalog.photos.update(replace(fresh, on_mac=True))
    return settings, month, ids


def plan(settings: Settings):
    with Catalog.open(settings) as catalog:
        return plan_offload(settings, catalog, today=TODAY)


def test_the_rule_keeps_recent_rated_and_unarchived_photographs(setup) -> None:
    settings, _, ids = setup

    result = plan(settings)

    assert [c.photo_id for c in result.candidates] == [ids["old.jpg"]]
    assert result.candidates[0].files == (
        "2026/2026-01/old.jpg", "2026/2026-01/old.raf", "2026/2026-01/old.jpg.xmp",
    )
    assert (result.on_mac, result.kept_recent, result.kept_rated, result.not_archived) == (4, 1, 1, 1)
    assert result.bytes == result.candidates[0].bytes > 0


def test_a_pin_keeps_and_a_mark_removes_whatever_the_rule_says(setup) -> None:
    settings, _, ids = setup
    with Catalog.open(settings) as catalog:
        old, new = catalog.photos.get(ids["old.jpg"]), catalog.photos.get(ids["new.jpg"])
        catalog.photos.update(replace(old, keep_on_mac=True))
        catalog.photos.update(replace(new, keep_on_mac=False))

    result = plan(settings)

    assert [c.photo_id for c in result.candidates] == [ids["new.jpg"]]
    assert result.candidates[0].marked is True
    assert result.kept_pinned == 1


def test_removing_trashes_every_file_and_the_drive_keeps_them(setup, drive: Path) -> None:
    settings, month, ids = setup
    trash = FakeTrash()

    with Catalog.open(settings) as catalog:
        result = execute_offload(settings, catalog, plan(settings), trash=trash)
        photo = catalog.photos.get(ids["old.jpg"])

    assert result.ok and result.removed == 1 and result.bytes_freed > 0
    assert trash.moved[-1] == "old.jpg"  # the JPEG goes last
    assert sorted(trash.moved) == ["old.jpg", "old.jpg.xmp", "old.raf"]
    assert not any(month.glob("old.*"))
    assert (drive / "Photos" / "2026" / "2026-01" / "old.jpg").is_file()
    assert photo.on_mac is False
    [line] = result.log.read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["photo_id"] == ids["old.jpg"]


def test_a_drive_copy_that_does_not_match_leaves_the_photograph_on_the_mac(setup, drive) -> None:
    settings, month, ids = setup
    (drive / "Photos" / "2026" / "2026-01" / "old.raf").write_bytes(b"decayed")
    trash = FakeTrash()

    with Catalog.open(settings) as catalog:
        result = execute_offload(settings, catalog, plan(settings), trash=trash)
        photo = catalog.photos.get(ids["old.jpg"])

    assert not result.ok and "does not match" in result.failures[0]
    assert trash.moved == [] and (month / "old.jpg").is_file()
    assert photo.on_mac is True


def test_a_sidecar_changed_since_archiving_is_archived_again_first(setup, drive) -> None:
    settings, month, _ = setup
    (month / "old.jpg.xmp").write_text("<x>harbour, Sam</x>", encoding="utf-8")

    with Catalog.open(settings) as catalog:
        result = execute_offload(settings, catalog, plan(settings), trash=FakeTrash())

    assert result.ok and result.sidecars_refreshed == 1
    assert (drive / "Photos" / "2026" / "2026-01" / "old.jpg.xmp").read_text() \
        == "<x>harbour, Sam</x>"


def test_nothing_is_removed_without_the_drive(setup, tmp_path: Path) -> None:
    settings, month, _ = setup
    to_do = plan(settings)
    unplugged = replace(settings, archive_root=tmp_path / "Volumes" / "Gone" / "Photos")

    with Catalog.open(unplugged) as catalog:
        with pytest.raises(OffloadError, match="Nothing was removed"):
            execute_offload(unplugged, catalog, to_do, trash=FakeTrash())
    assert (month / "old.jpg").is_file()


def test_bringing_back_copies_verifies_and_pins(setup) -> None:
    settings, month, ids = setup
    with Catalog.open(settings) as catalog:
        execute_offload(settings, catalog, plan(settings), trash=FakeTrash())
        result = restore_to_mac(settings, catalog, [ids["old.jpg"]])
        photo = catalog.photos.get(ids["old.jpg"])

    assert result.ok and result.restored == 1
    assert (month / "old.jpg").read_bytes() == b"photograph old.jpg"
    assert (month / "old.raf").read_bytes() == b"raw old"
    assert (month / "old.jpg.xmp").is_file()
    assert photo.on_mac is True and photo.keep_on_mac is True
    assert plan(settings).candidates == ()
