"""Finding the card, and reading what is on it.

Most of this file is about telling situations apart. The bug WU-2.1 exists to
fix was not that detection failed — it was that four different failures all
looked like an empty grid, so there are tests here whose only job is to assert
that two outcomes are *not* the same thing.

The rest is the two-tier dedup. The cheap tier is allowed to be wrong in one
direction and one direction only: it may offer a photo that turns out to be a
duplicate, never hide one that turns out to be new. There are tests for both
directions, because only one of them loses a photograph.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import fixtures
import pytest

from photo_manager import cards
from photo_manager.cards import (
    CardStatus,
    Dedup,
    candidate_volumes,
    confirm_imports,
    detect_card,
    removable_volumes,
    scan_card,
    summarise,
)
from photo_manager.catalog import Catalog
from photo_manager.config import Settings
from photo_manager.migration import directory_snapshot
from photo_manager.models import Photo


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def make_volume(root: Path, *folders: str) -> Path:
    """A volume with a DCIM tree. Returns the volume root, not the DCIM."""
    for folder in folders:
        (root / "DCIM" / folder).mkdir(parents=True)
    return root


def put(folder: Path, name: str, fixture: Path | None = None, size: int = 1024) -> Path:
    """Put a file on the card. A real photo when a fixture is named."""
    target = folder / name
    if fixture is not None:
        shutil.copy2(fixture, target)
    else:
        target.write_bytes(b"\xff\xd8" + b"x" * size)
    return target


def card_settings(library_root: Path, tmp_path: Path, **kwargs: object) -> Settings:
    return Settings(library_root=library_root, state_dir=tmp_path / "state", **kwargs)


@pytest.fixture
def card(tmp_path: Path) -> Path:
    """A synthetic card volume with one image folder. Returns the folder."""
    volume = make_volume(tmp_path / "card", "100_FUJI")
    return volume / "DCIM" / "100_FUJI"


@pytest.fixture
def volume(card: Path) -> Path:
    return card.parent.parent


# ----------------------------------------------------------------------
# Finding the card — the §8 fix
# ----------------------------------------------------------------------


def test_the_card_is_found_by_probing_not_by_a_drive_letter(
    library_root: Path, tmp_path: Path, card: Path, volume: Path
) -> None:
    """Nothing is configured. The card is found because it is there."""
    settings = card_settings(library_root, tmp_path)

    location = detect_card(settings, volumes=[volume])

    assert location.found
    assert location.dcim_root == volume / "DCIM"
    assert [f.name for f in location.image_folders] == ["100_FUJI"]


def test_no_media_and_media_without_photos_are_different_things(
    library_root: Path, tmp_path: Path
) -> None:
    """The whole point of the unit. Both used to be an empty list, and an
    empty list rendered as an empty grid."""
    settings = card_settings(library_root, tmp_path)
    stick = tmp_path / "usb-stick"
    stick.mkdir()

    nothing = detect_card(settings, volumes=[])
    not_a_card = detect_card(settings, volumes=[stick])

    assert nothing.status == CardStatus.NO_MEDIA
    assert not_a_card.status == CardStatus.NO_PHOTOS_ON_MEDIA
    assert nothing.status != not_a_card.status
    assert not nothing.found and not not_a_card.found


def test_a_wrong_configured_path_says_where_the_card_actually_is(
    library_root: Path, tmp_path: Path, card: Path, volume: Path
) -> None:
    """"Configured D: but the card is on E:" is the failure that prompted all
    of this, so the message has to name the alternative."""
    settings = card_settings(library_root, tmp_path, card_dcim_root=tmp_path / "gone" / "DCIM")

    location = detect_card(settings, volumes=[volume])

    assert location.status == CardStatus.CONFIGURED_PATH_MISSING
    assert location.elsewhere == (volume / "DCIM",)
    assert str(volume / "DCIM") in location.message


def test_a_configured_path_that_exists_but_holds_no_photos(
    library_root: Path, tmp_path: Path
) -> None:
    dcim = tmp_path / "empty" / "DCIM"
    dcim.mkdir(parents=True)
    settings = card_settings(library_root, tmp_path, card_dcim_root=dcim)

    location = detect_card(settings, volumes=[])

    assert location.status == CardStatus.CONFIGURED_PATH_EMPTY
    assert location.dcim_root == dcim
    assert not location.found


def test_an_explicit_setting_is_honoured_over_a_probe(
    library_root: Path, tmp_path: Path, card: Path, volume: Path
) -> None:
    """A setting the user wrote down is not quietly second-guessed."""
    other = make_volume(tmp_path / "other", "101_FUJI")
    settings = card_settings(library_root, tmp_path, card_dcim_root=other / "DCIM")

    location = detect_card(settings, volumes=[volume, other])

    assert location.found
    assert location.dcim_root == other / "DCIM"
    assert location.elsewhere == (volume / "DCIM",)


def test_two_cards_are_reported_rather_than_one_being_swallowed(
    library_root: Path, tmp_path: Path, card: Path, volume: Path
) -> None:
    second = make_volume(tmp_path / "second", "100_FUJI")
    settings = card_settings(library_root, tmp_path)

    location = detect_card(settings, volumes=[volume, second])

    assert location.found
    assert location.dcim_root == volume / "DCIM"
    assert location.elsewhere == (second / "DCIM",)
    assert "second" in location.message


def test_image_folders_are_matched_by_glob_because_the_number_rolls_over(
    library_root: Path, tmp_path: Path
) -> None:
    volume = make_volume(tmp_path / "card", "100_FUJI", "101_FUJI", "MISC", "999_OTHER")
    settings = card_settings(library_root, tmp_path)

    location = detect_card(settings, volumes=[volume])

    assert [f.name for f in location.image_folders] == ["100_FUJI", "101_FUJI"]


def test_a_custom_glob_is_honoured(library_root: Path, tmp_path: Path) -> None:
    volume = make_volume(tmp_path / "card", "200_FUJI")
    settings = card_settings(library_root, tmp_path, card_folder_glob="2??_FUJI")

    assert detect_card(settings, volumes=[volume]).found


@pytest.mark.parametrize(
    "status",
    [
        CardStatus.NO_MEDIA,
        CardStatus.NO_PHOTOS_ON_MEDIA,
        CardStatus.CONFIGURED_PATH_MISSING,
        CardStatus.CONFIGURED_PATH_EMPTY,
    ],
)
def test_every_failure_carries_a_sentence_a_human_can_act_on(
    library_root: Path, tmp_path: Path, status: str
) -> None:
    """An unexplained failure is the empty grid again, wearing a status code."""
    stick = tmp_path / "stick"
    stick.mkdir()
    empty_dcim = tmp_path / "empty" / "DCIM"
    empty_dcim.mkdir(parents=True)

    cases = {
        CardStatus.NO_MEDIA: (card_settings(library_root, tmp_path), []),
        CardStatus.NO_PHOTOS_ON_MEDIA: (card_settings(library_root, tmp_path), [stick]),
        CardStatus.CONFIGURED_PATH_MISSING: (
            card_settings(library_root, tmp_path, card_dcim_root=tmp_path / "nope"), []),
        CardStatus.CONFIGURED_PATH_EMPTY: (
            card_settings(library_root, tmp_path, card_dcim_root=empty_dcim), []),
    }
    settings, volumes = cases[status]
    location = detect_card(settings, volumes=volumes)

    assert location.status == status
    assert len(location.message) > 20
    assert location.message.strip().endswith((".", "!"))


def test_an_unreadable_volume_is_not_the_card_rather_than_a_crash(
    library_root: Path, tmp_path: Path
) -> None:
    settings = card_settings(library_root, tmp_path)
    assert detect_card(settings, volumes=[tmp_path / "never-existed"]).status == (
        CardStatus.NO_PHOTOS_ON_MEDIA
    )


def test_volume_probing_runs_without_raising_on_this_machine() -> None:
    """The ctypes path is Windows-only and easy to get subtly wrong."""
    assert isinstance(removable_volumes(), tuple)
    assert isinstance(candidate_volumes(), tuple)


# ----------------------------------------------------------------------
# Finding it on macOS (WU-5.1)
# ----------------------------------------------------------------------


@pytest.fixture
def mac_volumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for /Volumes, with the platform set to macOS."""
    root = tmp_path / "Volumes"
    root.mkdir()
    monkeypatch.setattr(cards.sys, "platform", "darwin")
    monkeypatch.setattr(cards, "_MAC_VOLUMES", root)
    # The temporary directory is on the startup disk itself, which APFS gives
    # the same st_dev as /, so the real rule would exclude every test volume.
    monkeypatch.setattr(cards, "_startup_device", lambda: -1)
    return root


