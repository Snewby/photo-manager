"""Cull decisions, their store, and what they add up to (WU-2.3).

The weight in this file is on two things.

**Work must survive the process.** FR-4.4 is the requirement, but the real
statement is that a person spent an hour judging 925 photographs and the app
may not lose it. So there are tests for a decision being on disk before the
call returns, for replaying a file written by something that was killed
mid-line, and for changing your mind being an append rather than a rewrite.

**A stored decision must never land on the wrong photograph.** A card photo has
no cheap identity — a hash costs 14 MB over a slow reader (C-4) — so the key is
folder, filename and size, inside a file named for the card's volume serial. A
stale ``skip`` applied to a different frame would mean a photograph silently
never imported while the UI showed it as dealt with, so there are tests for the
mismatch being *reported* rather than applied and rather than swallowed.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pytest

from photo_manager.cards import CardLocation, CardPhoto, CardScan, CardStatus, Dedup
from photo_manager import culling
from photo_manager.config import Settings
from photo_manager.culling import (
    FREE_SPACE_MARGIN,
    CullStore,
    Decision,
    PhotoKey,
    Verdict,
    card_key,
    known_serials,
    tally,
    volume_serial,
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def photo(name: str, *, size: int = 14_000_000, raw: int = 0,
          folder: str = "100_FUJI", dedup: str = Dedup.NEW) -> CardPhoto:
    root = Path("D:/DCIM") / folder
    return CardPhoto(
        stem=Path(name).stem.casefold(),
        folder=root,
        jpeg=root / name,
        raw=(root / name).with_suffix(".RAF") if raw else None,
        jpeg_bytes=size,
        raw_bytes=raw,
        dedup=dedup,
    )


def scan_of(*photos: CardPhoto) -> CardScan:
    return CardScan(
        location=CardLocation(status=CardStatus.FOUND, dcim_root=Path("D:/DCIM")),
        photos=photos,
    )


@pytest.fixture
def store(tmp_path: Path) -> CullStore:
    return CullStore(tmp_path / "cull" / "volume-deadbeef.jsonl")


# ----------------------------------------------------------------------
# The decision itself
# ----------------------------------------------------------------------


def test_a_verdict_outside_the_three_is_refused() -> None:
    with pytest.raises(ValueError, match="verdict"):
        Decision(verdict="maybe")


@pytest.mark.parametrize("rating", [-1, 6])
def test_a_rating_outside_zero_to_five_is_refused(rating: int) -> None:
    with pytest.raises(ValueError, match="rating"):
        Decision(verdict=Verdict.KEEP, rating=rating)


def test_undecided_is_the_default_and_is_not_a_decision() -> None:
    """The resume point (FR-4.4) is "no verdict yet", so it has to be a state
    of its own rather than a keep that happens to be unrated."""
    assert Decision().verdict == Verdict.UNDECIDED
    assert Decision().decided is False
    assert Decision(verdict=Verdict.KEEP).decided is True
    assert Decision(verdict=Verdict.SKIP).decided is True
    assert Decision(verdict=Verdict.SKIP).keeps is False


# ----------------------------------------------------------------------
# Which card is this?
# ----------------------------------------------------------------------


def test_two_cards_do_not_share_a_decision_file() -> None:
    assert card_key("D:/DCIM", serial="1111aaaa") != card_key("D:/DCIM", serial="2222bbbb")


def test_the_same_card_on_a_different_letter_is_the_same_card() -> None:
    """The whole point of keying on the serial rather than the path.

    WU-2.1 spent a unit on drive letters moving between readers and ports; an
    hour of culling must not evaporate because the card came up as E: today.
    """
    assert card_key("D:/DCIM", serial="1111aaaa") == card_key("E:/DCIM", serial="1111aaaa")


def test_without_a_serial_it_falls_back_to_the_path_and_says_so() -> None:
    """The safe direction to be wrong in: work repeated, never misapplied."""
    key = card_key("D:/DCIM", serial="")
    assert key.startswith("path-")
    assert card_key("D:/DCIM", serial="1111aaaa").startswith("volume-")


def test_a_card_key_is_always_a_usable_filename() -> None:
    key = card_key(r"D:\DCIM\..\weird path", serial="")
    assert set(key) <= set("abcdefghijklmnopqrstuvwxyz0123456789-_")


def test_the_real_volume_serial_is_readable_or_honestly_absent() -> None:
    serial = volume_serial(Path.cwd())
    assert serial is None or re.fullmatch(r"[0-9a-f]{8}|[0-9a-f-]{36}", serial)


# ----------------------------------------------------------------------
# Which card is this, on macOS (WU-5.1)
#
# macOS reports a FAT or exFAT volume's serial only as a UUID derived from it.
# The vectors are disk images formatted on macOS 2026-11-02, whose boot
# sectors were read directly and whose VolumeUUID diskutil reported.
# ----------------------------------------------------------------------

EXFAT_SERIAL, EXFAT_UUID = "6ae87a58", "9CF4369F-740D-314E-8918-31598FEFEDB1"
FAT32_SERIAL, FAT32_UUID = "34d91322", "B542301D-04C7-36EB-BF46-64AC9A98AE19"
#: The FAT32 image is 131,072 sectors; its boot sector says 131,070.
FAT32_PARTITION_BYTES = 131_072 * 512


@pytest.fixture
def mac(monkeypatch: pytest.MonkeyPatch):
    """macOS, with ``diskutil`` answering whatever the test says it does."""
    monkeypatch.setattr(culling.sys, "platform", "darwin")
    answer: dict = {}
    monkeypatch.setattr(culling, "_diskutil_info", lambda path: dict(answer) or None)
    return answer


def exfat(volume_uuid: str = EXFAT_UUID) -> dict:
    return {"FilesystemType": "exfat", "VolumeUUID": volume_uuid,
            "Size": 64 * 2**20, "DeviceBlockSize": 512}


def test_on_macos_a_card_culled_on_windows_keeps_its_key(mac: dict) -> None:
    """exFAT: the UUID is derived from the serial alone, so a known serial is
    proved and the card finds the decision file Windows named for it."""
    mac.update(exfat())
    assert card_key("/Volumes/Untitled/DCIM", known=["11112222", EXFAT_SERIAL]) == (
        card_key("E:/DCIM", serial=EXFAT_SERIAL)
    )


def test_on_macos_a_fat32_card_is_proved_despite_a_sector_count_nobody_reports(
    mac: dict,
) -> None:
    mac.update({"FilesystemType": "msdos", "VolumeUUID": FAT32_UUID,
                "Size": FAT32_PARTITION_BYTES, "DeviceBlockSize": 512})
    assert volume_serial("/Volumes/CARD/DCIM", known=[FAT32_SERIAL]) == FAT32_SERIAL


def test_on_macos_a_reformatted_card_is_a_different_card(mac: dict) -> None:
    """The event §5.3's guard exists for: the same name, the same mount point,
    and a new serial — so a new UUID, which proves no known serial."""
    mac.update(exfat("0429D11D-A8EA-36DC-8DA4-4B201169189B"))
    key = card_key("/Volumes/Untitled/DCIM", known=[EXFAT_SERIAL])

    assert key != card_key("E:/DCIM", serial=EXFAT_SERIAL)
    assert key.startswith("volume-")


def test_on_macos_a_new_card_is_named_by_its_uuid_wherever_it_mounts(mac: dict) -> None:
    mac.update(exfat())
    first = card_key("/Volumes/Untitled/DCIM")
    again = card_key("/Volumes/Untitled 1/DCIM")

    assert first == again == f"volume-{EXFAT_UUID.lower()}"
    assert set(first) <= set("abcdefghijklmnopqrstuvwxyz0123456789-_")


def test_on_macos_a_serial_is_not_proved_by_the_wrong_filesystem(mac: dict) -> None:
    """exFAT hashes the serial alone and FAT hashes the sector count too;
    checking one by the other's rule must not match."""
    mac.update({**exfat(), "FilesystemType": "msdos"})
    assert volume_serial("/Volumes/X/DCIM", known=[EXFAT_SERIAL]) != EXFAT_SERIAL


