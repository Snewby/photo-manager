"""The web app skeleton (WU-2.2).

Four things this file is really about, and they are not evenly weighted.

**A missing card must never render as an empty grid.** That is FR-1.1a, and
WU-2.1 fixed it one layer down; a UI is perfectly capable of throwing the
distinction away again on the way to the browser. So there are tests here whose
only job is to assert that two situations reach the page as different things.

**Already-imported photos must be visibly distinct** — the unit's acceptance
criterion — and the *two kinds* of already-imported must be distinct from each
other. ``likely-imported`` is a filename match and ``imported`` is a hash; the
first may skip a photo and the second may discard one, so a UI that drew them
identically would be undoing the entire two-tier design.

**No endpoint may take a path.** A browser on localhost is still a browser, and
other pages the user has open can reach this port. The only handle on a file is
an index into a scan the server performed itself.

**The card stays read-only** (§4.1, NFR-3), proved by snapshotting it around the
whole HTTP surface rather than by promising it.
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
import threading
import time
import typing
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import fixtures
import pytest
from fastapi.testclient import TestClient

from photo_manager import cards
from photo_manager.cards import CardStatus, Dedup
from photo_manager.catalog import Catalog
from photo_manager.config import LibraryRootError, Settings
from photo_manager.culling import FREE_SPACE_MARGIN, Verdict
from photo_manager.hashing import content_hash
from photo_manager.migration import directory_snapshot
from photo_manager.models import Location, Person, Photo, Session, Tag
from photo_manager.thumbnails import ThumbnailCache
from photo_manager.web.app import MAX_PAGE, create_app
from photo_manager.web import state as web_state
from photo_manager.web.state import AppState, ConfirmStatus


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def never_probe_the_real_machine(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No web test looks at this machine's actual drives.

    ``detect_card`` probes the attached volumes, which on the owner's machine means
    finding the real card — so without this, every assertion in the file would
    depend on whether it happens to be in the reader. The one test that *wants*
    the real card says so with a marker and keeps the real probe.
    """
    if request.node.get_closest_marker("needs_card") is None:
        monkeypatch.setattr(cards, "candidate_volumes", lambda: ())
        # A synthetic card is a temporary folder, which on macOS is on a
        # writable disk; as far as these tests go it is a card mounted the
        # safe way. The tests that want it writable say so.
        monkeypatch.setattr(cards, "card_writable", lambda path: False)
        monkeypatch.setattr(web_state, "card_writable", lambda path: False)

    # Never eject anything real. A synthetic card is a folder on this Mac's own
    # disk, and `diskutil eject` on it would be aimed at that disk. The tests
    # that eject replace this with a recorder.
    def refuse_to_eject(path: Path) -> None:
        raise AssertionError(f"a test tried to eject {path} for real")

    monkeypatch.setattr(web_state, "eject_card", refuse_to_eject)


@pytest.fixture
def card(tmp_path: Path) -> Path:
    """An image folder on a synthetic card. Returns the folder."""
    folder = tmp_path / "card" / "DCIM" / "100_FUJI"
    folder.mkdir(parents=True)
    return folder


def put(folder: Path, name: str, fixture: Path) -> Path:
    target = folder / name
    shutil.copy2(fixture, target)
    return target


def client_for(settings: Settings) -> TestClient:
    return TestClient(create_app(settings, state=AppState(settings)))


@pytest.fixture
def client(settings: Settings, card: Path) -> Iterator[TestClient]:
    """An app pointed at a synthetic card holding three real frames."""
    put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    put(card, "DSCF0807.RAF", fixtures.RAW_PAIR_RAF)
    put(card, "DSCF1029.JPG", fixtures.JPEG_WITHOUT_RAW)
    with client_for(settings) as open_client:
        yield open_client


def seed(settings: Settings, **columns: object) -> None:
    """Put one row in the catalog, creating it if it does not exist yet."""
    defaults: dict[str, object] = {
        "content_hash": "a" * 64,
        "library_path": "2026/2026-07-13/DSCF0807.JPG",
        "original_name": "DSCF0807.JPG",
        "captured_at": dt.datetime(2026, 7, 13, 12, 50, 56),
        "imported_at": dt.datetime(2026, 9, 20, 9, 0, 0),
    }
    defaults.update(columns)
    with Catalog.open(settings) as catalog:
        catalog.photos.add(Photo(**defaults))  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Starting up
# ----------------------------------------------------------------------


def test_the_app_refuses_to_start_when_the_library_root_is_missing(tmp_path: Path) -> None:
    """§4.2: fail loudly in the terminal, not with a 500 three clicks in."""
    settings = Settings(library_root=tmp_path / "gone", state_dir=tmp_path / "state")
    with pytest.raises(LibraryRootError):
        create_app(settings)


def test_health_reports_the_library_and_that_there_is_no_catalog_yet(
    settings: Settings,
) -> None:
    with client_for(settings) as client:
        body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["library_root"] == str(settings.library_root)
    # Before the migration has run there is no catalog, and that is a normal
    # state to be in rather than an error.
    assert body["catalogued_photos"] is None


def test_health_counts_the_catalog_once_there_is_one(settings: Settings) -> None:
    seed(settings)
    with client_for(settings) as client:
        body = client.get("/api/health").json()
    assert body["catalogued_photos"] == 1
    assert body["schema_version"] >= 1


def test_the_page_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'id="grid"' in response.text


# ----------------------------------------------------------------------
# FR-1.1a — a status, never an empty grid
# ----------------------------------------------------------------------


def test_no_card_reports_a_status_and_a_sentence_rather_than_no_photos(
    library_root: Path, tmp_path: Path
) -> None:
    settings = Settings(library_root=library_root, state_dir=tmp_path / "state")
    with client_for(settings) as client:
        body = client.get("/api/card").json()

    assert body["location"]["status"] == CardStatus.NO_MEDIA
    assert body["location"]["found"] is False
    assert body["location"]["message"]  # a sentence a human can act on
    assert body["photos"] == []


def test_a_wrong_configured_path_and_no_card_reach_the_browser_as_different_things(
    library_root: Path, tmp_path: Path
) -> None:
    """The bug WU-2.1 fixed, re-asserted at the HTTP boundary.

    Both of these used to be an empty grid. If they ever collapse into one
    again, this is the test that says so.
    """
    nothing = Settings(library_root=library_root, state_dir=tmp_path / "state")
    wrong = Settings(
        library_root=library_root,
        state_dir=tmp_path / "state",
        card_dcim_root=tmp_path / "Q" / "DCIM",
    )
    with client_for(nothing) as client:
        no_card = client.get("/api/card").json()["location"]
    with client_for(wrong) as client:
        bad_path = client.get("/api/card").json()["location"]

    assert no_card["status"] == CardStatus.NO_MEDIA
    assert bad_path["status"] == CardStatus.CONFIGURED_PATH_MISSING
    assert no_card["status"] != bad_path["status"]
    assert no_card["message"] != bad_path["message"]


def test_an_empty_dcim_tree_is_its_own_status(settings: Settings, tmp_path: Path) -> None:
    (tmp_path / "card" / "DCIM").mkdir(parents=True)
    with client_for(settings) as client:
        location = client.get("/api/card").json()["location"]
    assert location["status"] == CardStatus.CONFIGURED_PATH_EMPTY
    assert location["found"] is False


# ----------------------------------------------------------------------
# The grid
# ----------------------------------------------------------------------


def test_the_scan_lists_every_photo_with_a_thumbnail_url(client: TestClient) -> None:
    body = client.get("/api/card").json()

    assert body["location"]["found"] is True
    assert body["counts"]["photos"] == 2  # the RAF rides on its JPEG's row
    assert body["counts"]["pairs"] == 1
    assert [photo["name"] for photo in body["photos"]] == ["DSCF0807.JPG", "DSCF1029.JPG"]
    for index, photo in enumerate(body["photos"]):
        assert photo["index"] == index
        assert photo["thumbnail_url"] == (
            f"/api/card/{body['scan_id']}/photos/{index}/thumbnail"
        )


def test_a_raw_pair_is_one_photo_carrying_a_raw_flag(client: TestClient) -> None:
    photos = {p["name"]: p for p in client.get("/api/card").json()["photos"]}
    assert photos["DSCF0807.JPG"]["has_raw"] is True
    assert photos["DSCF1029.JPG"]["has_raw"] is False


def test_the_scan_reports_all_four_dedup_states_even_at_zero(client: TestClient) -> None:
    """A missing key and a zero read very differently in a UI.

    "0 imported" is a fact about the card; a gap where the number should be
    looks like "not checked yet", which is a different thing entirely.
    """
    dedup = client.get("/api/card").json()["counts"]["dedup"]
    assert set(dedup) == {
        Dedup.NEW, Dedup.LIKELY_IMPORTED, Dedup.IMPORTED, Dedup.NOT_IMPORTED,
    }


def test_no_filesystem_path_leaks_into_the_photo_list(client: TestClient) -> None:
    body = client.get("/api/card").json()
    for photo in body["photos"]:
        assert "path" not in photo
        assert photo["folder"] == "100_FUJI"  # the name, not the location


def test_rescanning_mints_a_new_scan_id(client: TestClient) -> None:
    first = client.get("/api/card").json()["scan_id"]
    again = client.get("/api/card").json()["scan_id"]
    rescanned = client.post("/api/card/scan").json()["scan_id"]

    assert again == first  # a plain GET does not re-read the card
    assert rescanned != first


# ----------------------------------------------------------------------
# Already imported, visibly
# ----------------------------------------------------------------------


def test_a_filename_the_catalog_knows_comes_back_as_likely_imported(
    settings: Settings, card: Path
) -> None:
    put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    seed(settings)  # the right name, deliberately the wrong hash

    with client_for(settings) as client:
        photo = client.get("/api/card").json()["photos"][0]

    # Cheap evidence, and never spelled the same as proof.
    assert photo["dedup"] == Dedup.LIKELY_IMPORTED
    assert photo["dedup"] != Dedup.IMPORTED
    assert photo["confirmed"] is False


def test_confirming_turns_a_filename_match_into_a_hash_confirmed_one(
    settings: Settings, card: Path
) -> None:
    source = put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    seed(settings, content_hash=content_hash(source))

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        client.post(f"/api/card/{scan_id}/confirm")
        _wait_for_confirm(client, scan_id)
        photo = client.get("/api/card").json()["photos"][0]

    assert photo["dedup"] == Dedup.IMPORTED
    assert photo["confirmed"] is True


def test_a_reused_filename_with_different_bytes_is_rescued_back_into_the_cull(
    settings: Settings, card: Path
) -> None:
    """The failure that would actually lose a photograph.

    The camera's counter rolls over, so a reformatted card can present a
    DSCF0807.JPG that is not the one in the library. The cheap tier says
    "imported?"; only the hash rescues it, and it must come back offered for
    culling rather than quietly skipped.
    """
    put(card, "DSCF0807.JPG", fixtures.JPEG_WITHOUT_RAW)  # same name, other frame
    seed(settings)  # catalog holds DSCF0807.JPG at a different hash

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        client.post(f"/api/card/{scan_id}/confirm")
        _wait_for_confirm(client, scan_id)
        body = client.get("/api/card").json()

    assert body["photos"][0]["dedup"] == Dedup.NOT_IMPORTED
    assert body["counts"]["to_cull"] == 1


def test_likely_imported_photos_are_withheld_from_the_cull_count(
    settings: Settings, card: Path
) -> None:
    put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    put(card, "DSCF1029.JPG", fixtures.JPEG_WITHOUT_RAW)
    seed(settings)

    with client_for(settings) as client:
        counts = client.get("/api/card").json()["counts"]

    assert counts["photos"] == 2
    assert counts["to_cull"] == 1  # success criterion 2, at the API boundary


# ----------------------------------------------------------------------
# Confirming in the background (FR-1.4, C-4)
# ----------------------------------------------------------------------