def test_on_macos_every_mounted_volume_is_probed_and_the_startup_disk_is_not(
    mac_volumes: Path,
) -> None:
    make_volume(mac_volumes / "Untitled", "100_FUJI")
    (mac_volumes / "Backup").mkdir()
    (mac_volumes / ".timemachine").mkdir()
    (mac_volumes / "stray-file").write_text("", encoding="utf-8")
    try:
        (mac_volumes / "Macintosh HD").symlink_to("/", target_is_directory=True)
    except OSError:
        pass  # Windows without the symlink privilege; the rest still holds.

    assert [p.name for p in candidate_volumes()] == ["Backup", "Untitled"]


def test_on_macos_a_volume_on_the_startup_disk_is_never_probed(
    mac_volumes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_volume(mac_volumes / "Untitled", "100_FUJI")
    monkeypatch.setattr(cards, "_startup_device", lambda: mac_volumes.stat().st_dev)
    assert candidate_volumes() == ()


def test_on_macos_the_card_is_found_under_volumes_without_being_configured(
    library_root: Path, tmp_path: Path, mac_volumes: Path
) -> None:
    make_volume(mac_volumes / "Backup")
    card_root = make_volume(mac_volumes / "Untitled", "100_FUJI")

    location = detect_card(card_settings(library_root, tmp_path))

    assert location.found
    assert location.dcim_root == card_root / "DCIM"


def test_on_macos_nothing_mounted_is_no_media(
    library_root: Path, tmp_path: Path, mac_volumes: Path
) -> None:
    location = detect_card(card_settings(library_root, tmp_path))
    assert location.status == CardStatus.NO_MEDIA


def _mounted(read_only: bool):
    class Result:
        f_flag = os.ST_RDONLY if read_only else 0
    return lambda path: Result()


@pytest.mark.skipif(not hasattr(os, "ST_RDONLY"), reason="no statvfs flags here")
def test_on_macos_a_card_mounted_read_write_is_said_to_be(
    library_root: Path, tmp_path: Path, mac_volumes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS writes to a FAT/exFAT volume it mounts read-write (.fseventsd at
    once), so the card is only read-only if it was mounted that way."""
    make_volume(mac_volumes / "Untitled", "100_FUJI")
    settings = card_settings(library_root, tmp_path)

    monkeypatch.setattr(cards.os, "statvfs", _mounted(read_only=False))
    writable = detect_card(settings)
    monkeypatch.setattr(cards.os, "statvfs", _mounted(read_only=True))
    read_only = detect_card(settings)

    assert writable.found and read_only.found
    assert writable.writable is True and read_only.writable is False
    assert "read-write" in writable.message
    assert "read-write" not in read_only.message
    assert writable.message.endswith(".")


def test_the_mount_warning_is_macos_only(
    library_root: Path, tmp_path: Path, card: Path, volume: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cards.sys, "platform", "win32")
    location = detect_card(card_settings(library_root, tmp_path), volumes=[volume])
    assert "read-write" not in location.message
    assert location.writable is None


def test_on_macos_remounting_asks_diskutil_and_says_what_went_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from photo_manager import macos
    calls: list[str] = []
    monkeypatch.setattr(cards.sys, "platform", "darwin")
    monkeypatch.setattr(macos, "remount_read_only", lambda path: calls.append(str(path)))
    cards.remount_read_only(tmp_path / "DCIM")
    assert calls == [str(tmp_path / "DCIM")]

    def refuses(path):
        raise macos.DiskutilError("diskutil unmount failed: Resource busy")

    monkeypatch.setattr(macos, "remount_read_only", refuses)
    with pytest.raises(cards.CardError, match="Resource busy.*lock switch"):
        cards.remount_read_only(tmp_path / "DCIM")


def test_remounting_is_refused_off_macos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cards.sys, "platform", "win32")
    with pytest.raises(cards.CardError, match="macOS"):
        cards.remount_read_only(tmp_path)


# ----------------------------------------------------------------------
# Reading the card
# ----------------------------------------------------------------------


def test_raf_and_jpeg_pair_by_basename(
    library_root: Path, tmp_path: Path, card: Path, volume: Path
) -> None:
    put(card, "DSCF0007.JPG")
    put(card, "DSCF0007.RAF")
    settings = card_settings(library_root, tmp_path)

    scan = scan_card(settings, location=detect_card(settings, volumes=[volume]))

    assert scan.photo_count == 1
    assert scan.pair_count == 1
    photo = scan.photos[0]
    assert photo.jpeg is not None and photo.raw is not None
    assert photo.has_raw and not photo.unpaired


def test_a_jpeg_with_no_raf_is_an_ordinary_photo(
    library_root: Path, tmp_path: Path, card: Path, volume: Path
) -> None:
    put(card, "DSCF0008.JPG")
    settings = card_settings(library_root, tmp_path)

    scan = scan_card(settings, location=detect_card(settings, volumes=[volume]))

    assert scan.photo_count == 1
    assert scan.pair_count == 0
    assert scan.photos[0].unpaired
    assert scan.photos[0].primary.name == "DSCF0008.JPG"


def test_a_raf_with_no_jpeg_still_appears(
    library_root: Path, tmp_path: Path, card: Path, volume: Path
) -> None:
    """Unusual, but a frame that is invisible to the grid is worse."""
    put(card, "DSCF0009.RAF")
    settings = card_settings(library_root, tmp_path)

    scan = scan_card(settings, location=detect_card(settings, volumes=[volume]))

    assert scan.photo_count == 1
    assert scan.photos[0].jpeg is None
    assert scan.photos[0].primary.name == "DSCF0009.RAF"
    assert scan.unpaired == scan.photos


def test_non_image_files_are_reported_not_silently_dropped(
    library_root: Path, tmp_path: Path, card: Path, volume: Path
) -> None:
    put(card, "DSCF0010.JPG")
    (card / "notes.txt").write_text("hello", encoding="utf-8")
    settings = card_settings(library_root, tmp_path)

    scan = scan_card(settings, location=detect_card(settings, volumes=[volume]))

    assert scan.photo_count == 1
    assert [p.name for p in scan.ignored] == ["notes.txt"]


def test_appledouble_files_are_not_photographs(
    library_root: Path, tmp_path: Path, card: Path, volume: Path
) -> None:
    """``._DSCF0010.JPG`` is macOS metadata with a JPEG's suffix. Paired by
    suffix it would be a second, unreadable photograph; it is reported instead,
    like any other file that is not one."""
    put(card, "DSCF0010.JPG")
    put(card, "._DSCF0010.JPG", size=4000)
    put(card, "._DSCF0011.RAF", size=4000)
    settings = card_settings(library_root, tmp_path)

    scan = scan_card(settings, location=detect_card(settings, volumes=[volume]))

    assert [p.primary.name for p in scan.photos] == ["DSCF0010.JPG"]
    assert scan.photos[0].raw is None
    assert sorted(p.name for p in scan.ignored) == ["._DSCF0010.JPG", "._DSCF0011.RAF"]


def test_several_image_folders_are_all_scanned(
    library_root: Path, tmp_path: Path
) -> None:
    volume = make_volume(tmp_path / "card", "100_FUJI", "101_FUJI")
    put(volume / "DCIM" / "100_FUJI", "DSCF0001.JPG")
    put(volume / "DCIM" / "101_FUJI", "DSCF0002.JPG")
    settings = card_settings(library_root, tmp_path)

    scan = scan_card(settings, location=detect_card(settings, volumes=[volume]))

    assert scan.photo_count == 2


def test_sizes_come_back_without_opening_anything(
    library_root: Path, tmp_path: Path, card: Path, volume: Path
) -> None:
    """FR-4.5 needs these to estimate what an import will cost."""
    put(card, "DSCF0011.JPG", size=5000)
    put(card, "DSCF0011.RAF", size=9000)
    settings = card_settings(library_root, tmp_path)

    photo = scan_card(settings, location=detect_card(settings, volumes=[volume])).photos[0]

    assert photo.jpeg_bytes == 5002  # the 2-byte JPEG magic, plus the padding
    assert photo.raw_bytes == 9002
    assert photo.total_bytes == photo.jpeg_bytes + photo.raw_bytes


def test_scanning_an_absent_card_yields_an_empty_scan_that_says_why(
    library_root: Path, tmp_path: Path
) -> None:
    settings = card_settings(library_root, tmp_path)

    scan = scan_card(settings, location=detect_card(settings, volumes=[]))

    assert scan.photos == ()
    assert scan.location.status == CardStatus.NO_MEDIA
    assert CardStatus.NO_MEDIA in summarise(scan)


# ----------------------------------------------------------------------
# Dedup (FR-1.4, C-3)
# ----------------------------------------------------------------------


@pytest.fixture
def populated(settings: Settings) -> Iterator[Catalog]:
    """A catalog holding one photo, DSCF0007.JPG, with known bytes."""
    with Catalog.open(settings) as catalog:
        catalog.photos.add(Photo(
            content_hash="a" * 64,
            library_path="2026/2026-09-06/DSCF0007.JPG",
            original_name="DSCF0007.JPG",
            captured_at=dt.datetime(2026, 9, 6, 11, 0, 0),
            imported_at=dt.datetime(2026, 9, 7, 9, 0, 0),
        ))
        yield catalog


def test_an_empty_catalog_makes_everything_new(
    library_root: Path, tmp_path: Path, card: Path, volume: Path, catalog: Catalog
) -> None:
    put(card, "DSCF0007.JPG")
    settings = card_settings(library_root, tmp_path)

    scan = scan_card(settings, catalog=catalog,
                     location=detect_card(settings, volumes=[volume]))

    assert scan.photos[0].dedup == Dedup.NEW
    assert scan.to_cull == scan.photos


def test_a_known_filename_is_likely_imported_without_reading_a_byte(
    tmp_path: Path, populated: Catalog
) -> None:
    volume = make_volume(tmp_path / "card", "100_FUJI")
    card = volume / "DCIM" / "100_FUJI"
    put(card, "DSCF0007.JPG")
    put(card, "DSCF0099.JPG")
    settings = populated.settings

    scan = scan_card(settings, catalog=populated,
                     location=detect_card(settings, volumes=[volume]))

    states = {p.primary.name: p.dedup for p in scan.photos}
    assert states == {
        "DSCF0007.JPG": Dedup.LIKELY_IMPORTED,
        "DSCF0099.JPG": Dedup.NEW,
    }
    assert scan.photos[0].content_hash is None  # nothing was hashed


def test_filename_matching_ignores_case(tmp_path: Path, populated: Catalog) -> None:
    volume = make_volume(tmp_path / "card", "100_FUJI")
    put(volume / "DCIM" / "100_FUJI", "dscf0007.jpg")
    settings = populated.settings

    scan = scan_card(settings, catalog=populated,
                     location=detect_card(settings, volumes=[volume]))

    assert scan.photos[0].dedup == Dedup.LIKELY_IMPORTED


def test_confirming_upgrades_a_real_duplicate_to_imported(
    tmp_path: Path, settings: Settings
) -> None:
    volume = make_volume(tmp_path / "card", "100_FUJI")
    card = volume / "DCIM" / "100_FUJI"
    source = put(card, "DSCF0007.JPG", fixture=fixtures.JPEG_WITHOUT_RAW)

    from photo_manager.hashing import content_hash
    with Catalog.open(settings) as catalog:
        catalog.photos.add(Photo(
            content_hash=content_hash(source),
            library_path="2026/2026-09-06/DSCF0007.JPG",
            original_name="DSCF0007.JPG",
            captured_at=dt.datetime(2026, 9, 6, 11, 0, 0),
            imported_at=dt.datetime(2026, 9, 7, 9, 0, 0),
        ))
        location = detect_card(settings, volumes=[volume])
        scan = scan_card(settings, catalog=catalog, location=location)
        assert scan.photos[0].dedup == Dedup.LIKELY_IMPORTED

        scan = confirm_imports(scan, catalog)

    assert scan.photos[0].dedup == Dedup.IMPORTED
    assert scan.to_cull == ()


def test_confirming_rescues_a_different_photo_that_shares_a_filename(
    tmp_path: Path, populated: Catalog
) -> None:
    """The direction that loses a photograph if it goes wrong.

    The camera's counter rolls over, so a reformatted card can present a
    DSCF0007.JPG that is not the DSCF0007.JPG already in the library. The cheap
    tier calls it a duplicate; the hash has to rescue it, or it is silently
    never imported.
    """
    volume = make_volume(tmp_path / "card", "100_FUJI")
    card = volume / "DCIM" / "100_FUJI"
    put(card, "DSCF0007.JPG", fixture=fixtures.JPEG_WITHOUT_RAW)
    settings = populated.settings

    location = detect_card(settings, volumes=[volume])
    scan = scan_card(settings, catalog=populated, location=location)
    assert scan.photos[0].dedup == Dedup.LIKELY_IMPORTED
    assert scan.to_cull == ()  # withheld on the cheap verdict

    scan = confirm_imports(scan, populated)

    assert scan.photos[0].dedup == Dedup.NOT_IMPORTED
    assert scan.to_cull == scan.photos  # and handed back for culling


def test_confirming_reads_only_the_candidates(
    tmp_path: Path, populated: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason the scan is instant. Hashing everything is 7 minutes on this
    reader; hashing the candidates is proportional to what is already imported."""
    volume = make_volume(tmp_path / "card", "100_FUJI")
    card = volume / "DCIM" / "100_FUJI"
    put(card, "DSCF0007.JPG", fixture=fixtures.JPEG_WITHOUT_RAW)
    for name in ("DSCF0100.JPG", "DSCF0101.JPG", "DSCF0102.JPG"):
        put(card, name, fixture=fixtures.JPEG_WITHOUT_RAW)
    settings = populated.settings

    hashed: list[str] = []
    import photo_manager.cards as cards_module
    real = cards_module.content_hash
    monkeypatch.setattr(
        cards_module, "content_hash",
        lambda path, **kw: (hashed.append(Path(path).name), real(path, **kw))[1],
    )

    location = detect_card(settings, volumes=[volume])
    scan = scan_card(settings, catalog=populated, location=location)
    confirm_imports(scan, populated)

    assert hashed == ["DSCF0007.JPG"]


def test_confirming_is_a_no_op_when_nothing_is_a_candidate(
    library_root: Path, tmp_path: Path, card: Path, volume: Path, catalog: Catalog
) -> None:
    put(card, "DSCF0500.JPG")
    settings = card_settings(library_root, tmp_path)
    location = detect_card(settings, volumes=[volume])
    scan = scan_card(settings, catalog=catalog, location=location)

    assert confirm_imports(scan, catalog) is scan


def test_an_unreadable_candidate_is_offered_rather_than_hidden(
    tmp_path: Path, populated: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frame we cannot read is not a frame we can call a duplicate."""
    volume = make_volume(tmp_path / "card", "100_FUJI")
    put(volume / "DCIM" / "100_FUJI", "DSCF0007.JPG")
    settings = populated.settings

    import photo_manager.cards as cards_module
    monkeypatch.setattr(cards_module, "content_hash",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("device error")))

    location = detect_card(settings, volumes=[volume])
    scan = confirm_imports(scan_card(settings, catalog=populated, location=location),
                           populated)

    assert scan.photos[0].dedup == Dedup.NOT_IMPORTED
    assert scan.to_cull == scan.photos


def test_the_pair_of_an_imported_jpeg_is_not_offered_again(
    tmp_path: Path, settings: Settings
) -> None:
    """Dedup is keyed on the JPEG, so its RAF rides along and does not come
    back as a separate frame to cull."""
    volume = make_volume(tmp_path / "card", "100_FUJI")
    card = volume / "DCIM" / "100_FUJI"
    source = put(card, "DSCF0007.JPG", fixture=fixtures.JPEG_WITHOUT_RAW)
    put(card, "DSCF0007.RAF", fixture=fixtures.RAW_PAIR_RAF)

    from photo_manager.hashing import content_hash
    with Catalog.open(settings) as catalog:
        catalog.photos.add(Photo(
            content_hash=content_hash(source),
            library_path="2026/2026-09-06/DSCF0007.JPG",
            original_name="DSCF0007.JPG",
            captured_at=dt.datetime(2026, 9, 6, 11, 0, 0),
            imported_at=dt.datetime(2026, 9, 7, 9, 0, 0),
        ))
        location = detect_card(settings, volumes=[volume])
        scan = confirm_imports(
            scan_card(settings, catalog=catalog, location=location), catalog
        )

    assert scan.photo_count == 1
    assert scan.to_cull == ()


# ----------------------------------------------------------------------
# The card is read-only (§4.1, NFR-3)
# ----------------------------------------------------------------------


def test_scanning_writes_nothing_to_the_card(
    library_root: Path, tmp_path: Path, card: Path, volume: Path, catalog: Catalog
) -> None:
    put(card, "DSCF0007.JPG", fixture=fixtures.JPEG_WITHOUT_RAW)
    put(card, "DSCF0007.RAF", fixture=fixtures.RAW_PAIR_RAF)
    (card / "notes.txt").write_text("x", encoding="utf-8")
    settings = card_settings(library_root, tmp_path)

    before = directory_snapshot(volume)
    scan_card(settings, catalog=catalog, location=detect_card(settings, volumes=[volume]))

    assert directory_snapshot(volume) == before


def test_confirming_writes_nothing_to_the_card(
    tmp_path: Path, populated: Catalog
) -> None:
    volume = make_volume(tmp_path / "card", "100_FUJI")
    put(volume / "DCIM" / "100_FUJI", "DSCF0007.JPG", fixture=fixtures.JPEG_WITHOUT_RAW)
    settings = populated.settings
    location = detect_card(settings, volumes=[volume])
    scan = scan_card(settings, catalog=populated, location=location)

    before = directory_snapshot(volume)
    confirm_imports(scan, populated)

    assert directory_snapshot(volume) == before


def test_detection_writes_nothing_even_when_it_finds_nothing(
    library_root: Path, tmp_path: Path
) -> None:
    """Probing must not create the DCIM folder it was looking for."""
    stick = tmp_path / "stick"
    stick.mkdir()
    settings = card_settings(library_root, tmp_path)

    before = directory_snapshot(stick)
    detect_card(settings, volumes=[stick])

    assert directory_snapshot(stick) == before
    assert not (stick / "DCIM").exists()


# ----------------------------------------------------------------------
# The real card
# ----------------------------------------------------------------------


@pytest.fixture
def real_card(machine_settings: Settings) -> Settings:
    """The configured machine with a card actually inserted, or a skip."""
    settings = machine_settings
    if not detect_card(settings).found:
        pytest.skip("no card inserted")
    return settings


@pytest.mark.needs_card
def test_the_real_card_scans_correctly_and_is_left_untouched(
    real_card: Settings,
) -> None:
    """WU-2.1's acceptance criteria, against the card in the reader."""
    location = detect_card(real_card)
    assert location.found
    assert location.dcim_root is not None

    before = directory_snapshot(location.dcim_root)
    scan = scan_card(real_card, location=location)
    assert directory_snapshot(location.dcim_root) == before, "the scan wrote to the card"

    assert scan.photo_count > 0
    assert scan.pair_count <= scan.photo_count
    assert all(p.dedup == Dedup.NEW for p in scan.photos)  # catalog is empty


@pytest.mark.needs_card
def test_the_real_card_is_found_without_being_told_where_it_is(
    real_card: Settings,
) -> None:
    """The §8 fix, on the machine the bug was going to bite."""
    blind = Settings(
        library_root=real_card.library_root,
        state_dir=real_card.state_dir,
        card_dcim_root=None,
    )
    location = detect_card(blind)

    assert location.found
    assert location.dcim_root is not None
    assert location.dcim_root.name == "DCIM"


@pytest.mark.needs_card
def test_a_wrong_drive_letter_on_the_real_machine_is_not_an_empty_grid(
    real_card: Settings,
) -> None:
    wrong = Settings(
        library_root=real_card.library_root,
        state_dir=real_card.state_dir,
        card_dcim_root=Path("Q:/DCIM"),
    )
    location = detect_card(wrong)

    assert location.status == CardStatus.CONFIGURED_PATH_MISSING
    assert location.elsewhere  # and it names the real one