@pytest.mark.parametrize("answer", [
    {},                                                    # diskutil failed
    {"FilesystemType": "apfs", "VolumeUUID": EXFAT_UUID},  # not a camera card
    {"FilesystemType": "exfat", "VolumeUUID": "garbage"},
    {"FilesystemType": "exfat"},
])
def test_on_macos_anything_unprovable_falls_back_to_the_path(
    mac: dict, answer: dict
) -> None:
    mac.update(answer)
    assert card_key("/Volumes/Untitled/DCIM", known=[EXFAT_SERIAL]).startswith("path-")


def test_known_serials_are_the_cards_with_decision_files(settings: Settings) -> None:
    cull = settings.state_dir / "cull"
    cull.mkdir(parents=True)
    for name in ("volume-36ab0c1d.jsonl", "volume-0000ffff.jsonl",
                 f"volume-{EXFAT_UUID.lower()}.jsonl", "path-d-dcim.jsonl",
                 "volume-36ab0c1d.jsonl.bak"):
        (cull / name).write_text("", encoding="utf-8")

    assert known_serials(settings) == ("0000ffff", "36ab0c1d")


def test_known_serials_with_no_decisions_yet_is_empty(settings: Settings) -> None:
    assert known_serials(settings) == ()


# ----------------------------------------------------------------------
# The store
# ----------------------------------------------------------------------


