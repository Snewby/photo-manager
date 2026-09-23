"""Re-filing day folders into month folders (D-4, 2026-09-23).

Every test here builds a throwaway library of plain byte files with catalog rows
pointing at them: what is under test is where files go and that nothing is
lost on the way, not what is inside them.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest

from photo_manager import refile
from photo_manager.catalog import Catalog
from photo_manager.hashing import content_hash
from photo_manager.models import Photo
from photo_manager.paths import directory_snapshot, target_directory
from photo_manager.refile import RefileError, execute_refile, plan_refile, reverse_refile

NOW = dt.datetime(2026, 9, 23, 12, 0, 0)


def put(catalog: Catalog, relative: str, when: dt.datetime, *, raw: str | None = None,
        sidecars: bool = False, inferred: bool = False, body: bytes | None = None) -> Photo:
    root = catalog.settings.library_root
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body if body is not None else relative.encode())
    if raw:
        (root / raw).write_bytes(b"raw:" + raw.encode())
    if sidecars:
        for name in [relative] + ([raw] if raw else []):
            (root / f"{name}.xmp").write_text(f"<x:xmpmeta>{name}</x:xmpmeta>", encoding="utf-8")
    return catalog.photos.add(Photo(
        content_hash=content_hash(path),
        library_path=relative,
        original_name=Path(relative).name,
        captured_at=when,
        imported_at=NOW,
        raw_path=raw,
        captured_at_inferred=inferred,
    ))


def files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_the_folder_for_a_capture_time_is_its_month() -> None:
    assert target_directory(dt.datetime(2026, 9, 6, 14, 3)) == "2026/2026-09"


def test_planning_writes_nothing(catalog: Catalog) -> None:
    put(catalog, "2026/2026-09-05/DSCF0801.JPG", dt.datetime(2026, 9, 5, 10), sidecars=True)
    root = catalog.settings.library_root
    before = directory_snapshot(root)

    plan = plan_refile(catalog, now=NOW)

    assert directory_snapshot(root) == before
    assert [p.library_path for p in plan.photos] == ["2026/2026-09/DSCF0801.JPG"]


def test_a_pair_and_its_sidecars_move_together_and_the_catalog_follows(
    catalog: Catalog, tmp_path: Path
) -> None:
    root = catalog.settings.library_root
    photo = put(catalog, "2026/2026-09-06/DSCF0844.JPG", dt.datetime(2026, 9, 6, 15),
                raw="2026/2026-09-06/DSCF0844.RAF", sidecars=True)
    hashes = {name: content_hash(root / name) for name in files(root)}

    outcome = execute_refile(plan_refile(catalog, now=NOW), catalog,
                             reversal_path=tmp_path / "log.jsonl")

    assert outcome.complete and outcome.photos_refiled == 1
    assert files(root) == {
        "2026/2026-09/DSCF0844.JPG", "2026/2026-09/DSCF0844.JPG.xmp",
        "2026/2026-09/DSCF0844.RAF", "2026/2026-09/DSCF0844.RAF.xmp",
    }
    moved = {name.replace("2026-09-06", "2026-09"): digest for name, digest in hashes.items()}
    assert {name: content_hash(root / name) for name in files(root)} == moved
    row = catalog.photos.get(photo.id)
    assert (row.library_path, row.raw_path) == (
        "2026/2026-09/DSCF0844.JPG", "2026/2026-09/DSCF0844.RAF")
    assert not (root / "2026" / "2026-09-06").exists(), "the emptied day folder is removed"


def test_a_clash_inside_a_month_is_suffixed_with_its_sidecar(catalog: Catalog,
                                                             tmp_path: Path) -> None:
    put(catalog, "2026/2026-09-05/DSCF0001.JPG", dt.datetime(2026, 9, 5, 9))
    put(catalog, "2026/2026-09-20/DSCF0001.JPG", dt.datetime(2026, 9, 20, 9), sidecars=True)

    plan = plan_refile(catalog, now=NOW)
    execute_refile(plan, catalog, reversal_path=tmp_path / "log.jsonl")

    assert files(catalog.settings.library_root) == {
        "2026/2026-09/DSCF0001.JPG",
        "2026/2026-09/DSCF0001_1.JPG", "2026/2026-09/DSCF0001_1.JPG.xmp",
    }


def test_a_photograph_already_in_its_month_is_left_alone(catalog: Catalog) -> None:
    put(catalog, "2026/2026-09/2026-09-21_DSCF0900.jpg", dt.datetime(2026, 9, 21, 9))

    plan = plan_refile(catalog, now=NOW)

    assert plan.photos == () and plan.already_filed == 1


def test_a_stray_file_stops_the_run_before_anything_moves(catalog: Catalog,
                                                          tmp_path: Path) -> None:
    put(catalog, "2026/2026-09-05/DSCF0801.JPG", dt.datetime(2026, 9, 5, 10))
    root = catalog.settings.library_root
    (root / "2026" / "2026-09-05" / "notes.txt").write_text("mine", encoding="utf-8")
    before = directory_snapshot(root)

    plan = plan_refile(catalog, now=NOW)
    assert plan.strays == ("2026/2026-09-05/notes.txt",)
    with pytest.raises(RefileError, match="belong to no catalogued"):
        execute_refile(plan, catalog, reversal_path=tmp_path / "log.jsonl")
    assert directory_snapshot(root) == before


def test_a_file_changed_since_the_plan_stops_the_run(catalog: Catalog, tmp_path: Path) -> None:
    put(catalog, "2026/2026-09-05/DSCF0801.JPG", dt.datetime(2026, 9, 5, 10))
    put(catalog, "2026/2026-09-06/DSCF0844.JPG", dt.datetime(2026, 9, 6, 10))
    root = catalog.settings.library_root
    plan = plan_refile(catalog, now=NOW)
    (root / "2026/2026-09-06/DSCF0844.JPG").write_bytes(b"edited")

    with pytest.raises(RefileError, match="changed since the plan"):
        execute_refile(plan, catalog, reversal_path=tmp_path / "log.jsonl")
    assert (root / "2026/2026-09-05/DSCF0801.JPG").is_file(), "nothing moved at all"


def test_a_failed_check_puts_that_photograph_back_and_stops(
    catalog: Catalog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = put(catalog, "2026/2026-08-01/DSCF0866.JPG", dt.datetime(2026, 8, 1, 10))
    second = put(catalog, "2026/2026-09-06/DSCF0844.JPG", dt.datetime(2026, 9, 6, 10),
                 raw="2026/2026-09-06/DSCF0844.RAF")
    root = catalog.settings.library_root
    plan = plan_refile(catalog, now=NOW)

    real = refile.content_hash
    def corrupt_raf_on_arrival(path: Path) -> str:
        if Path(path).as_posix().endswith("2026-09/DSCF0844.RAF"):
            return "0" * 64
        return real(path)
    monkeypatch.setattr(refile, "content_hash", corrupt_raf_on_arrival)

    outcome = execute_refile(plan, catalog, reversal_path=tmp_path / "log.jsonl")

    assert not outcome.complete and outcome.photos_refiled == 1
    assert catalog.photos.get(first.id).library_path == "2026/2026-08/DSCF0866.JPG"
    assert catalog.photos.get(second.id).library_path == "2026/2026-09-06/DSCF0844.JPG"
    assert {"2026/2026-09-06/DSCF0844.JPG", "2026/2026-09-06/DSCF0844.RAF"} <= files(root)


def test_a_day_only_the_folder_records_is_found_and_can_be_kept_in_the_mtime(
    catalog: Catalog, tmp_path: Path
) -> None:
    decided = dt.datetime(2026, 4, 2, 17, 27, 14)
    crop = put(catalog, "2026/2026-04-02/Birthday_1.jpg", decided, inferred=True)
    put(catalog, "2026/2026-04-02/seafront.JPG", dt.datetime(2026, 4, 2, 9), inferred=True)
    root = catalog.settings.library_root
    a_day_later = dt.datetime(2026, 4, 3, 9, 7, 54).timestamp()
    os.utime(root / crop.library_path, (a_day_later, a_day_later))
    agrees = dt.datetime(2026, 4, 2, 9).timestamp()
    os.utime(root / "2026/2026-04-02/seafront.JPG", (agrees, agrees))

    plan = plan_refile(catalog, now=NOW)
    assert [p.library_path for p in plan.date_only_in_folder] == [crop.library_path]

    log = tmp_path / "log.jsonl"
    execute_refile(plan, catalog, reversal_path=log, fix_timestamps=True)

    moved = root / "2026/2026-04/Birthday_1.jpg"
    assert dt.datetime.fromtimestamp(moved.stat().st_mtime) == decided
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    [stamp] = [r for r in records if r["kind"] == "mtime"]
    assert stamp["path"] == crop.library_path and stamp["to"] == decided.isoformat()


def test_reversal_puts_files_and_catalog_paths_back(catalog: Catalog, tmp_path: Path) -> None:
    photo = put(catalog, "2026/2026-09-06/DSCF0844.JPG", dt.datetime(2026, 9, 6, 15),
                raw="2026/2026-09-06/DSCF0844.RAF", sidecars=True)
    root = catalog.settings.library_root
    before = files(root)
    log = tmp_path / "log.jsonl"
    execute_refile(plan_refile(catalog, now=NOW), catalog, reversal_path=log)

    outcome = reverse_refile(log, catalog)

    assert outcome.complete and outcome.restored == 4
    assert files(root) == before
    row = catalog.photos.get(photo.id)
    assert (row.library_path, row.raw_path) == (
        "2026/2026-09-06/DSCF0844.JPG", "2026/2026-09-06/DSCF0844.RAF")
    assert not (root / "2026" / "2026-09").exists()

    again = reverse_refile(log, catalog)
    assert again.complete and again.restored == 0