def _wait_for_confirm(client: TestClient, scan_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/card/{scan_id}/confirm").json()
        if body["confirm"]["status"] != ConfirmStatus.RUNNING:
            return body
        time.sleep(0.02)
    raise AssertionError("the confirming pass never finished")


def test_confirming_returns_immediately_and_reports_progress(
    settings: Settings, card: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """78 seconds of card reads may not happen on a request thread (C-4)."""
    source = put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    seed(settings, content_hash=content_hash(source))

    release = threading.Event()
    real = cards.content_hash

    def blocked(path: Path) -> str:
        release.wait(10)
        return real(path)

    monkeypatch.setattr(cards, "content_hash", blocked)

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        started = time.perf_counter()
        body = client.post(f"/api/card/{scan_id}/confirm").json()
        # The POST returned while a hash is still blocked, which is the point.
        assert time.perf_counter() - started < 5.0
        assert body["confirm"]["status"] == ConfirmStatus.RUNNING
        assert body["confirm"]["total"] == 1

        release.set()
        finished = _wait_for_confirm(client, scan_id)

    assert finished["confirm"]["status"] == ConfirmStatus.DONE
    assert finished["confirm"]["done"] == 1
    assert finished["dedup"][Dedup.IMPORTED] == 1


def test_asking_twice_does_not_start_a_second_pass_over_the_card(
    settings: Settings, card: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two passes would each get half the reader, and the reader is the limit."""
    source = put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    seed(settings, content_hash=content_hash(source))

    release = threading.Event()
    calls: list[Path] = []
    real = cards.content_hash

    def counted(path: Path) -> str:
        calls.append(path)
        release.wait(10)
        return real(path)

    monkeypatch.setattr(cards, "content_hash", counted)

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        client.post(f"/api/card/{scan_id}/confirm")
        _wait_until(lambda: len(calls) == 1)
        client.post(f"/api/card/{scan_id}/confirm")
        release.set()
        _wait_for_confirm(client, scan_id)

    assert len(calls) == 1


def test_a_rescan_stops_a_confirming_pass_instead_of_letting_it_finish(
    settings: Settings, card: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass nobody will read is 3 GB of reads competing with the grid."""
    for index in range(6):
        source = put(card, f"DSCF{index:04d}.JPG", fixtures.JPEG_WITHOUT_RAW)
    digest = content_hash(source)
    for index in range(6):
        seed(
            settings,
            content_hash=f"{index}" + digest[1:],
            library_path=f"2026/2026-09-06/DSCF{index:04d}.JPG",
            original_name=f"DSCF{index:04d}.JPG",
        )

    gate = threading.Event()
    hashed: list[Path] = []
    real = cards.content_hash

    def slow(path: Path) -> str:
        hashed.append(path)
        gate.wait(0.5)
        return real(path)

    monkeypatch.setattr(cards, "content_hash", slow)

    state = AppState(settings)
    with TestClient(create_app(settings, state=state)) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        assert client.get("/api/card").json()["counts"]["dedup"][Dedup.LIKELY_IMPORTED] == 6
        client.post(f"/api/card/{scan_id}/confirm")
        _wait_until(lambda: len(hashed) >= 1)
        client.post("/api/card/scan")  # a new scan; the old answer is worthless
        gate.set()
        time.sleep(0.3)

    assert len(hashed) < 6, "the pass carried on hashing a card nobody is looking at"


def _wait_until(predicate, timeout: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")


# ----------------------------------------------------------------------
# Thumbnails
# ----------------------------------------------------------------------


def test_a_camera_written_frame_serves_its_embedded_thumbnail(client: TestClient) -> None:
    """Tier 1, which is the whole of FR-2.1: a header read, not a decode."""
    body = client.get("/api/card").json()
    response = client.get(body["photos"][0]["thumbnail_url"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["X-Thumbnail-Tier"] == "embedded"
    assert response.content.startswith(b"\xff\xd8")
    # 160 x 120 of JPEG. If this ever arrives at hundreds of KB, something has
    # started decoding the 14 MB frame behind it.
    assert len(response.content) < 64 * 1024


def test_a_frame_with_no_embedded_thumbnail_falls_through_and_is_cached(
    settings: Settings, card: Path
) -> None:
    """FR-2.1a and FR-2.3 together.

    ``seafront.JPG`` has been through other software and lost its embedded
    thumbnail. That must be a fall-through to a generated one, never a hole in
    the grid — and the generated one is what the disk cache is for.
    """
    put(card, "seafront.JPG", fixtures.WITHOUT_EMBEDDED_THUMBNAIL)

    with client_for(settings) as client:
        url = client.get("/api/card").json()["photos"][0]["thumbnail_url"]
        first = client.get(url)
        second = client.get(url)

    assert first.status_code == 200
    assert first.headers["X-Thumbnail-Tier"] == "generated"
    assert first.headers["X-Thumbnail-Cache"] == "miss"
    assert second.headers["X-Thumbnail-Cache"] == "hit"
    assert second.content == first.content
    assert list(settings.thumbnail_cache_dir.rglob("*.jpg"))


def test_a_truncated_frame_still_shows_because_its_header_survived(
    settings: Settings, card: Path
) -> None:
    """Tier 1 earning its keep in the one case it was not designed for.

    A file cut off at 64 KB cannot be decoded at all, but its APP1 segment is
    at the very front and so its embedded thumbnail is intact — which means the
    grid can show the frame rather than a gap.
    """
    put(card, "DSCF9999.JPG", fixtures.TRUNCATED)

    with client_for(settings) as client:
        url = client.get("/api/card").json()["photos"][0]["thumbnail_url"]
        response = client.get(url)

    assert response.status_code == 200
    assert response.headers["X-Thumbnail-Tier"] == "embedded"


def test_a_frame_that_defeats_both_tiers_is_one_broken_tile_not_a_broken_grid(
    settings: Settings, card: Path
) -> None:
    """FR-1.3c at the UI boundary: one bad frame costs one tile."""
    (card / "DSCF9999.JPG").write_bytes(b"\xff\xd8 this is not a photograph")
    put(card, "DSCF1029.JPG", fixtures.JPEG_WITHOUT_RAW)

    with client_for(settings) as client:
        body = client.get("/api/card").json()
        assert body["counts"]["photos"] == 2  # the grid still lists both

        tiles = {photo["name"]: photo["thumbnail_url"] for photo in body["photos"]}
        assert client.get(tiles["DSCF9999.JPG"]).status_code == 422
        assert client.get(tiles["DSCF1029.JPG"]).status_code == 200


def test_thumbnails_are_cacheable_by_the_browser(client: TestClient) -> None:
    """Scrolling back up a thousand-tile grid must not re-read the card."""
    url = client.get("/api/card").json()["photos"][0]["thumbnail_url"]
    response = client.get(url)
    assert "immutable" in response.headers["cache-control"]


def test_a_scan_is_never_cached_by_the_browser(client: TestClient) -> None:
    """It is a live reading of what is in the reader. A cached one is a lie."""
    assert client.get("/api/card").headers["cache-control"] == "no-store"


# ----------------------------------------------------------------------
# Addressing, and what the API refuses to do
# ----------------------------------------------------------------------


#: Path parameters that are not integers and are allowed anyway, each with the
#: reason it cannot name a file. Anything else must be declared ``int``, which
#: is a type a path cannot survive being parsed into.
_OPAQUE_PARAMETERS = {
    # A server-issued token naming a scan the server itself performed. It is
    # looked up in a dict of scans, never joined to anything.
    "scan_id",
    # One of three literal words, checked against a closed mapping before it
    # reaches anything — see the test below.
    "kind",
}


def test_no_route_accepts_a_filesystem_path(settings: Settings) -> None:
    """The structural version of the rule, not an instance of it.

    Any page the user has open can issue requests at this port. If a route ever
    appears that takes a path, this app stops being a photo viewer and starts
    being a file server for the whole disk.

    This used to be an allowlist of parameter *names*, which meant every new
    route widened it and the rule got weaker each time — WU-3.4 added five and
    the list would have grown to eight. It now checks the thing that actually
    makes a parameter safe: **it is an integer**, which is a type no path
    survives being parsed into. A library photograph is addressed by its
    catalog id, a primary key the server issued, and the two named exceptions
    carry their reasons above.
    """
    app = create_app(settings, state=AppState(settings))

    checked = 0
    for route in app.routes:
        path = getattr(route, "path", "")
        for name in re.findall(r"\{([a-z_]+)[:}]", path):
            if name in _OPAQUE_PARAMETERS:
                continue
            # get_type_hints rather than the raw annotation, because
            # `from __future__ import annotations` makes every one of them a
            # string and `"int" is int` is False for the wrong reason.
            hints = typing.get_type_hints(route.endpoint)
            assert hints.get(name) is int, f"{path}: {name} is {hints.get(name)!r}, not int"
            checked += 1
    assert checked, "no parameterised routes found — the test has stopped testing"


def test_the_vocabulary_kind_is_a_closed_set(settings: Settings) -> None:
    """The one non-integer parameter that reaches a lookup, checked from the
    outside as well as reasoned about above.

    The catalog is seeded because "there is no library yet" is its own answer
    (``no-library``) and would otherwise mask the one being tested here.
    """
    seed(settings)
    with client_for(settings) as client:
        for kind in ("locations", "people", "tags"):
            assert client.get(f"/api/library/vocabulary/{kind}").status_code == 200, kind

        # A name that reaches the handler and is not one of the three.
        for bad in ("photos", "etc", "locations2"):
            response = client.get(f"/api/library/vocabulary/{bad}")
            assert response.status_code == 404, bad
            assert response.json()["error"] == "not-found", bad

        # And a traversal attempt, which does not reach a handler at all —
        # there is no route it can match. The claim worth making is only that
        # it is never answered.
        for bad in ("..", "%2e%2e", "../../etc/passwd"):
            response = client.get(f"/api/library/vocabulary/{bad}")
            assert response.status_code == 404, bad
            assert "items" not in (response.json() or {}), bad


def test_an_index_past_the_end_of_the_scan_is_a_404(client: TestClient) -> None:
    scan_id = client.get("/api/card").json()["scan_id"]
    response = client.get(f"/api/card/{scan_id}/photos/9999/thumbnail")
    assert response.status_code == 404
    assert response.json()["error"] == "unknown-photo"


def test_a_stale_scan_id_is_refused_rather_than_answered_from_the_current_scan(
    client: TestClient,
) -> None:
    """Indices only mean anything inside one scan.

    Falling back to "the current scan" would answer a request for photo 400 of
    one card with photo 400 of another, which is silent and wrong — the worst
    available combination.
    """
    stale = client.get("/api/card").json()["scan_id"]
    client.post("/api/card/scan")

    response = client.get(f"/api/card/{stale}/photos/0/thumbnail")
    assert response.status_code == 409
    assert response.json()["error"] == "stale-scan"
    assert "reload" in response.json()["detail"].lower()


# ----------------------------------------------------------------------
# The card is read-only (§4.1, NFR-3)
# ----------------------------------------------------------------------


def test_the_whole_http_surface_leaves_the_card_untouched(
    settings: Settings, card: Path
) -> None:
    source = put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    put(card, "DSCF0807.RAF", fixtures.RAW_PAIR_RAF)
    put(card, "seafront.JPG", fixtures.WITHOUT_EMBEDDED_THUMBNAIL)
    seed(settings, content_hash=content_hash(source))

    volume = card.parent.parent
    before = directory_snapshot(volume)

    with client_for(settings) as client:
        body = client.get("/api/card").json()
        scan_id = body["scan_id"]
        for photo in body["photos"]:
            client.get(photo["thumbnail_url"])
        client.post(f"/api/card/{scan_id}/confirm")
        _wait_for_confirm(client, scan_id)
        client.post("/api/card/scan")
        client.get("/api/health")

    assert directory_snapshot(volume) == before


# ----------------------------------------------------------------------
# The page itself
# ----------------------------------------------------------------------


def _frontend(name: str) -> str:
    from photo_manager.web.app import STATIC_DIR
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def test_the_page_renders_every_dedup_state_the_api_can_produce() -> None:
    """A state the page has no case for renders as nothing, which reads as new."""
    script = _frontend("card.js")
    for state in (Dedup.NEW, Dedup.LIKELY_IMPORTED, Dedup.IMPORTED, Dedup.NOT_IMPORTED):
        assert f"'{state}'" in script


def test_the_four_dedup_states_are_never_given_the_same_label() -> None:
    """The acceptance criterion, asserted rather than eyeballed.

    "Already imported" and "possibly already imported" mean different things to
    someone deciding whether a photograph is safe to lose.
    """
    labels = re.findall(r"label: '([^']+)'", _frontend("card.js"))
    assert len(labels) == 4
    assert len(set(labels)) == 4


def test_the_stylesheet_draws_each_dedup_state_differently() -> None:
    styles = _frontend("app.css")
    for state in (Dedup.LIKELY_IMPORTED, Dedup.IMPORTED, Dedup.NOT_IMPORTED):
        assert f".tile--{state}" in styles


# ----------------------------------------------------------------------
# The real card
# ----------------------------------------------------------------------


@pytest.mark.needs_card
def test_the_real_card_renders_through_the_api(machine_settings: Settings) -> None:
    """The only test here that touches the actual reader. Skipped without it."""
    settings = machine_settings
    if not cards.detect_card(settings).found:
        pytest.skip("no card in the reader")

    with client_for(settings) as client:
        body = client.get("/api/card").json()
        assert body["location"]["found"] is True
        assert body["counts"]["photos"] > 0
        response = client.get(body["photos"][0]["thumbnail_url"])
        assert response.headers["X-Thumbnail-Tier"] == "embedded"


# ----------------------------------------------------------------------
# Culling — the loop (§6.4)
# ----------------------------------------------------------------------


def cull_client(settings: Settings, card: Path, *names: str) -> TestClient:
    """An app over a card holding real frames under the given names."""
    for index, name in enumerate(names):
        put(card, name, fixtures.JPEG_WITHOUT_RAW if index % 2 else fixtures.BURST_SECOND)
    return client_for(settings)


def decide(client: TestClient, scan_id: str, index: int, **body: object):
    return client.put(f"/api/card/{scan_id}/photos/{index}/decision", json=body)


def test_the_cull_offers_the_frames_the_grid_withheld(
    settings: Settings, card: Path
) -> None:
    """Success criterion 2, carried through to the thing that does the work.

    An already-imported frame stays in the grid — it is on the card — but it is
    not one of the 925 decisions anyone has to make.
    """
    put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    put(card, "DSCF1029.JPG", fixtures.JPEG_WITHOUT_RAW)
    seed(settings)  # the catalog already knows DSCF0807.JPG

    with client_for(settings) as client:
        body = client.get("/api/card").json()

    assert body["counts"]["photos"] == 2
    assert body["cull"]["order"] == [1]
    assert body["photos"][1]["name"] == "DSCF1029.JPG"


def test_a_decision_comes_back_with_the_running_tally(
    settings: Settings, card: Path
) -> None:
    with cull_client(settings, card, "DSCF0001.JPG", "DSCF0002.JPG") as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        response = decide(client, scan_id, 0, verdict="keep", rating=4)

    body = response.json()
    assert response.status_code == 200
    assert body["decision"] == {
        "verdict": "keep", "rating": 4, "keep_raw": False, "decided": True,
    }
    assert body["tally"]["keepers"] == 1
    assert body["tally"]["undecided"] == 1
    # FR-4.4's resume point moves on as decisions are made.
    assert body["next"] == 1


def test_changing_your_mind_is_allowed_and_is_the_last_word(
    settings: Settings, card: Path
) -> None:
    """FR-4.3: nothing is committed until import, so nothing is final here."""
    with cull_client(settings, card, "DSCF0001.JPG") as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        decide(client, scan_id, 0, verdict="keep", rating=5)
        decide(client, scan_id, 0, verdict="skip")
        body = client.get(f"/api/card/{scan_id}/cull").json()

    assert body["decisions"]["0"]["verdict"] == "skip"
    assert body["tally"]["keepers"] == 0
    assert body["tally"]["skipped"] == 1


def test_a_rating_on_a_skip_is_dropped_rather_than_stored(
    settings: Settings, card: Path
) -> None:
    """A number nothing will ever read is worse than no number: it looks like
    a judgement that was made."""
    with cull_client(settings, card, "DSCF0001.JPG") as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        body = decide(client, scan_id, 0, verdict="skip", rating=5, keep_raw=True).json()

    assert body["decision"]["rating"] == 0
    assert body["decision"]["keep_raw"] is False


def test_a_frame_left_on_the_card_is_not_imported_and_blocks_formatting(
    settings: Settings, card: Path
) -> None:
    """LEAVE: not imported, like a reject, but formatting would lose it, so the
    card is not safe to format while one frame says it."""
    with cull_client(settings, card, "DSCF0001.JPG", "DSCF0002.JPG") as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        body = decide(client, scan_id, 0, verdict="leave", rating=5, keep_raw=True).json()
        decide(client, scan_id, 1, verdict="skip")
        card_now = client.get("/api/card").json()
        plan = client.get(f"/api/card/{scan_id}/import").json()

    assert body["decision"] == {
        "verdict": "leave", "rating": 0, "keep_raw": False, "decided": True,
    }
    assert body["tally"]["left"] == 1
    assert body["tally"]["keepers"] == 0
    assert body["tally"]["undecided"] == 1
    assert plan["photos"] == 0
    stages = [photo["stage"] for photo in card_now["photos"]]
    assert stages == ["leave", "skip"]
    lifecycle = card_now["lifecycle"]
    assert lifecycle["counts"]["leave"] == 1
    assert lifecycle["safe_to_format"] is False
    assert lifecycle["reasons"] == ["1 you chose to leave on the card would be lost"]


def test_a_rating_outside_zero_to_five_is_refused_at_the_boundary(
    settings: Settings, card: Path
) -> None:
    with cull_client(settings, card, "DSCF0001.JPG") as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        assert decide(client, scan_id, 0, verdict="keep", rating=9).status_code == 422
        assert decide(client, scan_id, 0, verdict="perhaps").status_code == 422


def test_deciding_on_an_already_imported_frame_says_what_it_is(
    settings: Settings, card: Path
) -> None:
    """409 and a sentence, not a 404: the frame is right there in the grid."""
    put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    seed(settings)

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        response = decide(client, scan_id, 0, verdict="keep")

    assert response.status_code == 409
    assert response.json()["error"] == "not-cullable"
    assert "likely-imported" in response.json()["detail"]


def test_a_stale_scan_id_cannot_decide_anything(settings: Settings, card: Path) -> None:
    with cull_client(settings, card, "DSCF0001.JPG") as client:
        stale = client.get("/api/card").json()["scan_id"]
        client.post("/api/card/scan")
        assert decide(client, stale, 0, verdict="keep").status_code == 409


# ----------------------------------------------------------------------
# FR-4.4 — the work survives the process
# ----------------------------------------------------------------------


def test_progress_survives_the_app_being_killed(settings: Settings, card: Path) -> None:
    """The acceptance criterion, as literally as a test can put it.

    The first app is thrown away entirely — no shutdown hook, no flush on the
    way out, nothing shared with the second but the settings and the card.
    """
    for name in ("DSCF0001.JPG", "DSCF0002.JPG", "DSCF0003.JPG"):
        put(card, name, fixtures.JPEG_WITHOUT_RAW)

    first = TestClient(create_app(settings, state=AppState(settings)))
    scan_id = first.get("/api/card").json()["scan_id"]
    decide(first, scan_id, 0, verdict="keep", rating=4)
    decide(first, scan_id, 1, verdict="skip")
    del first  # the process is gone; nothing tidied up

    with client_for(settings) as second:
        body = second.get("/api/card").json()

    cull = body["cull"]
    assert cull["decisions"]["0"] == {
        "verdict": "keep", "rating": 4, "keep_raw": False, "decided": True,
    }
    assert cull["decisions"]["1"]["verdict"] == "skip"
    assert cull["resumed"]["resumed"] == 2
    # And it opens on the third frame, not back at the first.
    assert cull["resume_at"] == 2


def test_a_fully_culled_card_has_nowhere_left_to_resume(
    settings: Settings, card: Path
) -> None:
    with cull_client(settings, card, "DSCF0001.JPG") as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        decide(client, scan_id, 0, verdict="keep")
        assert client.get(f"/api/card/{scan_id}/cull").json()["resume_at"] is None


def test_a_decision_that_no_longer_matches_the_file_is_reported_not_applied(
    settings: Settings, card: Path
) -> None:
    """The rolled-over frame counter, end to end.

    The same filename comes back with different bytes. The stored ``skip`` must
    not be applied — that would silently drop a photograph — and it must not be
    dropped in silence either.
    """
    put(card, "DSCF0001.JPG", fixtures.JPEG_WITHOUT_RAW)
    first = TestClient(create_app(settings, state=AppState(settings)))
    scan_id = first.get("/api/card").json()["scan_id"]
    decide(first, scan_id, 0, verdict="skip")
    del first

    # Same name, a different photograph.
    put(card, "DSCF0001.JPG", fixtures.RAW_PAIR_JPEG)

    with client_for(settings) as second:
        cull = second.get("/api/card").json()["cull"]

    assert cull["decisions"] == {}
    assert cull["resumed"]["stale"] == ["DSCF0001.JPG"]
    assert cull["resume_at"] == 0  # offered again, which is the whole point


def test_confirming_moves_a_rescued_frame_into_the_cull(
    settings: Settings, card: Path
) -> None:
    """The cull list is re-derived when the hash changes the answer.

    A frame whose filename the catalog knew but whose bytes differ is not a
    duplicate; once the hash says so it has to become something to decide on.
    """
    put(card, "DSCF0807.JPG", fixtures.JPEG_WITHOUT_RAW)  # name matches, bytes do not
    seed(settings)

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        assert client.get("/api/card").json()["cull"]["order"] == []
        client.post(f"/api/card/{scan_id}/confirm")
        _wait_for_confirm(client, scan_id)
        assert client.get("/api/card").json()["cull"]["order"] == [0]


# ----------------------------------------------------------------------
# FR-4.5 — keepers against the space actually free
# ----------------------------------------------------------------------


def test_the_free_space_warning_fires_on_the_real_figure(
    settings: Settings, card: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil as real_shutil

    from photo_manager import culling

    tiny = FREE_SPACE_MARGIN + 1000
    monkeypatch.setattr(
        culling.shutil, "disk_usage",
        lambda path: real_shutil._ntuple_diskusage(total=tiny, used=0, free=tiny),
    )

    with cull_client(settings, card, "DSCF0001.JPG") as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        before = client.get(f"/api/card/{scan_id}/cull").json()["tally"]
        after = decide(client, scan_id, 0, verdict="keep").json()["tally"]

    assert before["over_capacity"] is False   # nothing kept yet, nothing to overrun
    assert after["over_capacity"] is True     # one real frame is bigger than 1000 bytes
    assert after["free_bytes"] == tiny


def test_the_tally_costs_a_raf_only_when_it_was_flagged(
    settings: Settings, card: Path
) -> None:
    put(card, "DSCF0807.JPG", fixtures.RAW_PAIR_JPEG)
    put(card, "DSCF0807.RAF", fixtures.RAW_PAIR_RAF)

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        plain = decide(client, scan_id, 0, verdict="keep").json()["tally"]
        with_raw = decide(client, scan_id, 0, verdict="keep", keep_raw=True).json()["tally"]

    assert plain["raw_bytes"] == 0
    assert with_raw["raw_bytes"] > 0
    assert with_raw["total_bytes"] > plain["total_bytes"]


# ----------------------------------------------------------------------
# Previews
# ----------------------------------------------------------------------


def test_the_preview_is_bigger_than_the_grid_tile_and_is_cached(
    settings: Settings, card: Path
) -> None:
    """You cannot judge a photograph on 160 x 120 (FR-4.1).

    And the second look at a frame must not read the card again — going back to
    revise a decision (FR-4.3) is meant to be free.
    """
    from io import BytesIO

    from PIL import Image

    put(card, "DSCF0807.JPG", fixtures.FULL_SIZE_ORIGINAL)

    with client_for(settings) as client:
        photo = client.get("/api/card").json()["photos"][0]
        tile = client.get(photo["thumbnail_url"])
        first = client.get(photo["preview_url"])
        again = client.get(photo["preview_url"])

    assert first.status_code == 200
    assert first.headers["X-Thumbnail-Tier"] == "preview"
    assert first.headers["X-Thumbnail-Cache"] == "miss"
    assert again.headers["X-Thumbnail-Cache"] == "hit"

    with Image.open(BytesIO(first.content)) as preview, Image.open(BytesIO(tile.content)) as small:
        assert max(preview.size) == settings.preview_max_px
        assert max(preview.size) > max(small.size)


def test_a_preview_and_a_tile_of_one_photo_do_not_evict_each_other(
    settings: Settings, card: Path
) -> None:
    """FR-2.3 puts the size in the cache filename precisely so this holds."""
    put(card, "seafront.JPG", fixtures.WITHOUT_EMBEDDED_THUMBNAIL)

    with client_for(settings) as client:
        photo = client.get("/api/card").json()["photos"][0]
        client.get(photo["thumbnail_url"])   # tier 2, cached at thumbnail_max_px
        client.get(photo["preview_url"])     # cached at preview_max_px
        assert client.get(photo["thumbnail_url"]).headers["X-Thumbnail-Cache"] == "hit"
        assert client.get(photo["preview_url"]).headers["X-Thumbnail-Cache"] == "hit"

    cached = sorted(path.name for path in settings.thumbnail_cache_dir.rglob("*.jpg"))
    assert len(cached) == 2


def test_an_unrenderable_frame_is_one_broken_preview(
    settings: Settings, card: Path
) -> None:
    (card / "DSCF9999.JPG").write_bytes(b"\xff\xd8 this is not a photograph")

    with client_for(settings) as client:
        photo = client.get("/api/card").json()["photos"][0]
        assert client.get(photo["preview_url"]).status_code == 422


def test_looking_at_a_frame_warms_the_ones_after_it(
    settings: Settings, card: Path
) -> None:
    """C-4 says a cold preview is about a second, so it cannot be paid for at
    the moment the key is pressed. The server renders ahead of the user."""
    for index in range(4):
        put(card, f"DSCF000{index}.JPG", fixtures.JPEG_WITHOUT_RAW)

    state = AppState(settings)
    with TestClient(create_app(settings, state=state)) as client:
        photos = client.get("/api/card").json()["photos"]
        client.get(photos[0]["preview_url"])
        _wait_until(lambda: len(state.session.hashes) >= 4, timeout=30)

    # All four hashed and rendered, from one request for the first.
    assert sorted(state.session.hashes) == [0, 1, 2, 3]


# ----------------------------------------------------------------------
# The page
# ----------------------------------------------------------------------


def test_the_culling_page_is_now_the_review_step(client: TestClient) -> None:
    """Old bookmarks still work: /cull lands on the card workflow's Review
    step, on the frame they named."""
    response = client.get("/cull?start=DSCF1029.JPG", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/?step=review&start=DSCF1029.JPG"
    page = client.get("/cull").text
    assert 'id="preview"' in page
    assert 'data-panel="review"' in page


def test_the_page_binds_every_key_the_requirement_names() -> None:
    """FR-4.2: next, previous, keep, skip, flag RAW, and 0-5.

    A requirement listing keys is one of the few a test can check literally,
    and a missing binding is invisible until someone reaches for it mid-card.
    """
    script = _frontend("card.js")
    for key in ("ArrowRight", "ArrowLeft", "'k'", "'l'", "'x'", "'r'", "RATINGS"):
        assert key in script
    assert "'0', '1', '2', '3', '4', '5'" in script


def test_the_page_says_what_each_key_does() -> None:
    """Five keys to learn is only true if they are written down somewhere."""
    page = _frontend("index.html")
    for described in ("keep, and move on", "leave it on the card", "reject, and move on",
                      "keep the RAF"):
        assert described in page


def test_the_stylesheet_draws_each_verdict_differently() -> None:
    styles = _frontend("app.css")
    for verdict in Verdict.ALL:
        assert f".verdict__chip--{verdict}" in styles


def test_the_photograph_is_sized_by_the_stage_not_by_itself() -> None:
    """A 1560 px preview pushed the verdict bar off a laptop screen: the
    stage's one grid track was sized by its content, so the image's
    max-height: 100% had nothing to be a percentage of."""
    styles = _frontend("app.css")
    stage = styles[styles.index(".stage {"):]
    stage = stage[:stage.index("}")]
    assert "grid-template: minmax(0, 1fr) / minmax(0, 1fr)" in stage
    assert "overflow: hidden" in stage


def test_every_key_is_on_screen_without_asking_for_it() -> None:
    page = _frontend("index.html")
    start = page.index('<nav class="keys"')
    keys = page[start:page.index("</nav>", start)]
    for key in ("K", "L", "X", "1", "5", "0", "R", "U", "←", "→", "G", "?"):
        assert f"<kbd>{key}</kbd>" in keys


def test_the_cull_says_what_each_verdict_will_do() -> None:
    script = _frontend("card.js")
    meaning = script[script.index("const MEANING"):]
    meaning = meaning[:meaning.index("};")]
    for verdict in Verdict.ALL:
        assert f"{verdict}:" in meaning


# ----------------------------------------------------------------------
# Where each frame has got to, and whether the card can be formatted
# ----------------------------------------------------------------------


def test_every_frame_says_where_it_has_got_to(settings: Settings, card: Path) -> None:
    put(card, "DSCF0001.JPG", fixtures.BURST_FIRST)
    put(card, "DSCF0002.JPG", fixtures.BURST_SECOND)
    put(card, "DSCF1029.JPG", fixtures.JPEG_WITHOUT_RAW)

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        decide(client, scan_id, 0, verdict="keep", rating=4)
        decide(client, scan_id, 1, verdict="skip")
        payload = client.get("/api/card").json()

    stages = {photo["name"]: photo["stage"] for photo in payload["photos"]}
    assert stages == {"DSCF0001.JPG": "keep", "DSCF0002.JPG": "skip", "DSCF1029.JPG": "undecided"}
    assert all(photo["backed_up"] is None for photo in payload["photos"])
    lifecycle = payload["lifecycle"]
    assert lifecycle["counts"]["keep"] == 1
    assert lifecycle["counts"]["skip"] == 1
    assert lifecycle["counts"]["undecided"] == 1
    assert lifecycle["safe_to_format"] is False
    assert lifecycle["reasons"] == ["1 not decided yet", "1 kept but not imported yet"]


def test_a_card_is_safe_to_format_only_once_its_keepers_are_backed_up(
    settings: Settings, card: Path, backup_drive: Path
) -> None:
    """In the library is not enough: until the backup holds it, formatting the
    card would leave one copy, on one disk."""
    settings = replace(settings, backup_root=backup_drive / "PMB")
    seed(settings, content_hash=content_hash(fixtures.JPEG_WITHOUT_RAW),
         library_path="2026/2026-01/DSCF1029.JPG", original_name="DSCF1029.JPG")
    (settings.library_root / "2026" / "2026-01").mkdir(parents=True, exist_ok=True)
    shutil.copy2(fixtures.JPEG_WITHOUT_RAW,
                 settings.library_root / "2026" / "2026-01" / "DSCF1029.JPG")
    put(card, "DSCF1029.JPG", fixtures.JPEG_WITHOUT_RAW)
    put(card, "DSCF0002.JPG", fixtures.BURST_SECOND)

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        by_name = lambda payload: {p["name"]: p for p in payload["photos"]}  # noqa: E731
        likely = by_name(client.get("/api/card").json())["DSCF1029.JPG"]

        client.post(f"/api/card/{scan_id}/confirm")
        _wait_for_confirm(client, scan_id)
        index = by_name(client.get("/api/card").json())["DSCF0002.JPG"]["index"]
        decide(client, scan_id, index, verdict="skip")
        before = client.get("/api/card").json()

        client.post("/api/backup")
        deadline = time.monotonic() + 30
        while client.get("/api/backup").json()["progress"]["status"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        after = client.get("/api/card").json()

    assert likely["stage"] == "likely-in-library"
    assert by_name(before)["DSCF1029.JPG"]["stage"] == "in-library"
    assert before["lifecycle"]["safe_to_format"] is False
    assert before["lifecycle"]["reasons"] == [
        "1 in the library but not confirmed in the last backup"
    ]
    assert by_name(after)["DSCF1029.JPG"]["stage"] == "backed-up"
    assert by_name(after)["DSCF1029.JPG"]["backed_up"] is True
    assert after["lifecycle"]["safe_to_format"] is True


def test_the_page_draws_every_stage_the_api_can_produce() -> None:
    from photo_manager.web.app import Stage
    script, styles, page = _frontend("card.js"), _frontend("app.css"), _frontend("index.html")
    for stage in Stage.ALL:
        assert f"'{stage}': {{" in script
        assert f'id="stage-{stage}"' in page
        assert f".chip--stage-{stage}" in styles


# ----------------------------------------------------------------------
# Importing (§6.6)
# ----------------------------------------------------------------------


def _wait_for_import(client: TestClient, scan_id: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/card/{scan_id}/import/progress").json()
        if body["import"]["status"] not in ("running",):
            return body
        time.sleep(0.02)
    raise AssertionError("the import never finished")


def test_planning_an_import_copies_nothing(settings: Settings, card: Path) -> None:
    """The first of the two steps exists precisely so that nothing happens."""
    put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        decide(client, scan_id, 0, verdict="keep")
        plan = client.get(f"/api/card/{scan_id}/import").json()

    assert plan["photos"] == 1
    assert plan["fits"] is True
    assert plan["sample"] == ["2026/2026-09/2026-09-06_DSCF1029.jpg"]
    assert list(settings.library_root.rglob("*.jpg")) == []


def test_only_keepers_are_in_the_plan(settings: Settings, card: Path) -> None:
    put(card, "DSCF0001.JPG", fixtures.BURST_FIRST)
    put(card, "DSCF0002.JPG", fixtures.BURST_SECOND)

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        decide(client, scan_id, 0, verdict="keep")
        decide(client, scan_id, 1, verdict="skip")
        plan = client.get(f"/api/card/{scan_id}/import").json()

    assert plan["photos"] == 1


def test_an_import_runs_in_the_background_and_reports_all_three_numbers(
    settings: Settings, card: Path
) -> None:
    """FR-6.6. A card is minutes of copying (C-4), so the request that starts
    it cannot be the request that waits for it."""
    put(card, "DSCF0001.JPG", fixtures.BURST_FIRST)
    put(card, "DSCF0002.JPG", fixtures.BURST_SECOND)

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        decide(client, scan_id, 0, verdict="keep", rating=5)
        decide(client, scan_id, 1, verdict="keep")

        started = client.post(f"/api/card/{scan_id}/import").json()
        assert started["import"]["status"] in ("running", "done")
        finished = _wait_for_import(client, scan_id)

    outcome = finished["import"]["outcome"]
    assert finished["import"]["status"] == "done"
    assert (outcome["imported"], outcome["skipped"], outcome["failed"]) == (2, 0, 0)
    assert outcome["complete"] is True
    assert len(list(settings.library_root.rglob("*.jpg"))) == 2


def test_an_imported_photograph_is_in_the_catalog_afterwards(
    settings: Settings, card: Path
) -> None:
    put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        decide(client, scan_id, 0, verdict="keep", rating=3)
        client.post(f"/api/card/{scan_id}/import")
        _wait_for_import(client, scan_id)
        health = client.get("/api/health").json()

    assert health["catalogued_photos"] == 1


def test_rescanning_after_an_import_offers_nothing_a_second_time(
    settings: Settings, card: Path
) -> None:
    """Success criterion 2, all the way round the loop for the first time:
    cull, import, re-insert, and the card offers zero.

    The rescan straight after the import settles the copied frames from the
    hashes the import took, without reading the card again. After a restart
    those are gone, so the filename is only evidence until the hash confirms it.
    """
    put(card, "DSCF0001.JPG", fixtures.BURST_FIRST)
    put(card, "DSCF0002.JPG", fixtures.BURST_SECOND)

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        decide(client, scan_id, 0, verdict="keep")
        decide(client, scan_id, 1, verdict="keep")
        client.post(f"/api/card/{scan_id}/import")
        _wait_for_import(client, scan_id)

        fresh = client.post("/api/card/scan").json()
        assert fresh["counts"]["dedup"][Dedup.IMPORTED] == 2
        assert fresh["counts"]["to_cull"] == 0
        assert {photo["stage"] for photo in fresh["photos"]} == {"in-library"}

    with client_for(settings) as restarted:
        cold = restarted.get("/api/card").json()
        assert cold["counts"]["dedup"][Dedup.LIKELY_IMPORTED] == 2

        # ... and the hash confirms it rather than the filename merely
        # suggesting it.
        restarted.post(f"/api/card/{cold['scan_id']}/confirm")
        _wait_for_confirm(restarted, cold["scan_id"])
        settled = restarted.get("/api/card").json()

    assert settled["counts"]["dedup"][Dedup.IMPORTED] == 2
    assert settled["counts"]["to_cull"] == 0


def test_an_import_that_would_not_fit_is_refused_with_the_numbers(
    settings: Settings, card: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-4.5 at the only moment it can still be acted on."""
    import shutil as real_shutil

    from photo_manager import importing

    put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)
    monkeypatch.setattr(
        importing.shutil, "disk_usage",
        lambda path: real_shutil._ntuple_diskusage(total=1000, used=0, free=1000),
    )

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        decide(client, scan_id, 0, verdict="keep")
        plan = client.get(f"/api/card/{scan_id}/import").json()
        client.post(f"/api/card/{scan_id}/import")
        finished = _wait_for_import(client, scan_id)

    assert plan["fits"] is False
    assert finished["import"]["status"] == "failed"
    assert "gigabyte spare" in finished["import"]["error"]
    assert list(settings.library_root.rglob("*.jpg")) == []


def test_starting_an_import_returns_before_it_has_finished(
    settings: Settings, card: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full card is minutes of copying (C-4). The request cannot hold it open.

    Asserted by blocking the copy and checking the POST comes back anyway —
    measuring the elapsed time of a fast import would pass whether it ran in
    the background or not.
    """
    from photo_manager import importing

    put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)
    release = threading.Event()
    real = importing._copy_verified

    def blocked(source: Path, destination: Path) -> str:
        release.wait(10)
        return real(source, destination)

    monkeypatch.setattr(importing, "_copy_verified", blocked)

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        decide(client, scan_id, 0, verdict="keep")
        body = client.post(f"/api/card/{scan_id}/import").json()
        # Still copying, and we are already back.
        assert body["import"]["status"] == "running"
        assert body["import"]["outcome"] is None
        release.set()
        finished = _wait_for_import(client, scan_id)

    assert finished["import"]["outcome"]["imported"] == 1


def test_asking_twice_does_not_start_a_second_import(
    settings: Settings, card: Path
) -> None:
    """Two importers over one card would race for the same target names."""
    put(card, "DSCF1029.JPG", fixtures.BURST_FIRST)

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        decide(client, scan_id, 0, verdict="keep")
        client.post(f"/api/card/{scan_id}/import")
        client.post(f"/api/card/{scan_id}/import")
        _wait_for_import(client, scan_id)
        health = client.get("/api/health").json()

    assert health["catalogued_photos"] == 1
    assert len(list(settings.library_root.rglob("*.jpg"))) == 1


def test_the_import_endpoints_take_no_path_either(settings: Settings) -> None:
    """The structural rule, re-checked now that four more routes exist.

    Kept as its own test rather than folded into the one above, because the
    point of it is that it is re-run after every batch of new routes — but it
    now calls the same check rather than keeping a second copy of the
    allowlist, which was how the two drifted into disagreeing about what was
    permitted.
    """
    test_no_route_accepts_a_filesystem_path(settings)


def test_the_summary_of_an_import_survives_the_rescan_that_follows_it() -> None:
    """FR-6.6 asks for a clear summary. One wiped a second later is not one.

    Finishing an import rescans the card, because the catalog has changed and
    every already-imported state in the grid is now stale — and that fresh
    session reports its import as 'idle'. The page has to remember.
    """
    # The guard itself, not merely a mention of the variable: assigning
    # `state.lastImport` and never reading it back would leave the summary just
    # as wiped, and a looser assertion stayed green when the read was deleted.
    script = _frontend("card.js")
    assert "state.lastImport" in script
    assert "progress = state.lastImport" in script


def test_the_page_makes_the_import_two_steps() -> None:
    """Plan, read, then go. The only button in this app that writes a
    photograph should not be the first one under the pointer."""
    page = _frontend("index.html")
    assert 'id="import-plan"' in page
    assert 'id="import-go"' in page
    script = _frontend("card.js")
    assert "renderImport" in script


# ----------------------------------------------------------------------
# The library grid and its filter panel (WU-4.2)
# ----------------------------------------------------------------------
#
# The endpoint answers the page in one request, so these tests are mostly
# about the two halves of that answer agreeing: the photographs and the counts
# beside them describe one filter, and every count is a promise about what
# clicking it will give.


def stock(settings: Settings) -> dict[str, int]:
    """A small library with something to filter on. Returns the ids by name.

    Nine photographs: an outing at St Aldo with an event, two people and a
    tag, one frame of it overriding the location (FR-3.4), a second outing
    with neither, and two frames in no session at all.
    """
    with Catalog.open(settings) as catalog:
        ids: dict[str, int] = {}
        for name, slug in (("St Aldo", "st-aldo"), ("Bristol", "bristol")):
            row = catalog.locations.add(Location(name=name, slug=slug))
            assert row.id is not None
            ids[name] = row.id
        for name in ("Alice", "Bob"):
            person = catalog.people.add(Person(name=name))
            assert person.id is not None
            ids[name] = person.id
        tag = catalog.tags.add(Tag(name="beach"))
        assert tag.id is not None
        ids["beach"] = tag.id

        outing = catalog.sessions.add(
            Session(
                started_at=dt.datetime(2026, 9, 6, 9),
                ended_at=dt.datetime(2026, 9, 6, 18),
                location_id=ids["St Aldo"],
                event="Saturday market",
            )
        )
        other = catalog.sessions.add(
            Session(
                started_at=dt.datetime(2026, 9, 7, 9),
                ended_at=dt.datetime(2026, 9, 7, 18),
                location_id=ids["Bristol"],
            )
        )
        assert outing.id is not None and other.id is not None
        ids["outing"] = outing.id
        ids["other"] = other.id

        plan = [
            # (day, hour, rating, raw, session, people, tags)
            ("2026-09-06", 10, 5, True, outing.id, ("Alice", "Bob"), ("beach",)),
            ("2026-09-06", 11, 4, False, outing.id, ("Alice",), ("beach",)),
            ("2026-09-06", 12, 0, False, outing.id, (), ("beach",)),
            ("2026-09-06", 13, 0, False, outing.id, (), ()),   # the override
            ("2026-09-07", 10, 3, False, other.id, ("Bob",), ()),
            ("2026-09-07", 11, 0, False, other.id, (), ()),
            ("2026-09-07", 12, 0, False, other.id, (), ()),
            ("2025-12-24", 10, 0, False, None, (), ()),
            ("2025-12-24", 11, 0, False, None, (), ()),
        ]
        for index, (day, hour, rating, raw, session_id, people, tags) in enumerate(plan, 1):
            photo = catalog.photos.add(
                Photo(
                    content_hash=f"{index:064x}",
                    library_path=f"{day[:4]}/{day}/{day}_DSCF{index:04d}.jpg",
                    original_name=f"DSCF{index:04d}.JPG",
                    captured_at=dt.datetime.fromisoformat(f"{day}T{hour:02d}:00"),
                    imported_at=dt.datetime(2026, 9, 21, 12, 0),
                    rating=rating,
                    raw_path=f"{day[:4]}/{day}/{day}_DSCF{index:04d}.raf" if raw else None,
                    session_id=session_id,
                )
            )
            assert photo.id is not None
            ids[f"p{index}"] = photo.id
            for person in people:
                catalog.photos.add_person(photo.id, ids[person])
            for name in tags:
                catalog.photos.add_tag(photo.id, ids[name])

        catalog.photos.set_location(ids["p4"], ids["Bristol"])
    return ids


def grid(client: TestClient, params: str = "") -> dict:
    response = client.get(f"/api/library/photos?{params}" if params else "/api/library/photos")
    assert response.status_code == 200, response.text
    return response.json()


def rows(data: dict, dimension: str) -> dict[str, int]:
    return {row["label"]: row["count"] for row in data["facets"][dimension]}


@pytest.fixture
def library(settings: Settings) -> Iterator[TestClient]:
    """An app over the nine-photograph library above."""
    stock(settings)
    with client_for(settings) as open_client:
        yield open_client


def test_the_grid_is_the_whole_library_newest_first(library: TestClient) -> None:
    """FR-7.1, and the shape of the answer the page is built on."""
    data = grid(library)

    assert data["total"] == 9
    assert data["shown"] == 9
    assert data["more"] is False
    assert data["filtered"] is False
    assert [photo["captured_at"] for photo in data["photos"]] == sorted(
        (photo["captured_at"] for photo in data["photos"]), reverse=True
    )


def test_a_tile_carries_what_a_tile_draws_and_nothing_else(library: TestClient) -> None:
    """Pinned as an exact set, because the cost here is per photograph.

    `_photo_summary` asks the catalog four questions per frame — fine for the
    54 of an outing, 480 round trips for a page of this grid. People and tags
    are not on a tile, so they are not fetched; the place is, in one batched
    query. A field added here without thinking is a field fetched 120 times.
    """
    tile = grid(library)["photos"][0]

    assert set(tile) == {
        "id", "name", "captured_at", "captured_at_inferred",
        "rating", "has_raw", "location", "on_mac",
    }


def test_the_effective_location_reaches_the_tile(library: TestClient) -> None:
    """FR-3.4 all the way to the browser: the odd frame out says where it was."""
    by_name = {photo["name"]: photo["location"] for photo in grid(library)["photos"]}

    assert by_name["2026-09-06_DSCF0003.jpg"] == "St Aldo"
    assert by_name["2026-09-06_DSCF0004.jpg"] == "Bristol"   # the override
    assert by_name["2025-12-24_DSCF0008.jpg"] is None


def test_filtering_narrows_the_grid_and_the_counts_together(library: TestClient) -> None:
    """The acceptance criterion, in one test: filters apply, counts update."""
    everything = grid(library)
    assert rows(everything, "location") == {"St Aldo": 3, "Bristol": 4, "No location": 2}

    st_aldo = grid(library, "location=1")
    assert st_aldo["total"] == 3
    assert st_aldo["filtered"] is True
    # "3 of 9" needs both numbers, and the second one is not the first. Found
    # by breaking it: with nothing filtered the two are equal, so every test
    # that only ever asked unfiltered passed a version that conflated them.
    assert st_aldo["library"] == 9
    # Every other dimension is now counted inside St Aldo...
    assert rows(st_aldo, "tag") == {"beach": 3}
    assert rows(st_aldo, "person") == {"Alice": 2, "Nobody tagged": 1, "Bob": 1}
    # ...while its own dimension still offers the alternatives, because that
    # is what its counts are for (FR-7.4a).
    assert rows(st_aldo, "location") == {"St Aldo": 3, "Bristol": 4, "No location": 2}


def test_show_me_everything_from_st_aldo_rated_four_plus(library: TestClient) -> None:
    """Success criterion 3, as the URL a person could actually be sent."""
    data = grid(library, "location=1&rating=4")

    assert data["total"] == 2
    # Newest first, which is capture time and not rating — so the four-star
    # frame taken at 11:00 comes before the five-star one taken at 10:00.
    assert [photo["rating"] for photo in data["photos"]] == [4, 5]
    assert data["params"] == "location=1&rating=4"


def test_every_facet_count_is_what_clicking_it_gives_through_http(
    library: TestClient,
) -> None:
    """FR-7.4a end to end, and the reason the page can be as thin as it is.

    The count came from a GROUP BY and the total comes from running the link
    the same row carries. They are different code paths on both sides of the
    wire, and nothing in the browser is involved in keeping them equal.
    """
    checked = 0
    for dimension, values in grid(library)["facets"].items():
        for value in values:
            assert grid(library, value["params"])["total"] == value["count"], (
                f"{dimension} {value['label']!r}"
            )
            checked += 1
    assert checked >= 15


def test_a_filter_that_is_on_says_so_and_can_be_taken_off(library: TestClient) -> None:
    """Every filter that can go on has to come off, from the row that put it on."""
    data = grid(library, "location=1")
    picked = [row for row in data["facets"]["location"] if row["selected"]]

    assert [row["label"] for row in picked] == ["St Aldo"]
    assert grid(library, picked[0]["clear"])["total"] == 9
    assert grid(library, picked[0]["clear"])["filtered"] is False


def test_a_filter_survives_being_written_down_and_read_back(library: TestClient) -> None:
    """The query string the server emits is one the server can parse.

    Not a detail: the page keeps the whole filter in `location.search`, so a
    round trip that lost a field would lose it on every reload and on every
    link anybody sent anybody.
    """
    start = "location=1&location_none=true&person=1&rating=2&since=2026-01-01&until=2026-12-31&raw=false"
    once = grid(library, start)
    twice = grid(library, once["params"])

    assert once["params"] == twice["params"]
    assert once["total"] == twice["total"]
    assert [p["id"] for p in once["photos"]] == [p["id"] for p in twice["photos"]]


def test_an_event_called_none_is_not_the_absence_of_an_event(
    settings: Settings,
) -> None:
    """Why "unset" is its own parameter rather than a reserved word.

    Location, person and tag are ids, where no word could collide. An event is
    whatever somebody typed — and somebody may perfectly well run an event
    called "none". Spelling the four dimensions the same way costs four
    parameter names and removes the question entirely.
    """
    stock(settings)
    with Catalog.open(settings) as catalog:
        catalog.sessions.update(
            replace(catalog.sessions.get(2), event="none")  # type: ignore[arg-type]
        )

    with client_for(settings) as client:
        named = grid(client, "event=none")
        absent = grid(client, "event_none=true")

    assert named["total"] == 3      # the Bristol outing, now called "none"
    assert absent["total"] == 2     # the two frames in no session at all
    assert named["total"] != absent["total"]


def test_paging_walks_the_library_once(library: TestClient) -> None:
    """`more` is the page's only signal to keep going, so it has to be right
    at the boundary rather than one page past it."""
    seen: list[int] = []
    offset = 0
    while True:
        page = grid(library, f"limit=4&offset={offset}")
        seen.extend(photo["id"] for photo in page["photos"])
        if not page["more"]:
            break
        offset += page["shown"]

    assert len(seen) == len(set(seen)) == 9
    assert seen == [photo["id"] for photo in grid(library)["photos"]]


def test_a_page_says_how_many_there_are_not_how_many_it_sent(
    library: TestClient,
) -> None:
    page = grid(library, "limit=2")

    assert page["shown"] == 2
    assert page["total"] == 9
    assert page["library"] == 9
    assert page["more"] is True


def test_an_enormous_page_is_capped_rather_than_honoured(library: TestClient) -> None:
    """`limit` arrives from the browser, and an unbounded one is an invitation
    to build the whole library as JSON."""
    assert grid(library, "limit=100000")["limit"] == MAX_PAGE
    assert grid(library, "limit=0")["limit"] == 1
    assert grid(library, "offset=-5")["offset"] == 0


def test_a_filter_that_cannot_exist_is_refused_rather_than_answered_emptily(
    library: TestClient,
) -> None:
    """An empty grid would read as "you have no photographs like that" rather
    than as "that question is malformed" (FR-1.1a's lesson, one screen over)."""
    response = library.get("/api/library/photos?rating=9")

    assert response.status_code == 422
    assert response.json()["error"] == "bad-filter"
    assert "0-5" in response.json()["detail"]

    backwards = library.get("/api/library/photos?since=2026-09-07&until=2026-09-06")
    assert backwards.status_code == 422
    assert "never match" in backwards.json()["detail"]


def test_the_grid_before_there_is_a_library_is_its_own_answer(
    settings: Settings,
) -> None:
    with client_for(settings) as client:
        response = client.get("/api/library/photos")

    assert response.status_code == 404
    assert response.json()["error"] == "no-library"


def test_drawing_the_grid_writes_nothing(settings: Settings) -> None:
    """A read that left a row behind would be a surprise worth catching."""
    stock(settings)
    tables = ("photo", "session", "location", "person", "tag",
              "photo_person", "photo_tag", "photo_location")

    def counts() -> dict[str, int]:
        with Catalog.open(settings) as catalog:
            return {
                table: catalog.connection.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0]
                for table in tables
            }

    before = counts()
    with client_for(settings) as client:
        grid(client)
        grid(client, "location=1&rating=4")
        grid(client, "location_none=true")
    assert counts() == before


# ----------------------------------------------------------------------
# The detail view (WU-4.3)
# ----------------------------------------------------------------------
#
# FR-7.5: one photograph, everything known about it, and all of it editable.
# Two things carry the weight.
#
# **An edit is not saved until it is in both stores.** The catalog is an index
# (NFR-2) and the sidecar is what survives this app, so these tests read the
# .xmp back off disk rather than trusting the 200.
#
# **Stepping walks the filter.** Which frame is next is asked of the server
# against the filter the grid was drawn with, so a link to one photograph of a
# filtered view opens knowing where it sits in it with no grid loaded at all.


def on_disk(settings: Settings, count: int = 3, *, session: bool = True) -> list[int]:
    """A small library whose photographs are really there.

    `stock` above is rows without files, which is all the grid needs. A
    preview is a decode and a sidecar is a file, so this one copies real
    fixture bytes into the library root.
    """
    from photo_manager.sessions import apply_proposal, propose

    with Catalog.open(settings) as catalog:
        ids: list[int] = []
        for index in range(1, count + 1):
            relative = f"2026/2026-09-06/2026-09-06_DSCF{index:04d}.jpg"
            destination = settings.library_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(fixtures.JPEG_WITHOUT_RAW.read_bytes())
            photo = catalog.photos.add(
                Photo(
                    content_hash=f"{index:064x}",
                    library_path=relative,
                    original_name=f"DSCF{index:04d}.JPG",
                    captured_at=dt.datetime(2026, 9, 6, 10, index),
                    imported_at=dt.datetime(2026, 9, 21, 12, 0),
                    camera_model=fixtures.CAMERA_MODEL,
                    lens=fixtures.LENS_MODEL,
                    width=6240,
                    height=4160,
                )
            )
            assert photo.id is not None
            ids.append(photo.id)
        if session:
            apply_proposal(catalog, propose(catalog)[0].as_one_block())
    return ids


@pytest.fixture
def frames(settings: Settings) -> Iterator[tuple[TestClient, list[int]]]:
    """An app over three photographs that exist on disk, grouped as one outing."""
    ids = on_disk(settings)
    with client_for(settings) as open_client:
        yield open_client, ids


def detail(client: TestClient, photo_id: int, params: str = "") -> dict:
    response = client.get(
        f"/api/library/photos/{photo_id}?{params}" if params
        else f"/api/library/photos/{photo_id}"
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_the_detail_view_carries_everything_the_catalog_knows(
    frames: tuple[TestClient, list[int]],
) -> None:
    """Pinned as an exact set, the same way the tile is.

    The tile's set is pinned because every field costs 120 fetches; this one
    is pinned for the opposite reason — it is the contract the panel draws,
    and something quietly dropped from it is a field that stops being
    editable with nothing going red.
    """
    client, ids = frames

    body = detail(client, ids[0])

    assert set(body) == {
        # what the frame is
        "id", "name", "original_name", "library_path", "raw_path", "bytes",
        "width", "height", "captured_at", "captured_at_inferred", "imported_at",
        # what the camera said
        "camera_model", "lens", "exposure", "f_number", "iso", "focal_length",
        # what a person said — the editable half
        "rating", "location", "override", "people", "tags", "has_raw",
        # what its outing says, and how much of it there is
        "session_id", "session_location", "session_photographs", "event",
        # the other store (D-1), and where this sits in the filter
        "sidecar", "neighbours", "params",
        # where its files are: this Mac, the archive drive
        "archive", "archived_at", "on_mac", "keep_on_mac",
    }
    assert body["camera_model"] == fixtures.CAMERA_MODEL
    assert body["bytes"] == fixtures.JPEG_WITHOUT_RAW.stat().st_size
    assert body["session_photographs"] == 3


def test_the_detail_view_knows_where_it_sits_in_the_filter(
    library: TestClient,
) -> None:
    """A link to one frame of a filtered view opens knowing "2 of 7"."""
    whole = grid(library)
    ordered = [photo["id"] for photo in whole["photos"]]

    for index, photo_id in enumerate(ordered, 1):
        around = detail(library, photo_id)["neighbours"]
        assert around["position"] == index
        assert around["total"] == len(ordered)
        assert around["previous"] == (ordered[index - 2] if index > 1 else None)
        assert around["next"] == (ordered[index] if index < len(ordered) else None)


def test_stepping_through_a_filtered_view_never_leaves_it(library: TestClient) -> None:
    """Every frame the arrow keys can reach is one the filter matches."""
    filtered = grid(library, "rating=3")
    inside = {photo["id"] for photo in filtered["photos"]}
    assert len(inside) == 3

    walked = []
    at: int | None = filtered["photos"][0]["id"]
    while at is not None:
        walked.append(at)
        at = detail(library, at, "rating=3")["neighbours"]["next"]

    assert set(walked) == inside
    assert walked == [photo["id"] for photo in filtered["photos"]]


def test_a_frame_rated_out_of_its_own_filter_is_told_rather_than_hidden(
    frames: tuple[TestClient, list[int]],
) -> None:
    """Rate a 4 down to 2 while looking at "4+" and it is out of the set but
    still on screen. That is a state, and saying it is better than a number
    the photograph does not have."""
    client, ids = frames
    client.put(f"/api/library/photos/{ids[0]}/tags", json={"rating": 4})
    assert detail(client, ids[0], "rating=4")["neighbours"]["position"] == 1

    client.put(f"/api/library/photos/{ids[0]}/tags", json={"rating": 2})

    around = detail(client, ids[0], "rating=4")["neighbours"]
    assert around["position"] is None
    assert around["total"] == 0


def test_the_detail_view_filter_is_the_grid_filter(library: TestClient) -> None:
    """Both read the query string through one definition, so a filter cannot
    mean one thing to the tiles and another to the arrow keys."""
    params = "rating=3&raw=false"
    photo_id = grid(library, params)["photos"][0]["id"]

    assert detail(library, photo_id, params)["params"] == grid(library, params)["params"]


def test_an_edit_from_the_detail_view_lands_in_both_stores(
    frames: tuple[TestClient, list[int]], settings: Settings
) -> None:
    """FR-5.6 through the path the page actually uses. The .xmp is read back
    off disk, because a tag that reached only the database is a tag a rebuild
    would lose (NFR-2, success criterion 4)."""
    from photo_manager.sidecars import read_sidecar

    client, ids = frames

    body = client.put(
        f"/api/library/photos/{ids[1]}/tags",
        json={"rating": 5, "location": "Penzance", "people": ["Alice"], "tags": ["beach"]},
    ).json()

    assert body["rating"] == 5
    assert body["override"] == "Penzance"
    assert body["sidecar"] is not None
    with Catalog.open(settings) as catalog:
        photo = catalog.photos.get(ids[1])
        assert photo is not None
        written = read_sidecar(catalog.absolute_path(photo))
    assert written.rating == 5
    assert written.location == "Penzance"
    assert written.people == ("Alice",)
    assert written.tags == ("beach",)


def test_the_outings_fields_reach_every_frame_and_the_frames_do_not(
    frames: tuple[TestClient, list[int]],
) -> None:
    """The reason the detail view has two forms. An event is the outing's and
    is fifty-four photographs; a rating is this frame's and is one."""
    client, ids = frames
    session_id = detail(client, ids[0])["session_id"]

    client.put(f"/api/library/sessions/{session_id}/tags", json={"event": "Beach day"})
    client.put(f"/api/library/photos/{ids[0]}/tags", json={"rating": 4})

    assert [detail(client, i)["event"] for i in ids] == ["Beach day"] * 3
    assert [detail(client, i)["rating"] for i in ids] == [4, 0, 0]


def test_an_ungrouped_frame_has_no_outing_to_edit(settings: Settings) -> None:
    """Which is every photograph in the real library today: nothing creates a
    session at import time (FR-3.2a), so the detail view has to say so rather
    than offer an event field that would go nowhere."""
    ids = on_disk(settings, 1, session=False)
    with client_for(settings) as client:
        body = detail(client, ids[0])

    assert body["session_id"] is None
    assert body["event"] is None
    assert body["session_photographs"] == 0


def test_an_override_and_its_outings_place_are_two_different_statements(
    frames: tuple[TestClient, list[int]],
) -> None:
    """FR-3.4 is precedence, not replacement, so both are sent and clearing
    the override gives the outing's answer back rather than leaving a hole."""
    client, ids = frames
    session_id = detail(client, ids[0])["session_id"]
    client.put(f"/api/library/sessions/{session_id}/tags", json={"location": "St Aldo"})

    client.put(f"/api/library/photos/{ids[0]}/tags", json={"location": "Penzance"})
    overridden = detail(client, ids[0])
    client.put(f"/api/library/photos/{ids[0]}/tags", json={"location": ""})
    cleared = detail(client, ids[0])

    assert (overridden["location"], overridden["override"]) == ("Penzance", "Penzance")
    assert overridden["session_location"] == "St Aldo"
    assert (cleared["location"], cleared["override"]) == ("St Aldo", None)


# ----------------------------------------------------------------------
# The preview the detail view is looked at on
# ----------------------------------------------------------------------


def test_the_library_preview_is_the_big_one_and_is_cached(
    frames: tuple[TestClient, list[int]],
) -> None:
    """FR-7.5's full-size view, FR-2.3's cache. A miss is a decode; everything
    after it is the file being handed back."""
    client, ids = frames

    first = client.get(f"/api/library/photos/{ids[0]}/preview")
    second = client.get(f"/api/library/photos/{ids[0]}/preview")
    tile = client.get(f"/api/library/photos/{ids[0]}/thumbnail")

    assert first.status_code == 200
    assert first.headers["content-type"] == "image/jpeg"
    assert first.headers["X-Thumbnail-Tier"] == "preview"
    assert first.headers["X-Thumbnail-Cache"] == "miss"
    assert second.headers["X-Thumbnail-Cache"] == "hit"
    assert first.content == second.content
    # Two sizes of one photograph, in one cache, neither evicting the other.
    assert len(first.content) > len(tile.content)


def test_the_preview_is_the_same_render_the_culling_view_uses(
    frames: tuple[TestClient, list[int]], settings: Settings
) -> None:
    """Keyed by content hash at `preview_max_px`, so a frame judged on the
    card is already warm in the library (FR-2.3)."""
    client, ids = frames
    client.get(f"/api/library/photos/{ids[0]}/preview")

    cached = ThumbnailCache(settings.thumbnail_cache_dir, max_px=settings.preview_max_px)

    assert cached.cached(f"{1:064x}") is not None


def test_a_row_whose_file_has_gone_is_a_404_with_a_sentence(
    library: TestClient,
) -> None:
    """The `stock` library is rows without files, which is exactly this case.
    A 500 would say "something broke"; this says which file is missing."""
    photo_id = grid(library)["photos"][0]["id"]

    for path in (f"/api/library/photos/{photo_id}/preview",
                 f"/api/library/photos/{photo_id}/thumbnail"):
        response = library.get(path)
        assert response.status_code == 404, path
        assert "not on disk" in response.json()["detail"]


def test_an_unknown_id_is_a_404_on_the_preview_too(library: TestClient) -> None:
    response = library.get("/api/library/photos/9999/preview")
    assert response.status_code == 404
    assert response.json()["error"] == "not-found"


def test_the_preview_is_cacheable_by_the_browser(
    frames: tuple[TestClient, list[int]],
) -> None:
    """Keyed by content hash and the bytes never change, so this one really is
    immutable — unlike a card tile, which is only immutable for one scan."""
    client, ids = frames

    headers = client.get(f"/api/library/photos/{ids[0]}/preview").headers

    assert "immutable" in headers["Cache-Control"]


# ----------------------------------------------------------------------
# The page itself
# ----------------------------------------------------------------------


def test_the_library_page_can_be_followed_into_a_photograph(
    library: TestClient,
) -> None:
    """WU-4.2 left a grid you could look at but not follow. This is the unit
    that fixes it, so the tile is a button and it opens the detail view."""
    page = library.get("/library").text
    script = _frontend("library.js")

    assert 'id="detail"' in page
    assert 'class="card__open"' in page
    assert "openDetail(photo.id)" in script


def test_the_page_steps_with_the_ids_the_server_gave_it(library: TestClient) -> None:
    """The same rule as the facet links one screen back: the browser follows
    what the server computed rather than working out the next frame from the
    page it happens to have loaded."""
    script = _frontend("library.js")

    assert "neighbours.previous" in script
    assert "neighbours.next" in script
    # And it prefetches them, because a cold preview is ~450 ms of decode.
    assert "prefetchNeighbours" in script
    assert "new Image()" in script


def test_the_page_binds_the_keys_the_detail_view_needs(library: TestClient) -> None:
    script = _frontend("library.js")

    for key in ("ArrowLeft", "ArrowRight", "Escape"):
        assert key in script, key
    # Rating from the keyboard, but never while something is being typed into.
    assert "[0-5]" in script
    assert 'matches("input, textarea, select")' in script


def test_the_page_puts_the_frame_in_the_url_without_burying_the_filters(
    library: TestClient,
) -> None:
    """A photograph is a link like a filter is — but stepping *replaces* the
    entry, because Back belongs to the filters. Forty frames should not be
    forty presses of Back."""
    script = _frontend("library.js")

    assert 'params.set("photo"' in script
    assert "history.replaceState" in script
    # And `photo` is never sent to the grid endpoint as if it were a filter.
    assert 'params.delete("photo")' in script


def test_the_page_says_how_many_photographs_the_outings_fields_would_touch(
    library: TestClient,
) -> None:
    """One of the two forms is fifty-four photographs (FR-3.3). That has to be
    readable before Save, not discoverable afterwards."""
    page = library.get("/library").text
    script = _frontend("library.js")

    assert 'id="outing-scope"' in page
    assert "session_photographs" in script


def test_the_detail_endpoints_take_no_path_either(settings: Settings) -> None:
    """Re-run after this batch of routes, like every batch before it."""
    test_no_route_accepts_a_filesystem_path(settings)


def test_the_page_sends_only_what_changed(library: TestClient) -> None:
    """FR-5.6b. Opening a frame, looking at it and pressing Save must not
    clear the fields nobody touched."""
    script = _frontend("library.js")

    assert "state.initial" in script
    assert "Nothing changed." in script


def test_the_grid_endpoint_takes_no_path_either(settings: Settings) -> None:
    """Re-run after this batch of routes, like every batch before it."""
    test_no_route_accepts_a_filesystem_path(settings)


# ----------------------------------------------------------------------
# The backlog (WU-4.4)
# ----------------------------------------------------------------------
#
# FR-7.6's two halves. **Correctly identifies untagged photographs** is
# `queries._UNTAGGED` and is tested at that layer; here it is the second half
# that matters — **tagging from this view removes them from it** — which means
# the view has to carry the action, and the action has to be a whole day at
# once (FR-3.3) rather than a photograph at a time.


def a_day(
    catalog: Catalog,
    day: str,
    hours: tuple[int, ...],
    *,
    session_id: int | None = None,
    start: int = 1,
) -> list[int]:
    """Photographs on one day, with real bytes, optionally already grouped."""
    ids: list[int] = []
    for offset, hour in enumerate(hours):
        index = start + offset
        relative = f"{day[:4]}/{day}/{day}_DSCF{index:04d}.jpg"
        destination = catalog.settings.library_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(fixtures.JPEG_WITHOUT_RAW.read_bytes())
        photo = catalog.photos.add(
            Photo(
                content_hash=f"{index:064x}",
                library_path=relative,
                original_name=f"DSCF{index:04d}.JPG",
                captured_at=dt.datetime.fromisoformat(f"{day}T{hour:02d}:00"),
                imported_at=dt.datetime(2026, 9, 21, 12, 0),
                session_id=session_id,
            )
        )
        assert photo.id is not None
        ids.append(photo.id)
    return ids


@pytest.fixture
def backlog(settings: Settings) -> Iterator[TestClient]:
    """Three days, one of each kind of not-done.

    * **2026-09-08** — two frames five hours apart and nobody has decided the
      shape of the day, so it is not this screen's to decide (FR-3.2).
    * **2026-09-07** — two frames an hour apart, ungrouped, nothing to decide.
    * **2026-09-06** — already one outing, and nobody has named it.
    """
    with Catalog.open(settings) as catalog:
        session = catalog.sessions.add(
            Session(
                started_at=dt.datetime(2026, 9, 6, 10),
                ended_at=dt.datetime(2026, 9, 6, 12),
            )
        )
        assert session.id is not None
        a_day(catalog, "2026-09-06", (10, 11, 12), session_id=session.id, start=1)
        a_day(catalog, "2026-09-07", (10, 11), start=4)
        a_day(catalog, "2026-09-08", (9, 14), start=6)
    with client_for(settings) as open_client:
        yield open_client


def triage(client: TestClient) -> dict:
    response = client.get("/api/library/triage")
    assert response.status_code == 200, response.text
    return response.json()


def on_day(body: dict, day: str) -> list[dict]:
    """Every row for one date — plural now, because a split day is two."""
    return [unit for unit in body["units"] if unit["day"] == day]


def one_on(body: dict, day: str) -> dict:
    rows = on_day(body, day)
    assert len(rows) == 1, f"{day} has {len(rows)} rows, expected one"
    return rows[0]


def test_the_backlog_is_everything_with_something_left_to_say(
    backlog: TestClient,
) -> None:
    body = triage(backlog)

    assert body["counts"] == {
        "photographs": 7, "untagged": 7, "tagged": 0, "units": 3, "days": 3,
    }
    assert [unit["day"] for unit in body["units"]] == [
        "2026-09-08", "2026-09-07", "2026-09-06",
    ]
    # A row says which part of its day it is, which is the only thing telling
    # two rows of one date apart.
    assert one_on(body, "2026-09-06")["started_at"].endswith("T10:00")
    assert one_on(body, "2026-09-06")["ended_at"].endswith("T12:00")


def test_each_row_carries_the_action_that_would_clear_it(
    backlog: TestClient,
) -> None:
    """Three answers, decided by the server rather than re-derived in the
    browser from three counts — and every one of them finishable here."""
    body = triage(backlog)

    assert one_on(body, "2026-09-06")["action"] == "tag"
    assert one_on(body, "2026-09-06")["session_id"] is not None
    assert one_on(body, "2026-09-07")["action"] == "group-and-tag"
    assert one_on(body, "2026-09-07")["session_id"] is None

    # A five-hour gap is more than FR-3.2's three, so there is a real question
    # in this one — and the row carries what it needs to ask it.
    gap = one_on(body, "2026-09-08")
    assert gap["action"] == "shape"
    assert len(gap["blocks"]) == 2
    assert [block["photographs"] for block in gap["blocks"]] == [1, 1]
    assert gap["blocks"][1]["gap_before"] == 5 * 3600


@pytest.mark.parametrize(("one_session", "outings"), [(True, 1), (False, 2)])
def test_the_shape_question_is_answered_here_either_way(
    backlog: TestClient, one_session: bool, outings: int
) -> None:
    """The refactor's point. The old screen refused this row and sent the
    reader to /tag; both answers are now one request from this one, and each
    gets its own library because the first answer settles the day."""
    assert one_on(triage(backlog), "2026-09-08")["action"] == "shape"

    created = backlog.post(
        "/api/library/sessions",
        json={"day": "2026-09-08", "one_session": one_session},
    ).json()["created"]

    assert len(created) == outings
    # ... and what comes back is rows that can be named, not a dead end.
    after = on_day(triage(backlog), "2026-09-08")
    assert len(after) == outings
    assert {unit["action"] for unit in after} == {"tag"}
    assert all(unit["session_id"] is not None for unit in after)


def test_a_day_split_in_two_is_two_things_to_name(backlog: TestClient) -> None:
    """And naming one of them leaves the other, which is what the old
    day-keyed list could not express at all."""
    backlog.post("/api/library/sessions",
                 json={"day": "2026-09-08", "one_session": False})
    rows = on_day(triage(backlog), "2026-09-08")
    assert len(rows) == 2

    backlog.put(f"/api/library/sessions/{rows[0]['session_id']}/tags",
                json={"location": "St Aldo"})

    left = on_day(triage(backlog), "2026-09-08")
    assert [unit["session_id"] for unit in left] == [rows[1]["session_id"]]


def test_naming_an_outing_takes_it_out_of_the_backlog(
    backlog: TestClient,
) -> None:
    """The acceptance criterion, for the row that is already an outing."""
    session_id = one_on(triage(backlog), "2026-09-06")["session_id"]

    backlog.put(
        f"/api/library/sessions/{session_id}/tags", json={"location": "St Aldo"}
    )

    body = triage(backlog)
    assert on_day(body, "2026-09-06") == []
    assert body["counts"] == {
        "photographs": 7, "untagged": 4, "tagged": 3, "units": 2, "days": 2,
    }


def test_a_day_nobody_has_shaped_is_grouped_and_named_in_one_act(
    backlog: TestClient, settings: Settings
) -> None:
    """What the button on an ungrouped row does, in the two requests the page
    makes — and then the whole way down to the sidecars, because a day that
    left this list without reaching disk would not really be tagged."""
    from photo_manager.sidecars import read_sidecar

    created = backlog.post(
        "/api/library/sessions", json={"day": "2026-09-07", "one_session": True}
    ).json()["created"]
    assert len(created) == 1

    backlog.put(
        f"/api/library/sessions/{created[0]['id']}/tags",
        json={"location": "Bristol", "event": "Harbour walk"},
    )

    assert on_day(triage(backlog), "2026-09-07") == []
    with Catalog.open(settings) as catalog:
        for photo in catalog.photos.in_session(created[0]["id"]):
            written = read_sidecar(catalog.absolute_path(photo))
            assert (written.location, written.event) == ("Bristol", "Harbour walk")


def test_working_the_backlog_empties_it(backlog: TestClient) -> None:
    """Every day, in the order the screen offers them, until there is nothing
    left — which is the only way to be sure the list is the work rather than a
    view that happens to look like it."""
    for day, one_session in (("2026-09-06", None), ("2026-09-07", True),
                             ("2026-09-08", True)):
        row = on_day(triage(backlog), day)[0]
        session_id = row["session_id"]
        if session_id is None:
            session_id = backlog.post(
                "/api/library/sessions",
                json={"day": day, "one_session": one_session},
            ).json()["created"][0]["id"]
        backlog.put(f"/api/library/sessions/{session_id}/tags",
                    json={"location": "St Aldo"})

    body = triage(backlog)
    assert body["units"] == []
    assert body["counts"]["untagged"] == 0
    assert body["counts"]["tagged"] == 7


def test_a_row_shows_a_few_of_its_own_frames(backlog: TestClient) -> None:
    """So a row says what it was, not only when it was."""
    body = triage(backlog)

    assert len(one_on(body, "2026-09-06")["covers"]) == 3
    for photo_id in one_on(body, "2026-09-06")["covers"]:
        assert backlog.get(f"/api/library/photos/{photo_id}/thumbnail").status_code == 200


def test_a_row_links_to_its_day_in_the_grid(backlog: TestClient) -> None:
    """"Look at these properly" is one click and not a filter to assemble."""
    row = one_on(triage(backlog), "2026-09-07")

    data = grid(backlog, row["params"])

    assert data["total"] == 2
    assert {photo["captured_at"][:10] for photo in data["photos"]} == {"2026-09-07"}


def test_untagged_is_a_filter_like_any_other(library: TestClient) -> None:
    """FR-7.2b's four unset values, asked as the one question they add up to."""
    whole = grid(library)
    backlog_only = grid(library, "untagged=true")

    assert backlog_only["total"] == 2
    assert backlog_only["filtered"] is True
    assert backlog_only["total"] < whole["total"]
    # Counted with its own constraint lifted, because clicking the other one
    # switches to it rather than narrowing (FR-7.4a) — so the row offering the
    # other 7 keeps saying 7 while you are looking at the 2.
    assert rows(backlog_only, "triage") == {"Nothing said yet": 2, "Something said": 7}


def test_the_backlog_filter_survives_being_written_down_and_read_back(
    library: TestClient,
) -> None:
    """The same round trip every other filter makes, so a triage view is a
    link (FR-7.1a)."""
    first = grid(library, "untagged=true&rating=0")

    assert grid(library, first["params"])["total"] == first["total"]
    assert "untagged=true" in first["params"]


def test_the_backlog_before_there_is_a_library_is_its_own_answer(
    settings: Settings,
) -> None:
    with client_for(settings) as client:
        response = client.get("/api/library/triage")

    assert response.status_code == 404
    assert response.json()["error"] == "no-library"


def test_drawing_the_backlog_writes_nothing(backlog: TestClient) -> None:
    """It is a screen about work still to do, not a screen that does any."""
    before = triage(backlog)

    for _ in range(3):
        triage(backlog)

    assert triage(backlog) == before


def test_the_triage_endpoint_takes_no_path_either(settings: Settings) -> None:
    """Re-run after this batch of routes, like every batch before it."""
    test_no_route_accepts_a_filesystem_path(settings)


def test_the_old_triage_and_tag_pages_land_in_the_library(backlog: TestClient) -> None:
    """Five screens became two. Bookmarks to the old ones still work."""
    for old, new in (("/triage", "/library?view=totag"), ("/tag", "/library?view=outings")):
        response = backlog.get(old, follow_redirects=False)
        assert response.status_code in (302, 307), old
        assert response.headers["location"] == new
    page = backlog.get("/triage").text
    assert "totag.js" in page
    assert 'id="view-totag"' in page
    assert 'id="view-outings"' in page


def test_with_no_card_the_app_opens_on_the_library(settings: Settings) -> None:
    """No card path configured (the usual case) and nothing removable attached:
    there is no card workflow to show, so / is the Library."""
    settings = replace(settings, card_dcim_root=None)
    with client_for(settings) as client:
        response = client.get("/", follow_redirects=False)
        status = client.get("/api/status").json()["card"]["status"]
    assert status == "no-media"
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/library"


def test_a_configured_card_path_that_is_missing_is_still_said_on_the_card_page(
    settings: Settings, tmp_path: Path
) -> None:
    """Only "no card at all" goes to the Library. A card_dcim_root that is not
    there is a problem with a sentence, and the card page is where it is said."""
    settings = replace(settings, card_dcim_root=tmp_path / "nowhere" / "DCIM")
    with client_for(settings) as client:
        response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert 'id="status-message"' in response.text


def test_the_page_offers_a_day_at_a_time_and_never_a_frame_at_a_time(
    backlog: TestClient,
) -> None:
    """The design failure this list exists to avoid is typing a location
    fifty-four times (FR-3.3), and a backlog with a form per photograph would
    be exactly that with a progress bar on top."""
    script = _frontend("totag.js")

    assert "/api/library/sessions/" in script
    assert "Apply to all " in script
    # Nothing here tags one photograph; that is the detail view's job.
    assert "/tags" in script
    assert "/api/library/photos/${" not in script.replace(
        "/api/library/photos/${id}/thumbnail", "")


def test_the_page_follows_the_action_the_server_named(backlog: TestClient) -> None:
    """The same rule as the facet links: the browser does what the server
    worked out rather than re-deriving a rule about sessions from counts."""
    script = _frontend("totag.js")

    for action in ("group-and-tag", "shape"):
        assert action in script, action
    assert "unit.session_id" in script


def test_the_page_offers_both_answers_to_a_gap(backlog: TestClient) -> None:
    """The refactor: asking is not deciding, and refusing to ask was the wrong
    end of the same stick."""
    script = _frontend("totag.js")

    assert "backlog__split" in script
    assert "backlog__one" in script
    assert "one_session: asOne" in script
    # The one shape it still cannot express, offered where it could be wanted.
    assert "blocks.length < 3" in script


def test_apply_to_all_waits_for_every_gap_question_to_be_answered() -> None:
    """Applying to "all" of a day nobody has shaped would be answering the
    shape question for them."""
    script = _frontend("totag.js")

    assert "applyAll" in script
    assert "disabled = shaping > 0" in script


def test_the_outings_view_says_what_it_is_now_for(backlog: TestClient) -> None:
    """Two views that both name an outing need to say which is which, or
    the overlap is just confusion with a tab bar."""
    page = backlog.get("/library").text

    assert 'href="/library?view=totag"' in page
    assert "already named" in page
    assert "Edit outing" in page
    script = _frontend("outings.js")
    # Its own names, so it cannot collide with the grid's script on one page.
    assert script.lstrip().startswith("/*")
    assert "window.Outings = (() => {" in script


def test_the_page_reloads_the_backlog_after_every_apply(
    backlog: TestClient,
) -> None:
    """Which is what makes the list empty as it is worked, rather than a row
    being hidden by the page while the server still holds it."""
    script = _frontend("totag.js")

    assert "await refresh();" in script
    assert "refresh: () => loadToTag({ draw: true })" in _frontend("library.js")


def test_the_page_says_what_untagged_means(backlog: TestClient) -> None:
    """The definition was the hard part of this unit, so the screen carries
    it rather than leaving it in a requirements document."""
    page = backlog.get("/library?view=totag").text

    assert "no place" in page
    assert "A star is not a description" in page


def test_the_backlog_is_reachable_from_every_other_screen(
    backlog: TestClient,
) -> None:
    """FR-7.6 says first-class, and a view nobody can find is not that. The
    top bar is Card and Library; To tag is a tab of the Library, and the card
    workflow's Tag step is the same list narrowed to the card."""
    assert 'href="/library?view=totag"' in backlog.get("/library").text
    assert 'href="/library"' in _frontend("strip.js")
    assert "/static/totag.js" in _frontend("index.html")
    assert "ToTag.mount" in _frontend("card.js")
    assert "ToTag.mount" in _frontend("library.js")


def test_the_stylesheet_draws_a_day_that_cannot_be_cleared_from_here(
    backlog: TestClient,
) -> None:
    """A row whose shape is a judgement call is not one you can clear with the
    button, and should not look like one."""
    styles = _frontend("app.css")

    assert ".backlog__state--shape" in styles


# ----------------------------------------------------------------------
# The page
# ----------------------------------------------------------------------


def test_the_library_page_is_served(library: TestClient) -> None:
    response = library.get("/library")

    assert response.status_code == 200
    assert "library.js" in response.text


def test_the_page_follows_the_links_the_server_sends_rather_than_building_them(
    library: TestClient,
) -> None:
    """The thing that keeps FR-7.4a true once a browser is involved.

    If the page built its own query strings, a count computed by the server
    and a filter built by the page could disagree about what clicking meant —
    and nothing would notice, because both would look reasonable. So it uses
    `value.params` and `value.clear`, and the assertion below is that it does.
    """
    script = _frontend("library.js")

    assert "load(value.params)" in script
    assert "load(value.clear)" in script
    # And no hand-rolled filter building: no dimension name is ever turned
    # into a parameter in the browser.
    for dimension in ("location=", "rating=", "person=", "tag=", "event="):
        assert dimension not in script, dimension


def test_the_page_keeps_the_filter_in_the_url(library: TestClient) -> None:
    """So Back works, a filtered view is a link, and reloading keeps it."""
    script = _frontend("library.js")

    assert "history.pushState" in script
    assert "popstate" in script
    assert "location.search" in script


def test_the_page_draws_a_lone_facet_value_rather_than_hiding_it(
    library: TestClient,
) -> None:
    """On the real library every dimension has exactly one value — "No
    location, 300" — and that is the answer, not noise to be tidied away."""
    script = _frontend("library.js")

    assert "values.length === 1" not in script
    assert 'if (!values.length) continue;' in script


def test_the_page_marks_a_guessed_date_as_guessed(library: TestClient) -> None:
    """FR-8.2a reaches the grid: a date from mtime must not look like one read
    from EXIF, because the whole point of the flag is that it can be corrected."""
    script = _frontend("library.js")
    styles = _frontend("app.css")

    assert "captured_at_inferred" in script
    assert "card__when--inferred" in styles


# ----------------------------------------------------------------------
# The card's mount, and backups — the strip above every page
# ----------------------------------------------------------------------


@pytest.fixture
def writable_card(monkeypatch: pytest.MonkeyPatch) -> dict:
    """A card macOS has mounted read-write, until something remounts it."""
    mount = {"writable": True, "remounts": 0}
    monkeypatch.setattr(cards, "card_writable", lambda path: mount["writable"])
    monkeypatch.setattr(web_state, "card_writable", lambda path: mount["writable"])

    def remount(path: Path) -> None:
        mount["remounts"] += 1
        mount["writable"] = False

    monkeypatch.setattr(web_state, "remount_read_only", remount)
    return mount


def test_a_read_write_card_is_named_and_culling_and_importing_wait(
    settings: Settings, card: Path, writable_card: dict
) -> None:
    with cull_client(settings, card, "DSCF0001.JPG", "DSCF0002.JPG") as client:
        status = client.get("/api/status").json()["card"]
        scan_id = client.get("/api/card").json()["scan_id"]
        decided = decide(client, scan_id, 0, verdict="keep")
        imported = client.post(f"/api/card/{scan_id}/import")

    assert status["writable"] is True and "read-write" in status["message"]
    assert decided.status_code == 409 and decided.json()["error"] == "card-writable"
    assert imported.status_code == 409 and imported.json()["error"] == "card-writable"


def test_remounting_makes_the_card_safe_and_rescans_it(
    settings: Settings, card: Path, writable_card: dict
) -> None:
    with cull_client(settings, card, "DSCF0001.JPG") as client:
        before = client.get("/api/card").json()["scan_id"]
        remounted = client.post("/api/card/remount")
        after = remounted.json()["scan_id"]
        decided = decide(client, after, 0, verdict="keep")
        status = client.get("/api/status").json()["card"]

    assert remounted.status_code == 200 and writable_card["remounts"] == 1
    assert after != before  # the old scan's URLs are stale, and say so
    assert decided.status_code == 200
    assert status["writable"] is False


def test_a_remount_that_fails_says_why(
    settings: Settings, card: Path, writable_card: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuses(path: Path) -> None:
        raise cards.CardError("The card could not be remounted read-only: busy.")

    monkeypatch.setattr(web_state, "remount_read_only", refuses)
    with cull_client(settings, card, "DSCF0001.JPG") as client:
        response = client.post("/api/card/remount")
    assert response.status_code == 409 and "busy" in response.json()["detail"]


def test_with_no_backup_root_the_strip_says_how_to_set_one(
    settings: Settings, card: Path
) -> None:
    with client_for(settings) as client:
        status = client.get("/api/status").json()["backup"]
        started = client.post("/api/backup")
    assert status["configured"] is False and "backup_root" in status["message"]
    assert started.status_code == 409 and started.json()["error"] == "backup-refused"


def test_an_unplugged_backup_drive_is_refused_before_anything_starts(
    settings: Settings, card: Path, tmp_path: Path
) -> None:
    settings = replace(settings, backup_root=tmp_path / "Volumes" / "Gone" / "PMB")
    with client_for(settings) as client:
        response = client.post("/api/backup")
        status = client.get("/api/status").json()["backup"]
    assert response.status_code == 409 and "not attached" in response.json()["detail"]
    assert status["attached"] is False and status["progress"]["status"] == "idle"


@pytest.fixture
def backup_drive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from photo_manager import backup
    root = tmp_path / "Volumes" / "Backup"
    root.mkdir(parents=True)
    real = backup.device
    monkeypatch.setattr(
        backup, "device",
        lambda path: -1 if path == root or root in path.parents else real(path),
    )
    private = tmp_path / "private"
    private.mkdir()
    (private / "PLAN.md").write_text("plan", encoding="utf-8")
    monkeypatch.setattr(backup, "PRIVATE_DIR", private)
    return root


def test_the_backup_button_takes_a_verified_snapshot_and_the_strip_updates(
    settings: Settings, card: Path, backup_drive: Path
) -> None:
    from photo_manager.backup import verify_snapshot
    settings = replace(settings, backup_root=backup_drive / "PMB")
    seed(settings, content_hash=content_hash(fixtures.JPEG_WITHOUT_RAW),
         library_path="2026/2026-01/a.jpg")
    (settings.library_root / "2026" / "2026-01").mkdir(parents=True, exist_ok=True)
    shutil.copy2(fixtures.JPEG_WITHOUT_RAW, settings.library_root / "2026" / "2026-01" / "a.jpg")

    with client_for(settings) as client:
        before = client.get("/api/status").json()["backup"]
        started = client.post("/api/backup").json()
        deadline = time.monotonic() + 30
        while (progress := client.get("/api/backup").json())["progress"]["status"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        after = client.get("/api/status").json()["backup"]

    assert before["attached"] is True and before["last_snapshot"] is None
    assert started["progress"]["status"] in ("running", "done")
    assert progress["progress"]["status"] == "done", progress["progress"]["error"]
    assert after["last_snapshot"] == progress["progress"]["result"]["snapshot"]
    assert after["photos_not_backed_up"] == 0
    [snapshot] = (backup_drive / "PMB" / settings.library_root.name).iterdir()
    assert verify_snapshot(snapshot)[1] == []


def test_the_app_says_what_quitting_would_cut_short(settings: Settings, card: Path) -> None:
    """Asked by the Mac app before it stops the server (tools/make_mac_app.py)."""
    state = AppState(settings)
    with TestClient(create_app(settings, state=state)) as client:
        put(card, "DSCF0001.JPG", fixtures.BURST_SECOND)
        client.get("/api/card")
        idle = client.get("/api/busy").json()

        state._backup.status = web_state.ImportStatus.RUNNING
        state.session.importing.status = web_state.ImportStatus.RUNNING
        working = client.get("/api/busy").json()
        state._backup.status = web_state.ImportStatus.IDLE
        state.session.importing.status = web_state.ImportStatus.IDLE

    assert idle == {"busy": []}
    assert working == {"busy": ["a backup", "an import"]}


def test_an_import_and_a_backup_never_run_together(
    settings: Settings, card: Path, backup_drive: Path
) -> None:
    settings = replace(settings, backup_root=backup_drive / "PMB")
    state = AppState(settings)
    with TestClient(create_app(settings, state=state)) as client:
        put(card, "DSCF0001.JPG", fixtures.BURST_SECOND)
        scan_id = client.get("/api/card").json()["scan_id"]

        state._backup.status = web_state.ImportStatus.RUNNING
        importing = client.post(f"/api/card/{scan_id}/import")
        state._backup.status = web_state.ImportStatus.IDLE

        state.session.importing.status = web_state.ImportStatus.RUNNING
        backing_up = client.post("/api/backup")
        state.session.importing.status = web_state.ImportStatus.IDLE

    assert importing.status_code == 409 and importing.json()["error"] == "busy"
    assert backing_up.status_code == 409 and backing_up.json()["error"] == "busy"


def test_a_drive_that_stops_answering_does_not_freeze_the_app(
    settings: Settings, card: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A USB drive can hang outright — every call into it blocks, ls included.
    The strip polls every few seconds, so each poll must give up and say so,
    and a hung drive must cost one waiting thread, not one per request."""
    from photo_manager import backup
    settings = replace(settings, backup_root=tmp_path / "Volumes" / "Hung" / "PMB")
    released = threading.Event()
    asked: list[int] = []

    def hangs(*args, **kwargs):
        asked.append(1)
        released.wait(10)
        raise OSError("gone")

    monkeypatch.setattr(web_state, "DRIVE_TIMEOUT", 0.2)
    monkeypatch.setattr(backup, "status", hangs)
    try:
        with client_for(settings) as client:
            started = time.monotonic()
            first = client.get("/api/status").json()["backup"]
            second = client.get("/api/status").json()["backup"]
            refused = client.post("/api/backup")
            took = time.monotonic() - started
            library = client.get("/api/library/photos")
    finally:
        released.set()

    assert "not responding" in first["message"] and "not responding" in second["message"]
    assert refused.status_code == 409 and "not responding" in refused.json()["detail"]
    assert took < 3 and len(asked) == 1
    assert library.status_code in (200, 404)  # the rest of the app still answers


def test_scripts_are_revalidated_so_an_update_is_seen_without_a_hard_reload(
    settings: Settings, card: Path
) -> None:
    with client_for(settings) as client:
        response = client.get("/static/strip.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


# ----------------------------------------------------------------------
# The archive drive
# ----------------------------------------------------------------------


def _archive_settings(settings: Settings, backup_drive: Path) -> Settings:
    settings = replace(settings, archive_root=backup_drive / "Photos")
    seed(settings, content_hash=content_hash(fixtures.JPEG_WITHOUT_RAW),
         library_path="2026/2026-01/DSCF1029.JPG", original_name="DSCF1029.JPG")
    (settings.library_root / "2026" / "2026-01").mkdir(parents=True, exist_ok=True)
    shutil.copy2(fixtures.JPEG_WITHOUT_RAW,
                 settings.library_root / "2026" / "2026-01" / "DSCF1029.JPG")
    return settings


def _wait_for_archive(client: TestClient) -> dict:
    deadline = time.monotonic() + 30
    while (payload := client.get("/api/archive").json())["progress"]["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return payload


def test_the_archive_button_copies_verifies_and_the_photo_says_so(
    settings: Settings, backup_drive: Path
) -> None:
    settings = _archive_settings(settings, backup_drive)

    with client_for(settings) as client:
        before = client.get("/api/status").json()["archive"]
        photo_id = grid(client)["photos"][0]["id"]
        unarchived = client.get(f"/api/library/photos/{photo_id}").json()
        client.post("/api/archive")
        done = _wait_for_archive(client)
        after = client.get("/api/status").json()["archive"]
        archived = client.get(f"/api/library/photos/{photo_id}").json()

    assert before["configured"] and before["attached"] and before["photos_not_archived"] == 1
    assert done["progress"]["status"] == "done", done["progress"]["error"]
    assert done["progress"]["result"]["copied"] == 1
    assert after["photos_not_archived"] == 0 and after["last_verified"] is not None
    assert (backup_drive / "Photos" / "2026" / "2026-01" / "DSCF1029.JPG").read_bytes() \
        == fixtures.JPEG_WITHOUT_RAW.read_bytes()
    assert unarchived["archive"] is True and unarchived["archived_at"] is None
    assert archived["archived_at"] is not None and archived["on_mac"] is True


def test_with_no_archive_the_photo_says_nothing_about_one(settings: Settings) -> None:
    seed(settings, content_hash="a" * 64, library_path="2026/2026-01/a.jpg")
    with client_for(settings) as client:
        photo_id = grid(client)["photos"][0]["id"]
        detail = client.get(f"/api/library/photos/{photo_id}").json()
        status = client.get("/api/status").json()["archive"]
        refused = client.post("/api/archive")
    assert detail["archive"] is False and detail["archived_at"] is None
    assert status["configured"] is False
    assert refused.status_code == 409 and refused.json()["error"] == "archive-refused"


def test_a_card_is_safe_to_format_once_its_keepers_are_on_the_archive_drive(
    settings: Settings, card: Path, backup_drive: Path
) -> None:
    settings = _archive_settings(settings, backup_drive)
    put(card, "DSCF1029.JPG", fixtures.JPEG_WITHOUT_RAW)

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        client.post(f"/api/card/{scan_id}/confirm")
        _wait_for_confirm(client, scan_id)
        before = client.get("/api/card").json()
        client.post("/api/archive")
        _wait_for_archive(client)
        after = client.get("/api/card").json()

    [frame_before], [frame_after] = before["photos"], after["photos"]
    assert frame_before["stage"] == "in-library"
    assert before["lifecycle"]["archive"] is True
    assert before["lifecycle"]["reasons"] == ["1 on the Mac but not yet on the archive drive"]
    assert frame_after["stage"] == "backed-up" and frame_after["backed_up"] is True
    assert after["lifecycle"]["safe_to_format"] is True


def test_an_archive_never_runs_beside_an_import_or_a_backup(
    settings: Settings, card: Path, backup_drive: Path
) -> None:
    settings = _archive_settings(replace(settings, backup_root=backup_drive / "PMB"), backup_drive)
    state = AppState(settings)
    with TestClient(create_app(settings, state=state)) as client:
        put(card, "DSCF0001.JPG", fixtures.BURST_SECOND)
        scan_id = client.get("/api/card").json()["scan_id"]

        state._backup.status = web_state.ImportStatus.RUNNING
        while_backing_up = client.post("/api/archive")
        state._backup.status = web_state.ImportStatus.IDLE

        state._archive.status = web_state.ImportStatus.RUNNING
        importing = client.post(f"/api/card/{scan_id}/import")
        backing_up = client.post("/api/backup")
        busy = client.get("/api/busy").json()
        state._archive.status = web_state.ImportStatus.IDLE

    assert while_backing_up.status_code == 409 and while_backing_up.json()["error"] == "busy"
    assert importing.status_code == 409 and importing.json()["error"] == "busy"
    assert backing_up.status_code == 409 and backing_up.json()["error"] == "busy"
    assert busy == {"busy": ["an archive to the drive"]}


def test_a_photograph_removed_from_the_mac_is_still_shown_from_the_cache(
    settings: Settings, backup_drive: Path
) -> None:
    settings = _archive_settings(settings, backup_drive)
    with client_for(settings) as client:
        photo_id = grid(client)["photos"][0]["id"]
        first = client.get(f"/api/library/photos/{photo_id}/thumbnail")  # warms the cache
        client.post("/api/archive")
        _wait_for_archive(client)

        with Catalog.open(settings) as catalog:
            photo = catalog.photos.get(photo_id)
            catalog.photos.update(replace(photo, on_mac=False))
        (settings.library_root / "2026" / "2026-01" / "DSCF1029.JPG").unlink()

        cached = client.get(f"/api/library/photos/{photo_id}/thumbnail")
        detail = client.get(f"/api/library/photos/{photo_id}").json()
        # Never shown at this size, and the drive taken away: said plainly.
        unplugged = replace(settings, archive_root=backup_drive / "Gone" / "Photos")
    with client_for(unplugged) as client:
        preview = client.get(f"/api/library/photos/{photo_id}/preview")

    assert first.status_code == 200 and first.headers["X-Thumbnail-Cache"] == "miss"
    assert cached.status_code == 200 and cached.headers["X-Thumbnail-Cache"] == "hit"
    assert detail["on_mac"] is False and detail["archived_at"] is not None
    assert detail["bytes"] == fixtures.JPEG_WITHOUT_RAW.stat().st_size  # read off the drive
    assert preview.status_code == 404
    assert "only on the archive drive" in preview.json()["detail"]


def test_free_up_mac_space_follows_the_rule_and_bring_back_reverses_it(
    settings: Settings, backup_drive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import send2trash
    trashed: list[str] = []

    def fake_trash(path: str) -> None:
        Path(path).unlink()
        trashed.append(Path(path).name)

    monkeypatch.setattr(send2trash, "send2trash", fake_trash)
    settings = _archive_settings(settings, backup_drive)
    mac_copy = settings.library_root / "2026" / "2026-01" / "DSCF1029.JPG"

    with client_for(settings) as client:
        photo_id = grid(client)["photos"][0]["id"]
        before = client.get("/api/library/offload").json()
        client.post("/api/archive")
        _wait_for_archive(client)
        archived_plan = client.get("/api/library/offload").json()["plan"]

        marked = client.put(f"/api/library/photos/{photo_id}/keep-on-mac", json={"keep": False})
        client.post("/api/library/offload")
        deadline = time.monotonic() + 30
        while (done := client.get("/api/library/offload").json())["progress"]["status"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        removed = client.get(f"/api/library/photos/{photo_id}").json()
        strip = client.get("/api/status").json()["archive"]
        gone_from_mac = not mac_copy.exists()

        restored = client.post(f"/api/library/photos/{photo_id}/restore").json()

    assert before["configured"] is True and before["plan"]["not_archived"] == 1
    assert before["plan"]["photographs"] == 0  # not archived, so never a candidate
    assert archived_plan["photographs"] == 1
    assert marked.json()["keep_on_mac"] is False
    assert done["progress"]["status"] == "done", done["progress"]["error"]
    assert done["progress"]["result"]["removed"] == 1
    assert trashed == ["DSCF1029.JPG"] and gone_from_mac
    assert removed["on_mac"] is False and strip["only_on_drive"] == 1
    assert restored["restored"] == 1 and restored["photo"]["on_mac"] is True
    assert restored["photo"]["keep_on_mac"] is True
    assert mac_copy.read_bytes() == fixtures.JPEG_WITHOUT_RAW.read_bytes()


def test_free_up_needs_the_archive_drive(settings: Settings) -> None:
    with client_for(settings) as client:
        response = client.post("/api/library/offload")
    assert response.status_code == 409 and response.json()["error"] == "offload-refused"


def test_the_where_facet_partitions_the_library_and_filters_by_it(
    settings: Settings, backup_drive: Path
) -> None:
    settings = _archive_settings(settings, backup_drive)
    seed(settings, content_hash="b" * 64, library_path="2026/2026-01/b.jpg")
    with client_for(settings) as client:
        before = {v["value"]: v["count"] for v in grid(client)["facets"]["storage"]}
        client.post("/api/archive")
        _wait_for_archive(client)
        after = {v["value"]: v["count"] for v in grid(client)["facets"]["storage"]}
        filtered = client.get("/api/library/photos?storage=both").json()
        bad = client.get("/api/library/photos?storage=somewhere")

    # b.jpg has a row and no file, so it can never be archived.
    assert before == {"mac-only": 2}
    assert after == {"mac-only": 1, "both": 1}
    assert [p["name"] for p in filtered["photos"]] == ["DSCF1029.JPG"]
    assert bad.status_code == 422


def test_without_an_archive_there_is_no_where_facet(settings: Settings) -> None:
    seed(settings, content_hash="a" * 64, library_path="2026/2026-01/a.jpg")
    with client_for(settings) as client:
        assert grid(client)["facets"]["storage"] == []


# ----------------------------------------------------------------------
# The card workflow: Review → Import → Archive → Tag → Format card
# ----------------------------------------------------------------------


def _workflow(payload: dict) -> tuple[str, dict[str, bool]]:
    flow = payload["workflow"]
    return flow["current"], {step["key"]: step["done"] for step in flow["steps"]}


def test_the_workflow_opens_on_the_step_that_needs_attention(
    settings: Settings, card: Path, backup_drive: Path
) -> None:
    """A step is done when its work is done — worked out from state the
    server has, never from which buttons were pressed — and the page opens on
    the first one that is not."""
    settings = _archive_settings(settings, backup_drive)
    put(card, "DSCF0818.JPG", fixtures.SPLIT_SECOND)
    put(card, "DSCF1030.JPG", fixtures.BURST_SECOND)

    with client_for(settings) as client:
        fresh = client.get("/api/card").json()
        scan_id = fresh["scan_id"]
        decide(client, scan_id, 0, verdict="keep")
        halfway = client.get("/api/card").json()
        decide(client, scan_id, 1, verdict="leave")
        reviewed = client.get("/api/card").json()

        client.post(f"/api/card/{scan_id}/import")
        _wait_for_import(client, scan_id)
        imported = client.post("/api/card/scan").json()

        client.post("/api/archive")
        _wait_for_archive(client)
        archived = client.get("/api/card").json()
        outings = archived["workflow"]["outings"]

        unit = outings[0]
        session_id = unit["session_id"]
        if session_id is None:
            grouped = client.post("/api/library/sessions",
                                  json={"day": unit["day"], "one_session": True}).json()
            session_id = grouped["created"][0]["id"]
        client.put(f"/api/library/sessions/{session_id}/tags", json={"location": "St Aldo"})
        tagged = client.get("/api/card").json()

    assert _workflow(fresh) == ("review", {
        "review": False, "import": True, "archive": True, "tag": True, "format": False,
    })
    assert _workflow(halfway)[0] == "review"
    # Something kept and not imported: Import needs attention, and Archive and
    # Tag cannot be done while an import would add to both.
    assert _workflow(reviewed) == ("import", {
        "review": True, "import": False, "archive": False, "tag": False, "format": False,
    })
    assert _workflow(imported)[0] == "archive"
    assert _workflow(archived)[0] == "tag"
    # The whole unit, not only the card's part of it: naming an outing names
    # all of it, and this day also holds the photograph the library had already.
    assert len(outings) == 1 and outings[0]["photographs"] == 2
    assert _workflow(tagged) == ("format", {
        "review": True, "import": True, "archive": True, "tag": True, "format": False,
    })
    # The frame left on the card is why it is still not safe to format.
    assert tagged["lifecycle"]["reasons"] == ["1 you chose to leave on the card would be lost"]


def test_a_keeper_already_in_the_library_under_another_name_is_settled_by_the_import(
    settings: Settings, card: Path
) -> None:
    """The import skips a frame whose bytes the library already has, and says
    so. The rescan after it must not offer that frame as a keeper again —
    the Import step would never be done."""
    seed(settings, content_hash=content_hash(fixtures.BURST_FIRST),
         library_path="2026/2026-01/renamed.jpg", original_name="SOMETHING_ELSE.JPG")
    put(card, "DSCF0001.JPG", fixtures.BURST_FIRST)

    with client_for(settings) as client:
        before = client.get("/api/card").json()
        decide(client, before["scan_id"], 0, verdict="keep")
        client.post(f"/api/card/{before['scan_id']}/import")
        outcome = _wait_for_import(client, before["scan_id"])["import"]["outcome"]
        after = client.post("/api/card/scan").json()

    assert before["photos"][0]["dedup"] == "new"
    assert outcome["skipped"] == 1
    [frame] = after["photos"]
    assert frame["dedup"] == "imported"
    assert frame["stage"] == "in-library"
    assert _workflow(after)[1]["import"] is True


def test_a_cards_outings_are_only_the_ones_its_photographs_are_in(
    settings: Settings, card: Path
) -> None:
    """The Tag step is today's backlog narrowed to this card. Another day's
    untagged outing in the library is not this card's business."""
    seed(settings, content_hash="b" * 64, library_path="2026/2026-01/elsewhere.jpg",
         original_name="ELSEWHERE.JPG", captured_at=dt.datetime(2026, 1, 3, 10, 0))
    put(card, "DSCF0001.JPG", fixtures.BURST_FIRST)

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        assert client.get("/api/card").json()["workflow"]["outings"] == []
        decide(client, scan_id, 0, verdict="keep")
        client.post(f"/api/card/{scan_id}/import")
        _wait_for_import(client, scan_id)
        after = client.post("/api/card/scan").json()
        backlog = client.get("/api/library/triage").json()

    assert len(backlog["units"]) == 2
    [outing] = after["workflow"]["outings"]
    assert outing["day"] != "2026-01-03"


def test_the_archive_says_which_drive_to_plug_in_while_it_is_unplugged(
    settings: Settings, tmp_path: Path
) -> None:
    from photo_manager.web.app import _drive_name

    assert _drive_name(Path("/Volumes/My Passport/Photos")) == "My Passport"
    assert _drive_name(tmp_path / "archive") is None
    assert _drive_name(None) is None
    settings = replace(settings, archive_root=Path("/Volumes/No Such Drive Here/Photos"))
    with client_for(settings) as client:
        status = client.get("/api/status").json()
    assert status["archive"]["attached"] is False
    assert status["archive"]["drive"] == "No Such Drive Here"


def test_the_card_page_is_a_stepper_with_one_panel_per_step(client: TestClient) -> None:
    from photo_manager.web.app import STEPS

    page = client.get("/").text
    script = _frontend("card.js")
    for step in STEPS:
        assert f'data-step="{step}"' in page
        assert f'data-panel="{step}"' in page
        assert f"'{step}'" in script
    # Every step but the last has a Continue button naming the next one.
    for current, following in zip(STEPS, STEPS[1:]):
        assert f'data-next="{following}"' in page, current


def test_eject_hands_the_card_back_and_says_what_formatting_would_lose(
    settings: Settings, card: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Format card step's last button. Ejecting is an unmount; the answer
    carries the card's lifecycle as it was, so the page can still say what
    formatting would lose after the card has gone."""
    ejected: list[Path] = []
    monkeypatch.setattr(web_state, "eject_card", ejected.append)
    put(card, "DSCF0001.JPG", fixtures.BURST_FIRST)

    with client_for(settings) as client:
        scan_id = client.get("/api/card").json()["scan_id"]
        decide(client, scan_id, 0, verdict="leave")
        answer = client.post("/api/card/eject")
        stale = client.get(f"/api/card/{scan_id}/cull")

    assert answer.status_code == 200
    assert ejected == [card.parent]
    body = answer.json()
    assert body["lifecycle"]["reasons"] == ["1 you chose to leave on the card would be lost"]
    # The scan described a card that is no longer there.
    assert stale.status_code == 409


def test_eject_waits_for_an_import_to_finish(
    settings: Settings, card: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ejected: list[Path] = []
    monkeypatch.setattr(web_state, "eject_card", ejected.append)
    put(card, "DSCF0001.JPG", fixtures.BURST_FIRST)

    with client_for(settings) as client:
        client.get("/api/card")
        state = client.app.state.photo_manager
        state.session.importing.status = web_state.ImportStatus.RUNNING
        refused = client.post("/api/card/eject")
        state.session.importing.status = web_state.ImportStatus.DONE

    assert refused.status_code == 409
    assert "import" in refused.json()["detail"]
    assert ejected == []


def test_eject_takes_no_path_either(settings: Settings) -> None:
    test_no_route_accepts_a_filesystem_path(settings)


# ----------------------------------------------------------------------
# Browsing the library by date
# ----------------------------------------------------------------------


def test_months_arrive_with_the_panel_and_click_through_to_their_days(
    library: TestClient,
) -> None:
    """A month row is a link like every other facet: its count is what
    clicking it gives, and clicking it is a since/until range."""
    data = library.get("/api/library/photos").json()
    months = data["facets"]["month"]
    assert months, "no month rows"
    for month in months:
        assert month["params"].startswith(f"since={month['value']}-01&until={month['value']}-")
        clicked = library.get(f"/api/library/photos?{month['params']}").json()
        assert clicked["total"] == month["count"], month["value"]
        assert all(p["captured_at"].startswith(month["value"]) for p in clicked["photos"])


def test_the_page_browses_by_month_by_range_and_by_day(library: TestClient) -> None:
    script = _frontend("library.js")
    page = library.get("/library").text

    # Months drawn under their year, not as a heading of their own.
    assert 'dimension === "month"' in script
    assert "drawMonths" in script
    # A range of your own sets the same two parameters a year or month does.
    assert 'id="range-since"' in page and 'id="range-until"' in page
    assert 'params.set(name, value)' in script
    # A heading wherever the day changes, carried across appended pages.
    assert "dayHeading" in script and "lastDay" in script
    assert ".grid__day" in _frontend("app.css")