def test_a_decision_is_readable_the_moment_record_returns(store: CullStore) -> None:
    """Not buffered, not batched, not written at the end.

    The file has to be complete at an arbitrary moment, because the moment it
    is needed is the one nobody chose.
    """
    store.record(PhotoKey("100_FUJI", "DSCF0007.JPG", 14_000_000),
                 Decision(verdict=Verdict.KEEP, rating=4))

    written = json.loads(store.path.read_text(encoding="utf-8").splitlines()[0])
    assert written["name"] == "DSCF0007.JPG"
    assert written["verdict"] == "keep"
    assert written["rating"] == 4


def test_a_decision_is_pushed_to_the_disk_and_not_just_to_the_operating_system(
    store: CullStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing the file would survive the process dying. This survives the
    power going.

    Worth separating, because the two are not the same promise and only one of
    them is free. Closing a file hands the bytes to the OS, which is enough for
    a crash or a kill -9 — and a test that merely reads the file back cannot
    tell the difference, which is how this one started out green while the
    fsync was deleted. The same reasoning put an fsync in the reversal log
    (FR-8.4), and a keystroke can afford a millisecond.
    """
    import os as real_os

    synced: list[int] = []
    monkeypatch.setattr(real_os, "fsync", lambda fd: synced.append(fd))

    store.record(PhotoKey("100_FUJI", "A.JPG", 1), Decision(verdict=Verdict.KEEP))
    assert synced, "the decision was left in the operating system's hands"


def test_changing_your_mind_is_another_line_and_the_last_one_wins(store: CullStore) -> None:
    """FR-4.3, and the reason the file is append-only rather than rewritten."""
    key = PhotoKey("100_FUJI", "DSCF0007.JPG", 14_000_000)
    store.record(key, Decision(verdict=Verdict.SKIP))
    store.record(key, Decision(verdict=Verdict.KEEP, rating=5))

    decisions, _ = store.replay()
    assert len(store.path.read_text(encoding="utf-8").splitlines()) == 2
    assert decisions[key].verdict == Verdict.KEEP
    assert decisions[key].rating == 5


def test_a_second_store_over_the_same_file_sees_everything(store: CullStore) -> None:
    """What "close the app and come back" actually is."""
    store.record(PhotoKey("100_FUJI", "A.JPG", 1), Decision(verdict=Verdict.KEEP))
    store.record(PhotoKey("100_FUJI", "B.JPG", 2), Decision(verdict=Verdict.SKIP))

    reopened, unreadable = CullStore(store.path).replay()
    assert len(reopened) == 2
    assert unreadable == 0


def test_a_line_cut_off_mid_write_costs_one_decision_and_not_the_file(
    store: CullStore,
) -> None:
    """The shape of a hard kill, and it must not be the shape of data loss."""
    store.record(PhotoKey("100_FUJI", "A.JPG", 1), Decision(verdict=Verdict.KEEP))
    store.record(PhotoKey("100_FUJI", "B.JPG", 2), Decision(verdict=Verdict.KEEP))
    with store.path.open("a", encoding="utf-8") as stream:
        stream.write('{"folder": "100_FUJI", "name": "C.JP')

    decisions, unreadable = store.replay()
    assert len(decisions) == 2
    assert unreadable == 1


def test_an_absent_file_is_an_empty_cull_not_an_error(tmp_path: Path) -> None:
    decisions, unreadable = CullStore(tmp_path / "nothing.jsonl").replay()
    assert decisions == {}
    assert unreadable == 0


# ----------------------------------------------------------------------
# Resuming against the card actually in the reader
# ----------------------------------------------------------------------


def test_resuming_finds_the_frames_again(store: CullStore) -> None:
    scan = scan_of(photo("DSCF0007.JPG"), photo("DSCF0008.JPG"), photo("DSCF0009.JPG"))
    store.record(PhotoKey("100_FUJI", "DSCF0007.JPG", 14_000_000),
                 Decision(verdict=Verdict.KEEP, rating=3))
    store.record(PhotoKey("100_FUJI", "DSCF0009.JPG", 14_000_000),
                 Decision(verdict=Verdict.SKIP))

    resumed = store.resume(scan)
    assert resumed.resumed == 2
    assert resumed.decisions[0].rating == 3
    assert resumed.decisions[2].verdict == Verdict.SKIP
    assert 1 not in resumed.decisions
    assert resumed.stale == ()


def test_a_decision_whose_file_has_changed_size_is_reported_not_applied(
    store: CullStore,
) -> None:
    """The failure that would lose a photograph.

    The camera's counter rolls over, so DSCF0007.JPG comes round again. If a
    stored ``skip`` were applied to the new frame it would never be imported
    and the UI would show it as dealt with — silently. So a mismatch is named.
    """
    store.record(PhotoKey("100_FUJI", "DSCF0007.JPG", 14_000_000),
                 Decision(verdict=Verdict.SKIP))
    scan = scan_of(photo("DSCF0007.JPG", size=9_000_000))  # a different frame

    resumed = store.resume(scan)
    assert resumed.decisions == {}
    assert resumed.stale == ("DSCF0007.JPG",)


def test_a_frame_deleted_in_camera_is_absent_and_that_is_ordinary(
    store: CullStore,
) -> None:
    """Absent is not stale. One is housekeeping, the other is a near miss."""
    store.record(PhotoKey("100_FUJI", "GONE.JPG", 14_000_000), Decision(verdict=Verdict.KEEP))
    resumed = store.resume(scan_of(photo("DSCF0007.JPG")))

    assert resumed.absent == 1
    assert resumed.stale == ()


def test_the_store_lands_beside_the_catalog_and_not_in_the_library(
    settings: Settings,
) -> None:
    """NFR-2: the catalog is an index rebuildable from the library. A cull
    decision is about a photograph that is not in the library, so it cannot
    live in either of them."""
    store = CullStore.for_card(settings, "volume-deadbeef")
    assert settings.state_dir in store.path.parents
    assert settings.library_root not in store.path.parents


# ----------------------------------------------------------------------
# What it adds up to (FR-4.5)
# ----------------------------------------------------------------------


def test_the_tally_counts_the_three_states(settings: Settings) -> None:
    photos = [photo("A.JPG"), photo("B.JPG"), photo("C.JPG")]
    decisions = {0: Decision(verdict=Verdict.KEEP), 1: Decision(verdict=Verdict.SKIP)}

    result = tally(photos, decisions, settings, free_bytes=10 ** 12)
    assert (result.keepers, result.skipped, result.undecided) == (1, 1, 1)


def test_only_keepers_cost_anything(settings: Settings) -> None:
    photos = [photo("A.JPG", size=10), photo("B.JPG", size=999)]
    decisions = {0: Decision(verdict=Verdict.KEEP), 1: Decision(verdict=Verdict.SKIP)}

    assert tally(photos, decisions, settings, free_bytes=10 ** 12).total_bytes == 10


def test_a_raf_is_only_counted_when_it_was_flagged(settings: Settings) -> None:
    """D-2 is opt-in per photo, so the estimate has to be too."""
    photos = [photo("A.JPG", size=10, raw=30), photo("B.JPG", size=10, raw=30)]
    decisions = {
        0: Decision(verdict=Verdict.KEEP, keep_raw=True),
        1: Decision(verdict=Verdict.KEEP, keep_raw=False),
    }

    result = tally(photos, decisions, settings, free_bytes=10 ** 12)
    assert result.raw_keepers == 1
    assert result.raw_bytes == 30
    assert result.total_bytes == 50


def test_flagging_raw_on_a_frame_with_no_raf_costs_nothing(settings: Settings) -> None:
    photos = [photo("A.JPG", size=10, raw=0)]
    decisions = {0: Decision(verdict=Verdict.KEEP, keep_raw=True)}

    assert tally(photos, decisions, settings, free_bytes=10 ** 12).raw_bytes == 0


def test_an_already_imported_frame_is_not_work_still_to_do(settings: Settings) -> None:
    """The number the user is working down has to be the real one.

    Counting withheld frames as undecided would say a thousand remain on a card
    where 925 do, and the two figures sit next to each other on the page.
    """
    photos = [photo("A.JPG"), photo("B.JPG", dedup=Dedup.IMPORTED), photo("C.JPG")]

    everything = tally(photos, {}, settings, free_bytes=10 ** 12)
    offered = tally(photos, {}, settings, offered={0, 2}, free_bytes=10 ** 12)

    assert everything.undecided == 3
    assert offered.undecided == 2


def test_the_warning_fires_before_the_volume_is_full_not_at_it(settings: Settings) -> None:
    """A disk filled to literally zero breaks everything else on the machine."""
    photos = [photo("A.JPG", size=10 ** 9)]
    decisions = {0: Decision(verdict=Verdict.KEEP)}

    fits = tally(photos, decisions, settings, free_bytes=10 ** 9 + FREE_SPACE_MARGIN + 1)
    tight = tally(photos, decisions, settings, free_bytes=10 ** 9 + FREE_SPACE_MARGIN - 1)

    assert fits.over_capacity is False
    assert tight.over_capacity is True
    # And it is over by an amount that can be put in a sentence.
    assert tight.headroom_bytes == -1


def test_free_space_is_measured_and_never_assumed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C-2: storage is being expanded separately, so any hardcoded capacity
    would be wrong within the month, and wrong silently."""
    import shutil as real_shutil

    from photo_manager import culling

    seen: list[Path] = []

    def fake_usage(path: str | Path):  # type: ignore[no-untyped-def]
        seen.append(Path(path))
        return real_shutil._ntuple_diskusage(total=100, used=40, free=60)  # type: ignore[attr-defined]

    monkeypatch.setattr(culling.shutil, "disk_usage", fake_usage)
    result = tally([photo("A.JPG")], {}, settings)

    assert seen == [settings.library_root]
    assert result.free_bytes == 60


def test_an_empty_card_tallies_to_nothing_rather_than_failing(settings: Settings) -> None:
    result = tally([], {}, settings, free_bytes=10 ** 12)
    assert (result.keepers, result.total_bytes) == (0, 0)
    assert result.over_capacity is False


def test_a_decision_time_round_trips_through_the_file(store: CullStore) -> None:
    when = dt.datetime(2026, 9, 20, 19, 30, 0)
    key = PhotoKey("100_FUJI", "A.JPG", 1)
    store.record(key, Decision(verdict=Verdict.KEEP, decided_at=when))

    decisions, _ = store.replay()
    assert decisions[key].decided_at == when
