"""Tagging, and the promise that it lands in both stores (WU-3.4).

This file carries three arguments.

**FR-3.3: one action, every photograph.** Typing a location fifty-four times is
the design failure the session exists to prevent, so "tagging a session applies
to all its photos" is tested by counting photographs and by counting *actions*,
not just by checking one frame came out right.

**FR-5.6: both stores, or neither.** The catalog is an index and the sidecar is
what survives this app (NFR-2, D-1), so a tag that reached only the database is
a tag that is not really saved. There are tests for the sidecar existing, for
its *content* matching the row, for both halves of a RAW pair getting one, for
a failed sidecar leaving the catalog unchanged rather than half-applied, and
for the sidecar being in place *before* the catalog commits — which is the
direction a crash has to be wrong in.

**FR-3.4: an override is precedence, not replacement.** Setting one, clearing
one, and a session change arriving underneath one are three different things.

The HTTP half is at the bottom: the same rules through the endpoints the page
actually calls, including the one that makes "absent" and "empty" mean
different things.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photo_manager import tagging, vocabulary
from photo_manager.catalog import Catalog
from photo_manager.config import Settings
from photo_manager.models import Photo, Session
from photo_manager.sessions import apply_proposal, propose
from photo_manager.sidecars import (
    PendingSidecar,
    SidecarError,
    read_sidecar,
    sidecar_path,
)
from photo_manager.tagging import TagError
from photo_manager.web.app import create_app
from photo_manager.web.state import AppState

import fixtures

DAY = dt.datetime(2026, 9, 6, 10, 6, 45)


# ----------------------------------------------------------------------
# Helpers — a small real library on disk, because sidecars need files
# ----------------------------------------------------------------------


def add_photo(
    catalog: Catalog,
    *,
    minutes: int = 0,
    raw: bool = False,
    rating: int = 0,
    source: Path | None = None,
) -> Photo:
    """One photograph, with real bytes at its library path."""
    index = catalog.photos.count() + 1
    captured = DAY + dt.timedelta(minutes=minutes)
    relative = f"2026/2026-09-06/2026-09-06_DSCF{index:04d}.jpg"
    destination = catalog.settings.library_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes((source or fixtures.JPEG_WITHOUT_RAW).read_bytes())

    raw_relative = None
    if raw:
        raw_relative = relative.replace(".jpg", ".raf")
        (catalog.settings.library_root / raw_relative).write_bytes(b"not really a raf")

    return catalog.photos.add(
        Photo(
            content_hash=f"{index:064x}",
            library_path=relative,
            raw_path=raw_relative,
            original_name=f"DSCF{index:04d}.JPG",
            captured_at=captured,
            imported_at=dt.datetime(2026, 9, 21, 12, 0),
            rating=rating,
        )
    )


def an_outing(catalog: Catalog, count: int = 3, **kwargs: object) -> tuple[Session, list[Photo]]:
    photos = [add_photo(catalog, minutes=index, **kwargs) for index in range(count)]  # type: ignore[arg-type]
    proposal = propose(catalog)[0]
    session = apply_proposal(catalog, proposal.as_one_block())[0]
    return session, [catalog.photos.get(p.id) for p in photos]  # type: ignore[misc]


def sidecar_of(catalog: Catalog, photo: Photo) -> Path:
    return sidecar_path(catalog.absolute_path(photo))


@pytest.fixture
def library(settings: Settings) -> Settings:
    """Settings whose library root exists. Sidecars are files, so it must."""
    return settings


# ----------------------------------------------------------------------
# FR-3.3 — one action, the whole outing
# ----------------------------------------------------------------------


def test_tagging_a_session_reaches_every_photograph(catalog: Catalog) -> None:
    session, photos = an_outing(catalog, 5)

    outcome = tagging.set_session_location(catalog, session.id, "St Aldo")

    assert outcome.count == 5
    assert [catalog.photos.location(p.id).name for p in photos] == ["St Aldo"] * 5


def test_tagging_a_session_is_one_use_of_the_word(catalog: Catalog) -> None:
    """FR-5.1c. Fifty-four frames is one reach for "St Aldo", not fifty-four."""
    session, _ = an_outing(catalog, 5)

    tagging.set_session_location(catalog, session.id, "St Aldo")

    assert catalog.locations.by_name("St Aldo").use_count == 1


def test_an_event_reaches_every_photograph_too(catalog: Catalog) -> None:
    session, photos = an_outing(catalog, 3)

    tagging.set_session_event(catalog, session.id, "  Beach   day ")

    assert catalog.sessions.get(session.id).event == "Beach day"
    for photo in photos:
        assert read_sidecar(catalog.absolute_path(photo)).event == "Beach day"


def test_people_and_tags_apply_across_a_session(catalog: Catalog) -> None:
    """People are per photograph in the schema, so this is genuinely N writes —
    but it is one action, which is what FR-3.3 asks for."""
    session, photos = an_outing(catalog, 4)

    tagging.set_session_people(catalog, session.id, ["Alice", "Bob"])
    tagging.set_session_tags(catalog, session.id, ["beach"])

    for photo in photos:
        assert {p.name for p in catalog.photos.people(photo.id)} == {"Alice", "Bob"}
        assert [t.name for t in catalog.photos.tags(photo.id)] == ["beach"]


def test_setting_people_replaces_rather_than_adds(catalog: Catalog) -> None:
    """The panel shows what will be true afterwards, so Save has to mean that."""
    session, photos = an_outing(catalog, 2)
    tagging.set_session_people(catalog, session.id, ["Alice", "Bob"])

    tagging.set_session_people(catalog, session.id, ["Alice"])

    for photo in photos:
        assert [p.name for p in catalog.photos.people(photo.id)] == ["Alice"]


def test_clearing_a_session_location_clears_it_everywhere(catalog: Catalog) -> None:
    session, photos = an_outing(catalog, 3)
    tagging.set_session_location(catalog, session.id, "St Aldo")

    tagging.set_session_location(catalog, session.id, None)

    assert all(catalog.photos.location(p.id) is None for p in photos)
    assert catalog.locations.by_name("St Aldo").use_count == 0


def test_tagging_a_session_that_does_not_exist_is_refused(catalog: Catalog) -> None:
    for call in (
        lambda: tagging.set_session_event(catalog, 999, "x"),
        lambda: tagging.set_session_people(catalog, 999, ["Alice"]),
    ):
        with pytest.raises((TagError, vocabulary.VocabularyError)):
            call()


# ----------------------------------------------------------------------
# FR-5.6 — both stores, or neither
# ----------------------------------------------------------------------


def test_a_tag_lands_in_the_sidecar_as_well_as_the_catalog(catalog: Catalog) -> None:
    """D-1's whole point. A tag only in a rebuildable database is not saved."""
    session, photos = an_outing(catalog, 2)

    tagging.set_session_location(catalog, session.id, "St Aldo")

    for photo in photos:
        written = read_sidecar(catalog.absolute_path(photo))
        assert written is not None
        assert written.location == "St Aldo"


