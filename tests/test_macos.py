"""The diskutil calls, with diskutil replaced: what is asked, in what order."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from photo_manager import macos


class FakeDiskutil:
    def __init__(self, *, unmount_fails: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.unmount_fails = unmount_fails

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        verb = command[1]
        if verb == "info":
            out = plistlib.dumps({"DeviceIdentifier": "disk9s1", "MountPoint": "/Volumes/CARD"})
            return subprocess.CompletedProcess(command, 0, out if not kwargs.get("text") else out.decode(), b"")
        if verb == "unmount" and self.unmount_fails:
            return subprocess.CompletedProcess(command, 1, "", "Unmount failed: Resource busy")
        return subprocess.CompletedProcess(command, 0, "", "")


def test_a_remount_unmounts_and_mounts_the_same_disk_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDiskutil()
    monkeypatch.setattr(macos.subprocess, "run", fake)

    assert macos.remount_read_only(tmp_path / "DCIM") == Path("/Volumes/CARD")

    verbs = [call[1:3] for call in fake.calls]
    assert verbs[1] == ["unmount", str(macos.mount_point(tmp_path / "DCIM"))]
    assert verbs[2] == ["mount", "readOnly"] and fake.calls[2][3] == "/dev/disk9s1"


def test_a_refused_unmount_is_reported_in_diskutils_words_and_nothing_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDiskutil(unmount_fails=True)
    monkeypatch.setattr(macos.subprocess, "run", fake)

    with pytest.raises(macos.DiskutilError, match="Resource busy"):
        macos.remount_read_only(tmp_path)
    assert not any(call[1] == "mount" for call in fake.calls)


def test_a_disk_diskutil_cannot_name_is_not_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(macos, "diskutil_info", lambda path: None)
    monkeypatch.setattr(macos.subprocess, "run", lambda *a, **k: pytest.fail("ran diskutil"))
    with pytest.raises(macos.DiskutilError):
        macos.remount_read_only(tmp_path)
