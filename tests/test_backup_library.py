"""Tests for tools/backup_library.py's guards (WU-5.1).

The tool had no tests (EXECUTION_PLAN.md §8) and its guards were checked by
hand on Windows. Two of them turned out to be Windows assumptions: the
same-volume refusal compared ``Path.drive``, which is ``''`` for every path on
macOS and so refused *every* destination, and the free-space check measured
``destination.anchor``, which is ``/`` for every path on macOS and so reported
the internal disk's space for an external drive.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "backup_library.py"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("backup_library", _TOOL)
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: its dataclasses look their module up by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules[spec.name]


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "XT30"
    (root / "2026" / "2026-04").mkdir(parents=True)
    (root / "2026" / "2026-04" / "a.jpg").write_bytes(b"first")
    (root / "2026" / "2026-04" / "b.jpg").write_bytes(b"second")
    return root


def _external(tool, monkeypatch: pytest.MonkeyPatch, drive: Path) -> None:
    """Make everything under *drive* look like another physical volume."""
    real = tool._device

    def device(path: Path) -> int | None:
        if path == drive or drive in path.parents:
            return -1
        return real(path)

    monkeypatch.setattr(tool, "_device", device)


def test_a_destination_on_the_library_volume_is_refused_and_nothing_is_created(
    tool, library: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "backup"
    with pytest.raises(ValueError, match="same volume"):
        tool._check_target(library, destination, destination / "XT30" / "today")
    assert not destination.exists()


def test_a_destination_on_another_volume_is_accepted_without_a_drive_letter(
    tool, library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The macOS case: no path has a drive, so only the device number can say."""
    drive = tmp_path / "Volumes" / "Backup"
    drive.mkdir(parents=True)
    _external(tool, monkeypatch, drive)
    destination = drive / "PhotoManagerBackup"
    assert destination.drive == library.drive  # '' on macOS, the same letter here

    tool._check_target(library, destination, destination / "XT30" / "today")

    assert destination.is_dir()


def test_the_volume_of_a_destination_that_does_not_exist_yet_is_its_parents(
    tool, tmp_path: Path
) -> None:
    assert tool._device(tmp_path / "not" / "yet" / "made") == tmp_path.stat().st_dev


def test_a_path_with_nothing_on_it_is_reported_as_not_attached(
    tool, library: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tool, "_device", lambda path: None)
    with pytest.raises(ValueError, match="attached"):
        tool._check_target(library, tmp_path / "gone", tmp_path / "gone" / "x")


def test_a_destination_inside_the_library_is_still_refused(
    tool, library: Path
) -> None:
    destination = library / "backup"
    with pytest.raises(ValueError, match="inside the library root"):
        tool._check_target(library, destination, destination / "x")


def test_a_backup_to_another_volume_runs_and_measures_that_volume(
    tool, library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end, and the free-space question is asked of the destination."""
    drive = tmp_path / "Volumes" / "Backup"
    drive.mkdir(parents=True)
    _external(tool, monkeypatch, drive)
    asked: list[Path] = []
    real_usage = shutil.disk_usage

    def usage(path):
        asked.append(Path(path))
        return real_usage(path)

    monkeypatch.setattr(tool.shutil, "disk_usage", usage)
    config = tmp_path / "config.toml"
    config.write_text(
        f"library_root = '{library.as_posix()}'\nstate_dir = '{(tmp_path / 'state').as_posix()}'\n",
        encoding="utf-8",
    )

    assert tool.main(["--config", str(config), "--to", str(drive / "PhotoManagerBackup")]) == tool.OK

    assert asked and all(p == drive or drive in p.parents for p in asked)
    [snapshot] = (drive / "PhotoManagerBackup" / "XT30").iterdir()
    manifest = json.loads((snapshot / tool.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert sorted(f["path"] for f in manifest["files"]) == [
        "2026/2026-04/a.jpg", "2026/2026-04/b.jpg",
    ]
    assert tool._verify_only(snapshot) == tool.OK