def test_the_sidecar_says_the_same_as_the_row(catalog: Catalog) -> None:
    session, photos = an_outing(catalog, 1)
    tagging.set_session_location(catalog, session.id, "St Aldo")
    tagging.set_session_event(catalog, session.id, "Beach day")
    tagging.set_session_people(catalog, session.id, ["Alice"])
    tagging.set_session_tags(catalog, session.id, ["sunset", "beach"])
    tagging.set_photo_rating(catalog, photos[0].id, 4)

    written = read_sidecar(catalog.absolute_path(photos[0]))

    assert written.location == "St Aldo"
    assert written.event == "Beach day"
    assert written.people == ("Alice",)
    assert set(written.tags) == {"beach", "sunset"}
    assert written.rating == 4


def test_both_halves_of_a_raw_pair_get_one(catalog: Catalog) -> None:
    """§5.2 keeps the whole filename precisely so both can have one. Someone
    opening the RAF should find the rating on it."""
    session, photos = an_outing(catalog, 1, raw=True)

    tagging.set_session_location(catalog, session.id, "St Aldo")

    photo = catalog.photos.get(photos[0].id)
    assert sidecar_of(catalog, photo).is_file()
    assert sidecar_path(catalog.absolute_raw_path(photo)).is_file()
    assert read_sidecar(catalog.absolute_raw_path(photo)).location == "St Aldo"


