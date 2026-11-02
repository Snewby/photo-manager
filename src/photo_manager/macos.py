"""The few things this app asks macOS directly, through ``diskutil`` (WU-5.1).

Kept apart because card identity (``culling``), card detection (``cards``) and
the read-only remount all need them, and none of those should import another
for the sake of a subprocess call. Nothing here runs on Windows; every caller
checks the platform first.

None of these write to a volume. ``remount_read_only`` changes how macOS has
the card mounted — it unmounts it and mounts it again read-only — which is the
opposite of a write: it is what stops macOS adding ``.fseventsd`` and ``._*``
files to it.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

__all__ = ["DiskutilError", "diskutil_info", "mount_point", "remount_read_only"]


class DiskutilError(RuntimeError):
    """``diskutil`` refused, with its own words in the message."""


def mount_point(path: str | Path) -> Path:
    """The root of the volume *path* is on."""
    mount = Path(path).resolve()
    while not os.path.ismount(mount) and mount != mount.parent:
        mount = mount.parent
    return mount


def diskutil_info(path: str | Path) -> dict | None:
    """``diskutil info`` for the volume *path* is on, or None."""
    try:
        result = subprocess.run(
            ["diskutil", "info", "-plist", str(mount_point(path))],
            capture_output=True, timeout=15, check=True,
        )
        return plistlib.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
        return None


def remount_read_only(path: str | Path) -> Path:
    """Unmount the volume *path* is on and mount it again, read-only.

    No password is needed for removable media. What it cannot do is undo what a
    read-write mount has already written — ``.fseventsd`` arrives within
    seconds — so it stops further writes rather than reversing earlier ones.
    Returns the new mount point, which is normally the old one.
    """
    info = diskutil_info(path)
    if not info or not info.get("DeviceIdentifier"):
        raise DiskutilError(f"diskutil cannot say which disk {path} is on.")
    device = f"/dev/{info['DeviceIdentifier']}"
    _run(["diskutil", "unmount", str(mount_point(path))])
    _run(["diskutil", "mount", "readOnly", device])
    remounted = diskutil_info(device)
    if not remounted or not remounted.get("MountPoint"):
        raise DiskutilError(f"{device} was unmounted but did not mount again.")
    return Path(remounted["MountPoint"])


def _run(command: list[str]) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DiskutilError(f"{' '.join(command)} could not run: {exc}") from exc
    if result.returncode != 0:
        words = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise DiskutilError(f"{' '.join(command[:2])} failed: {words}")