def test_a_photograph_with_nothing_to_say_gets_no_sidecar(catalog: Catalog) -> None:
    """§5.2 again: Hundreds of files saying "nothing here" would double the file count
    in every folder, against success criterion 5."""
    session, photos = an_outing(catalog, 2)

    outcome = tagging.set_session_event(catalog, session.id, None)

    assert outcome.sidecars == ()
    assert outcome.skipped == 2
    assert not sidecar_of(catalog, photos[0]).exists()


def test_an_existing_sidecar_is_still_updated_when_everything_is_removed(
    catalog: Catalog,
) -> None:
    """There, the *removal* is itself the change worth recording."""
    session, photos = an_outing(catalog, 1)
    tagging.set_session_location(catalog, session.id, "St Aldo")
    assert sidecar_of(catalog, photos[0]).is_file()

    tagging.set_session_location(catalog, session.id, None)

    assert sidecar_of(catalog, photos[0]).is_file()
    assert read_sidecar(catalog.absolute_path(photos[0])).location is None


def test_a_sidecar_that_cannot_be_written_leaves_the_catalog_alone(
    catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure FR-5.6 is actually about. A photograph in the library that
    looks tagged and is not is worse than one that was never tagged."""
    session, photos = an_outing(catalog, 3)

    from photo_manager import tagging as module

    calls = {"n": 0}
    real = module.prepare_sidecar

    def fail_on_the_second(path: object, sidecar: object) -> object:
        calls["n"] += 1
        if calls["n"] == 2:
            raise SidecarError("the disk said no")
        return real(path, sidecar)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "prepare_sidecar", fail_on_the_second)

    with pytest.raises(SidecarError, match="the disk said no"):
        tagging.set_session_location(catalog, session.id, "St Aldo")

    assert catalog.sessions.get(session.id).location_id is None
    assert catalog.locations.all() == []
    assert all(not sidecar_of(catalog, photo).exists() for photo in photos)


def test_a_failure_part_way_leaves_no_temporary_files(catalog: Catalog,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """Litter in a folder success criterion 5 says should be *more* legible."""
    session, photos = an_outing(catalog, 3)

    from photo_manager import tagging as module

    calls = {"n": 0}
    real = module.prepare_sidecar

    def fail_on_the_third(path: object, sidecar: object) -> object:
        calls["n"] += 1
        if calls["n"] == 3:
            raise SidecarError("nope")
        return real(path, sidecar)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "prepare_sidecar", fail_on_the_third)

    with pytest.raises(SidecarError):
        tagging.set_session_location(catalog, session.id, "St Aldo")

    folder = catalog.absolute_path(photos[0]).parent
    assert list(folder.glob("*.tmp")) == []


def test_the_sidecars_are_in_place_before_the_catalog_commits(
    catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering, observed rather than described.

    A crash between the renames and the commit leaves sidecars saying something
    the catalog has not caught up with — and that is the recoverable direction,
    because the catalog is rebuildable from them (NFR-2) and nothing rebuilds
    them from it. If the order is ever reversed, this is the test that says so.

    Checked by asking a *second* connection what it can see at the moment a
    sidecar lands: a separate connection sees only committed state, so a NULL
    there means the catalog had not committed yet.
    """
    session, photos = an_outing(catalog, 2)
    assert catalog.settings.database_path is not None
    observed: dict[str, object] = {}

    real_commit = PendingSidecar.commit

    def watched(self: PendingSidecar) -> Path:
        result = real_commit(self)
        if "during" not in observed:
            other = sqlite3.connect(catalog.settings.database_path)
            try:
                observed["during"] = other.execute(
                    "SELECT location_id FROM session WHERE id = ?", (session.id,)
                ).fetchone()[0]
            finally:
                other.close()
        return result

    monkeypatch.setattr(PendingSidecar, "commit", watched)

    tagging.set_session_location(catalog, session.id, "St Aldo")

    assert observed["during"] is None, (
        "the catalog committed before the sidecars were in place — a crash there "
        "would lose the tags rather than leave them ahead"
    )
    assert catalog.sessions.get(session.id).location_id is not None
    assert read_sidecar(catalog.absolute_path(photos[0])).location == "St Aldo"


def test_resync_rewrites_sidecars_from_the_catalog(catalog: Catalog) -> None:
    """The recovery path for anything that wrote the catalog without coming
    through here — vocabulary on its own, or a cull rating from before this
    module existed."""
    session, photos = an_outing(catalog, 2)
    vocabulary.set_session_location(catalog, session.id, "St Aldo")  # catalog only
    assert not sidecar_of(catalog, photos[0]).exists()

    outcome = tagging.resync_sidecars(catalog)

    assert outcome.count == 2
    assert read_sidecar(catalog.absolute_path(photos[0])).location == "St Aldo"


# ----------------------------------------------------------------------
# FR-3.4 — the exception
# ----------------------------------------------------------------------


def test_an_override_reaches_that_photographs_sidecar_only(catalog: Catalog) -> None:
    session, photos = an_outing(catalog, 3)
    tagging.set_session_location(catalog, session.id, "St Aldo")

    outcome = tagging.set_photo_location(catalog, photos[1].id, "Penzance")

    assert outcome.count == 1
    assert read_sidecar(catalog.absolute_path(photos[1])).location == "Penzance"
    assert read_sidecar(catalog.absolute_path(photos[0])).location == "St Aldo"


def test_clearing_an_override_puts_the_sessions_location_back_in_the_sidecar(
    catalog: Catalog,
) -> None:
    session, photos = an_outing(catalog, 2)
    tagging.set_session_location(catalog, session.id, "St Aldo")
    tagging.set_photo_location(catalog, photos[0].id, "Penzance")

    tagging.set_photo_location(catalog, photos[0].id, None)

    assert read_sidecar(catalog.absolute_path(photos[0])).location == "St Aldo"


def test_retagging_a_session_rewrites_the_overriding_frames_sidecar_too(
    catalog: Catalog,
) -> None:
    """Its location does not change — but the session's *event* does, and that
    reaches every frame. A frame skipped because it had an override would end
    up with a stale event in its sidecar."""
    session, photos = an_outing(catalog, 2)
    tagging.set_photo_location(catalog, photos[0].id, "Penzance")
    tagging.set_session_event(catalog, session.id, "Beach day")

    tagging.set_session_event(catalog, session.id, "Pub")

    written = read_sidecar(catalog.absolute_path(photos[0]))
    assert written.event == "Pub"
    assert written.location == "Penzance"


def test_a_rating_is_per_photograph(catalog: Catalog) -> None:
    session, photos = an_outing(catalog, 2)

    tagging.set_photo_rating(catalog, photos[0].id, 5)

    assert catalog.photos.get(photos[0].id).rating == 5
    assert catalog.photos.get(photos[1].id).rating == 0
    assert read_sidecar(catalog.absolute_path(photos[0])).rating == 5


@pytest.mark.parametrize("bad", [-1, 6, 99])
def test_a_rating_outside_zero_to_five_is_refused(catalog: Catalog, bad: int) -> None:
    session, photos = an_outing(catalog, 1)

    with pytest.raises(TagError, match="0-5"):
        tagging.set_photo_rating(catalog, photos[0].id, bad)


def test_tagging_a_photograph_that_does_not_exist_is_refused(catalog: Catalog) -> None:
    with pytest.raises(TagError, match="No photograph with id 999"):
        tagging.set_photo_rating(catalog, 999, 3)
    with pytest.raises(vocabulary.VocabularyError):
        tagging.set_photo_location(catalog, 999, "St Aldo")


# ----------------------------------------------------------------------
# Through the HTTP surface — the page's actual path
# ----------------------------------------------------------------------


@pytest.fixture
def tagged_client(settings: Settings) -> TestClient:
    """An app over a small real library with one grouped outing."""
    with Catalog.open(settings) as catalog:
        an_outing(catalog, 3)
    return TestClient(create_app(settings, state=AppState(settings)))


def only_session(client: TestClient) -> dict:
    return client.get("/api/library/sessions").json()["sessions"][0]


def test_the_tag_page_is_served(tagged_client: TestClient) -> None:
    response = tagged_client.get("/tag")
    assert response.status_code == 200
    assert 'id="session-form"' in response.text


def test_sessions_and_the_backlog_arrive_together(settings: Settings) -> None:
    """Two halves of one screen: a day becomes an outing by being dealt with,
    so the backlog is the reason to open the page at all."""
    with Catalog.open(settings) as catalog:
        an_outing(catalog, 2)
        add_photo(catalog, minutes=60 * 24 * 3)  # a different day, ungrouped

    with TestClient(create_app(settings, state=AppState(settings))) as client:
        body = client.get("/api/library/sessions").json()

    assert body["counts"] == {"photographs": 3, "sessions": 1, "ungrouped": 1}
    assert len(body["sessions"]) == 1
    assert len(body["ungrouped"]) == 1
    assert body["ungrouped"][0]["photographs"] == 1


def test_a_day_is_grouped_through_the_api(settings: Settings) -> None:
    with Catalog.open(settings) as catalog:
        add_photo(catalog, minutes=0)
        add_photo(catalog, minutes=10)

    with TestClient(create_app(settings, state=AppState(settings))) as client:
        created = client.post(
            "/api/library/sessions", json={"day": "2026-09-06", "one_session": True}
        ).json()

        assert len(created["created"]) == 1
        assert created["created"][0]["photographs"] == 2
        assert client.get("/api/library/sessions").json()["ungrouped"] == []


def test_grouping_a_day_that_is_not_offered_is_a_404(tagged_client: TestClient) -> None:
    response = tagged_client.post(
        "/api/library/sessions", json={"day": "2026-09-06", "one_session": True}
    )
    assert response.status_code == 404
    assert "ungrouped" in response.json()["detail"]


def test_tagging_a_session_over_http_applies_to_every_photograph(
    tagged_client: TestClient,
) -> None:
    """The acceptance criterion, through the path the page actually uses."""
    session = only_session(tagged_client)

    body = tagged_client.put(
        f"/api/library/sessions/{session['id']}/tags",
        json={"location": "St Aldo", "event": "Beach day", "people": ["Alice"]},
    ).json()

    assert body["location"] == "St Aldo"
    assert body["event"] == "Beach day"
    assert body["people"] == ["Alice"]
    assert sorted(body["applied"]) == ["event", "location", "people"]
    assert all(photo["location"] == "St Aldo" for photo in body["photos"])


def test_a_field_that_was_not_sent_is_left_alone(tagged_client: TestClient) -> None:
    """Absent means "leave it"; present-and-empty means "clear it". Collapsing
    the two would make opening the panel and pressing Save a way to wipe things
    nobody looked at."""
    session = only_session(tagged_client)
    url = f"/api/library/sessions/{session['id']}/tags"
    tagged_client.put(url, json={"location": "St Aldo", "event": "Beach day"})

    body = tagged_client.put(url, json={"event": "Pub"}).json()

    assert body["event"] == "Pub"
    assert body["location"] == "St Aldo"
    assert body["applied"] == ["event"]


def test_an_empty_value_clears_it(tagged_client: TestClient) -> None:
    session = only_session(tagged_client)
    url = f"/api/library/sessions/{session['id']}/tags"
    tagged_client.put(url, json={"location": "St Aldo"})

    body = tagged_client.put(url, json={"location": ""}).json()

    assert body["location"] is None


def test_the_session_shows_only_people_every_frame_has(
    tagged_client: TestClient,
) -> None:
    """The intersection rather than the union: a name on two frames of fifty,
    shown as the session's, would be applied to the other forty-eight by the
    next Save."""
    session = only_session(tagged_client)
    detail = tagged_client.get(f"/api/library/sessions/{session['id']}").json()
    tagged_client.put(
        f"/api/library/photos/{detail['photos'][0]['id']}/tags", json={"people": ["Alice"]}
    )

    assert tagged_client.get(f"/api/library/sessions/{session['id']}").json()["people"] == []


def test_an_override_through_http_shows_as_its_own_statement(
    tagged_client: TestClient,
) -> None:
    session = only_session(tagged_client)
    detail = tagged_client.get(f"/api/library/sessions/{session['id']}").json()
    tagged_client.put(
        f"/api/library/sessions/{session['id']}/tags", json={"location": "St Aldo"}
    )
    first = detail["photos"][0]["id"]

    tagged_client.put(f"/api/library/photos/{first}/tags", json={"location": "Penzance"})
    after = tagged_client.get(f"/api/library/sessions/{session['id']}").json()

    assert after["overrides"] == 1
    assert after["photos"][0]["location"] == "Penzance"
    assert after["photos"][0]["override"] == "Penzance"
    # The frames that did not disagree say nothing of their own.
    assert after["photos"][1]["override"] is None
    assert after["photos"][1]["location"] == "St Aldo"


def test_a_rating_set_through_http_reaches_the_sidecar(
    tagged_client: TestClient, settings: Settings
) -> None:
    session = only_session(tagged_client)
    detail = tagged_client.get(f"/api/library/sessions/{session['id']}").json()
    photo_id = detail["photos"][0]["id"]

    body = tagged_client.put(
        f"/api/library/photos/{photo_id}/tags", json={"rating": 4}
    ).json()

    assert body["rating"] == 4
    assert body["sidecar"] is not None
    with Catalog.open(settings) as catalog:
        photo = catalog.photos.get(photo_id)
        assert read_sidecar(catalog.absolute_path(photo)).rating == 4


def test_a_library_thumbnail_is_served_and_then_cached(tagged_client: TestClient) -> None:
    session = only_session(tagged_client)
    detail = tagged_client.get(f"/api/library/sessions/{session['id']}").json()
    photo_id = detail["photos"][0]["id"]

    first = tagged_client.get(f"/api/library/photos/{photo_id}/thumbnail")
    second = tagged_client.get(f"/api/library/photos/{photo_id}/thumbnail")

    assert first.status_code == 200
    assert first.headers["content-type"] == "image/jpeg"
    assert first.headers["X-Thumbnail-Cache"] == "miss"
    # FR-2.3's shared, hash-keyed cache, on the library side of it.
    assert second.headers["X-Thumbnail-Cache"] == "hit"
    assert first.content == second.content


def test_an_unknown_library_id_is_a_404(tagged_client: TestClient) -> None:
    for path in (
        "/api/library/photos/9999",
        "/api/library/photos/9999/thumbnail",
        "/api/library/sessions/9999",
    ):
        response = tagged_client.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"] == "not-found"


def test_no_catalog_and_no_such_row_are_different_answers(
    settings: Settings,
) -> None:
    """FR-1.1a's lesson one screen over: "nothing imported yet" and "that id
    does not exist" have different fixes, so they are not spelled the same."""
    with TestClient(create_app(settings, state=AppState(settings))) as client:
        response = client.get("/api/library/sessions")

    assert response.status_code == 404
    assert response.json()["error"] == "no-library"
    assert "import something" in response.json()["detail"]


def test_a_bad_rating_over_http_is_a_422(tagged_client: TestClient) -> None:
    session = only_session(tagged_client)
    photo_id = tagged_client.get(
        f"/api/library/sessions/{session['id']}"
    ).json()["photos"][0]["id"]

    assert tagged_client.put(
        f"/api/library/photos/{photo_id}/tags", json={"rating": 9}
    ).status_code == 422


def test_autocomplete_ranks_through_the_api(tagged_client: TestClient) -> None:
    session = only_session(tagged_client)
    url = f"/api/library/sessions/{session['id']}/tags"
    tagged_client.put(url, json={"location": "Bristol"})
    tagged_client.put(url, json={"location": "St Aldo"})

    items = tagged_client.get(
        "/api/library/vocabulary/locations?q=st"
    ).json()["items"]

    # FR-5.1a: the name that starts with what was typed, before the one that
    # merely contains it.
    assert [item["name"] for item in items] == ["St Aldo", "Bristol"]
    assert items[0]["slug"] == "st-aldo"


def test_a_failed_sidecar_is_a_500_with_a_sentence(
    tagged_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-5.6 at the top of the stack: the browser is told, rather than shown a
    tick for something that did not reach the disk."""
    from photo_manager import tagging as module

    monkeypatch.setattr(
        module, "prepare_sidecar",
        lambda *_: (_ for _ in ()).throw(SidecarError("the disk said no")),
    )
    session = only_session(tagged_client)

    response = tagged_client.put(
        f"/api/library/sessions/{session['id']}/tags", json={"location": "St Aldo"}
    )

    assert response.status_code == 500
    assert response.json()["error"] == "sidecar-failed"
    assert "the disk said no" in response.json()["detail"]
    assert only_session(tagged_client)["location"] is None


# ----------------------------------------------------------------------
# The page itself — rules that live in the browser
# ----------------------------------------------------------------------


def frontend(name: str) -> str:
    from photo_manager.web.app import STATIC_DIR

    return (STATIC_DIR / name).read_text(encoding="utf-8")


def test_the_page_sends_only_the_fields_that_changed() -> None:
    """The server's "absent means leave alone" only helps if the browser
    actually leaves them out. Opening the panel and pressing Save must not be a
    way to wipe things nobody looked at.

    The guard itself, not a mention of it: the comparison against what the form
    was drawn with is what makes the body partial.
    """
    script = frontend("tag.js")
    assert "state.initial" in script
    assert "!== state.initial.location" in script
    assert "!== state.initial.people" in script


def test_the_page_says_the_tags_reached_the_disk_and_not_only_the_database() -> None:
    """A tick that meant "the database agreed" would be claiming the wrong half
    of D-1."""
    script = frontend("tag.js")
    assert "beside them on disk" in script


def test_the_page_draws_an_override_as_its_own_statement() -> None:
    """FR-3.4 is the exception, and an exception that looks like everything
    else around it is not visible at all — the same argument as FR-1.4's two
    kinds of already-imported."""
    assert "photo.override" in frontend("tag.js")
    assert ".frame__override" in frontend("app.css")
    # Dashed, like `imported?` on the card grid: a frame saying something for
    # itself, not a different shade of the session's answer.
    assert "dashed" in frontend("app.css").split(".frame__override")[1][:400]


def test_the_hidden_attribute_beats_every_display_rule() -> None:
    """A UA-stylesheet `display: none` loses to any `display:` in our own file,
    so an element toggled with `hidden` stays on screen forever. The per-frame
    sheet is `display: flex` and greeted the first page load.
    """
    css = frontend("app.css")
    assert "[hidden] { display: none !important; }" in css


def test_a_failure_is_shown_rather_than_swallowed() -> None:
    """FR-5.6 reaches the browser too: a sidecar that did not write must not
    leave the page looking saved."""
    script = frontend("tag.js")
    assert "Not saved" in script
