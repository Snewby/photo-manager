"""The FastAPI application (D-3).

The whole loop, now: find the card, render what is on it, judge it a frame at a
time, and copy the keepers into the library. WU-2.2 built the contact sheet,
WU-2.3 put a keyboard on it, WU-2.4 added the import — which is the only thing
here that writes a photograph anywhere, and is deliberately two steps: a plan
that copies nothing, then a button.

Three things it is careful about.

**It never renders an empty grid for a missing card.** FR-1.1a exists because
the first card implementation collapsed four different situations into "no
photos", and the same mistake is available to any UI built on top of it. Every
response carries :class:`photo_manager.cards.CardLocation`'s status *and* its
sentence, and the page leads with them.

**It never takes a path from the browser.** The only handle on a file is
``/api/card/<scan_id>/photos/<index>/thumbnail``, where the index is bounds
checked against a scan the server itself performed. There is no endpoint that
will read a path it is handed, so no page the user happens to have open in
another tab can turn this into a file server for the rest of the disk.

**It reads the card the way C-4 says it must.** The grid runs on tier 1
embedded thumbnails — a 128 KB header read per frame, ~25 ms — and falls
through to a generated, cached thumbnail only for the files that have no
embedded one (FR-2.1a). The expensive hashing pass that settles duplicates
(FR-1.4) runs on a background thread and the page updates when it lands. The
culling view's large previews cost about a second each cold, so the frames
after the one on screen are rendered ahead of the user rather than on demand
(WU-2.3).

**Nothing it does touches the card.** That was easy to say while the app only
read headers; the import reads every byte of every keeper and writes 15 GB
somewhere else, and it is still true. The card is opened for reading and
nothing else, which the tests check by snapshotting it rather than by trusting
this paragraph.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Query as QueryParam, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, Field

from .. import queries, tagging, vocabulary
from .. import backup
from ..backup import BackupError
from ..cards import CardError, CardLocation, CardPhoto, CardScan, Dedup
from ..catalog import Catalog
from ..config import Settings
from ..culling import Decision, Resume, Tally, Verdict
from ..importing import ImportFailed, ImportOutcome, ImportPlan
from ..hashing import content_hash
from ..models import Photo, Session
from ..queries import DIMENSIONS, QueryError
from ..sessions import DayProposal, SessionError, apply_proposal, propose
from ..sidecars import SidecarError, sidecar_path
from ..tagging import TagError
from ..thumbnails import ThumbnailCache, ThumbnailError, embedded_thumbnail
from ..vocabulary import VocabularyError
from .state import (
    AppState,
    BusyError,
    CardSession,
    CardWritableError,
    ConfirmProgress,
    ImportProgress,
    NotCullableError,
    StaleScanError,
    UnknownPhotoError,
    dedup_counts,
    open_catalog,
)

__all__ = ["create_app", "STATIC_DIR"]

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: A thumbnail for a given (scan, index) can never change — a rescan mints a
#: new scan_id and therefore new URLs — so the browser may hold them as long as
#: it likes. This matters: scrolling back up a thousand-tile grid would otherwise
#: re-read the card for every tile it passes.
_THUMBNAIL_CACHE_CONTROL = "private, max-age=86400, immutable"


class DecisionBody(BaseModel):
    """What the browser is allowed to say about a frame.

    Validated rather than trusted: ``verdict`` is one of three words and
    ``rating`` is 0-5, and both are checked here as well as in
    :class:`~photo_manager.culling.Decision`, so a malformed request is a 422
    with a sentence rather than a 500 from a dataclass three layers down.
    """

    verdict: Literal["undecided", "keep", "skip"] = Verdict.UNDECIDED
    rating: int = Field(default=0, ge=0, le=5)
    keep_raw: bool = False

    def to_decision(self) -> Decision:
        return Decision(
            verdict=self.verdict,
            # A rating on a frame that is not being kept would be a number
            # nothing will ever read; a skip is simply a skip.
            rating=self.rating if self.verdict == Verdict.KEEP else 0,
            keep_raw=self.keep_raw and self.verdict == Verdict.KEEP,
        )


# The literal above and the constants in culling.py are two spellings of one
# list, so they are checked against each other here rather than discovered to
# disagree by a request that should have been a 422 and was a 500.
assert set(DecisionBody.model_fields["verdict"].annotation.__args__) == set(Verdict.ALL)


class NoSuchRowError(LookupError):
    """A library id that the catalog does not hold. Always a 404."""


class NoLibraryError(LookupError):
    """There is no catalog yet, which is not the same as a missing row.

    FR-1.1a's lesson, one screen over: "nothing has ever been imported" and
    "that id does not exist" have different fixes, so they are not spelled the
    same. The first is answered by going to the card; the second means the page
    is out of date.
    """


class GroupBody(BaseModel):
    """Which day to resolve into sessions, and into what shape (FR-3.2)."""

    day: str
    #: True for "ignore the suggestion, it was one outing" — the whole day as
    #: one session. False takes the split as suggested.
    one_session: bool = False
    #: A contiguous run of block indices to combine before applying.
    merge: list[int] = Field(default_factory=list)


class SessionTagBody(BaseModel):
    """What may be said about a whole outing in one action (FR-3.3).

    Every field is optional and **absent means "leave it alone"**, which is not
    the same as present-and-empty meaning "clear it". A panel that only changed
    the location must not silently wipe the people, and a UI that sends the
    whole form must still be able to remove one.
    """

    location: str | None = None
    event: str | None = None
    people: list[str] | None = None
    tags: list[str] | None = None


class PhotoTagBody(BaseModel):
    """The per-frame exception (FR-3.4), plus its own rating and people."""

    location: str | None = None
    people: list[str] | None = None
    tags: list[str] | None = None
    rating: int | None = Field(default=None, ge=0, le=5)


class ImportBody(BaseModel):
    """What the browser may say about an import.

    ``allow_overrun`` exists so the free-space refusal can be overridden
    deliberately rather than worked around; it defaults to off, so the safe
    answer is the one you get by not thinking about it.
    """

    location_slug: str | None = None
    allow_overrun: bool = False


def create_app(
    settings: Settings | None = None,
    *,
    state: AppState | None = None,
) -> FastAPI:
    """Build the application.

    The library root is verified here, at startup, rather than on first
    request: a config that has not caught up with a moved library should fail
    with a sentence in the terminal, not a 500 three clicks in (§4.2).
    """
    settings = settings or Settings.load()
    settings.verify_library_root()
    app_state = state or AppState(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        # Ctrl-C with a confirming pass in flight would otherwise leave a
        # daemon thread reading the card until the process is torn out from
        # under it. Signal it and give it the one file read it needs to notice.
        app_state.shutdown()

    app = FastAPI(
        title="Photo Manager",
        summary="Local-only ingest, culling and cataloguing for a Fujifilm X-T30 library.",
        version="0.1.0",
        # Nothing here is a public API, and the docs pages are one more surface
        # on a port that other local pages can reach.
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.photo_manager = app_state

    # ------------------------------------------------------------------
    # Errors, as JSON with a sentence in them
    # ------------------------------------------------------------------

    @app.exception_handler(StaleScanError)
    def _stale(_: Request, exc: StaleScanError) -> JSONResponse:
        # 409, not 404: the photo is not missing, the browser's idea of the
        # card is. The fix is to reload, and the message says so.
        return JSONResponse({"error": "stale-scan", "detail": str(exc)}, status_code=409)

    @app.exception_handler(UnknownPhotoError)
    def _unknown(_: Request, exc: UnknownPhotoError) -> JSONResponse:
        return JSONResponse({"error": "unknown-photo", "detail": str(exc)}, status_code=404)

    @app.exception_handler(NotCullableError)
    def _not_cullable(_: Request, exc: NotCullableError) -> JSONResponse:
        # 409, not 404: the frame exists and is on screen in the grid. It is
        # withheld from the cull because it is already in the library (C-3),
        # and saying "not found" would suggest otherwise.
        return JSONResponse({"error": "not-cullable", "detail": str(exc)}, status_code=409)

    @app.exception_handler(ImportFailed)
    def _import_refused(_: Request, exc: ImportFailed) -> JSONResponse:
        # 409: the request was well formed and the library said no — almost
        # always because it will not fit (FR-4.5). The message carries the
        # numbers, because "cull further" needs to know by how much.
        return JSONResponse({"error": "import-refused", "detail": str(exc)}, status_code=409)

    @app.exception_handler(CardWritableError)
    def _card_writable(_: Request, exc: CardWritableError) -> JSONResponse:
        return JSONResponse({"error": "card-writable", "detail": str(exc)}, status_code=409)

    @app.exception_handler(CardError)
    def _card_refused(_: Request, exc: CardError) -> JSONResponse:
        return JSONResponse({"error": "card", "detail": str(exc)}, status_code=409)

    @app.exception_handler(BusyError)
    def _busy(_: Request, exc: BusyError) -> JSONResponse:
        return JSONResponse({"error": "busy", "detail": str(exc)}, status_code=409)

    @app.exception_handler(BackupError)
    def _backup_refused(_: Request, exc: BackupError) -> JSONResponse:
        return JSONResponse({"error": "backup-refused", "detail": str(exc)}, status_code=409)

    @app.exception_handler(NoSuchRowError)
    def _no_row(_: Request, exc: NoSuchRowError) -> JSONResponse:
        return JSONResponse({"error": "not-found", "detail": str(exc)}, status_code=404)

    @app.exception_handler(NoLibraryError)
    def _no_library(_: Request, exc: NoLibraryError) -> JSONResponse:
        return JSONResponse({"error": "no-library", "detail": str(exc)}, status_code=404)

    @app.exception_handler(TagError)
    def _bad_tag(_: Request, exc: TagError) -> JSONResponse:
        # 409: the request was well formed and the library said no. A failed
        # sidecar arrives here too (FR-5.6) — surfaced, never swallowed, and
        # with nothing changed in either store.
        return JSONResponse({"error": "tag-refused", "detail": str(exc)}, status_code=409)

    @app.exception_handler(QueryError)
    def _bad_filter(_: Request, exc: QueryError) -> JSONResponse:
        # 422: the request was understood and describes a filter that cannot
        # exist — a rating of 9, a date range that runs backwards. Answering
        # it with an empty grid would look like "you have no photographs
        # like that" rather than "that question is malformed".
        return JSONResponse({"error": "bad-filter", "detail": str(exc)}, status_code=422)

    @app.exception_handler(VocabularyError)
    def _bad_name(_: Request, exc: VocabularyError) -> JSONResponse:
        return JSONResponse({"error": "bad-name", "detail": str(exc)}, status_code=422)

    @app.exception_handler(SessionError)
    def _bad_grouping(_: Request, exc: SessionError) -> JSONResponse:
        # Includes StaleProposal: the day moved since the page drew it, and
        # the fix is to reload rather than to force it through.
        return JSONResponse({"error": "grouping-refused", "detail": str(exc)},
                            status_code=409)

    @app.exception_handler(SidecarError)
    def _bad_sidecar(_: Request, exc: SidecarError) -> JSONResponse:
        # FR-5.6, at the top of the stack. A tag that did not reach the disk is
        # a tag that is not saved, and the browser is told so rather than
        # showing a tick.
        return JSONResponse({"error": "sidecar-failed", "detail": str(exc)},
                            status_code=500)

    @app.exception_handler(ThumbnailError)
    def _unthumbnailable(_: Request, exc: ThumbnailError) -> JSONResponse:
        # One unreadable frame is one broken tile, never a broken grid
        # (FR-1.3c). The page draws a placeholder and carries on.
        return JSONResponse({"error": "no-thumbnail", "detail": str(exc)}, status_code=422)

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        """Enough to tell a running app from a running-but-misconfigured one."""
        with open_catalog(settings) as catalog:
            catalogued = None if catalog is None else catalog.photos.count()
            schema = None if catalog is None else catalog.schema_version
        return {
            "ok": True,
            "library_root": str(settings.library_root),
            "catalog_path": str(settings.database_path),
            "catalogued_photos": catalogued,
            "schema_version": schema,
            "thumbnail_max_px": settings.thumbnail_max_px,
        }

    @app.get("/api/card")
    def card() -> Response:
        """The current scan, scanning once if there isn't one yet."""
        return _json(_session_payload(app_state, app_state.scan()))

    @app.post("/api/card/scan")
    def rescan() -> Response:
        """Re-read the card. Mints a new scan_id, so every URL the browser
        holds is invalidated rather than quietly pointing at a different frame."""
        return _json(_session_payload(app_state, app_state.scan(force=True)))

    @app.post("/api/card/{scan_id}/confirm")
    def confirm_start(scan_id: str) -> Response:
        """Start the hashing pass that turns `likely-imported` into a fact."""
        return _json({"scan_id": scan_id, "confirm": _confirm_payload(app_state.start_confirm(scan_id))})

    @app.get("/api/card/{scan_id}/confirm")
    def confirm_progress(scan_id: str) -> Response:
        """Poll the pass. Small on purpose — the photo list is a separate fetch."""
        session = app_state.require(scan_id)
        return _json({
            "scan_id": scan_id,
            "confirm": _confirm_payload(session.confirm.snapshot()),
            "dedup": dedup_counts(session.scan),
        })

    @app.get("/api/card/{scan_id}/photos/{index}/thumbnail")
    def card_thumbnail(scan_id: str, index: int) -> Response:
        """A thumbnail for one frame on the card.

        Tier 1 (FR-2.1) wherever the camera left one, which on this card is
        every single frame; tier 2 (FR-2.2, cached by content hash per FR-2.3)
        only where it did not. Defined with ``def`` rather than ``async def``
        on purpose, so FastAPI runs the card read on a worker thread and the
        event loop stays free to answer the next tile (FR-2.4).
        """
        photo = app_state.photo(scan_id, index)
        data, tier, cache_state = _thumbnail(app_state, photo)
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={
                "Cache-Control": _THUMBNAIL_CACHE_CONTROL,
                # Not decoration: the split between these two is the entire
                # performance claim in FR-2.1, and being able to read it off a
                # response makes it measurable from outside.
                "X-Thumbnail-Tier": tier,
                "X-Thumbnail-Cache": cache_state,
            },
        )

    @app.get("/api/card/{scan_id}/photos/{index}/preview")
    def card_preview(scan_id: str, index: int) -> Response:
        """The large preview the culling view judges a photograph on (FR-4.1).

        Unlike the grid's tile, this one cannot come from the file's header:
        deciding whether a frame is a keeper needs the frame. That is a full
        ~15 MB read plus a decode, so it is cached by content hash like every
        other generated thumbnail, and the frames after this one are queued
        behind it.
        """
        data, cache_state = app_state.preview(scan_id, index)
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={
                "Cache-Control": _THUMBNAIL_CACHE_CONTROL,
                "X-Thumbnail-Tier": "preview",
                "X-Thumbnail-Cache": cache_state,
            },
        )

    @app.put("/api/card/{scan_id}/photos/{index}/decision")
    def card_decision(scan_id: str, index: int, body: DecisionBody) -> Response:
        """Keep, skip, rate or flag one frame (FR-4.2, FR-4.3).

        Nothing is committed to the library here — that is WU-2.4's import.
        This appends a line to the card's decision file and answers with the
        running tally, so the keeper count and the free-space warning move on
        every keypress rather than only when something asks (FR-4.5).
        """
        decision = app_state.decide(scan_id, index, body.to_decision())
        session = app_state.require(scan_id)
        return _json({
            "index": index,
            "decision": _decision_payload(decision),
            "tally": _tally_payload(app_state.tally(session)),
            "next": session.resume_at,
        })

    @app.get("/api/card/{scan_id}/import")
    def import_plan(scan_id: str) -> Response:
        """What the import would do, before it does any of it.

        Reads EXIF headers to work out each keeper's folder; copies nothing.
        The point is that every target name, every collision and the whole size
        is known and reviewable before the first byte moves (FR-8.6's habit,
        applied to FR-6.1).
        """
        return _json(_plan_payload(app_state.plan(scan_id)))

    @app.post("/api/card/{scan_id}/import")
    def import_start(scan_id: str, body: ImportBody | None = None) -> Response:
        """Copy the keepers into the library. Runs in the background.

        A full card is ~15 GB at 20-37 MB/s, so this is minutes (C-4) and
        cannot be held open by a request. Unlike the confirming pass it is not
        cancelled by a rescan: it is writing photographs.
        """
        body = body or ImportBody()
        progress = app_state.start_import(
            scan_id,
            location_slug=body.location_slug,
            allow_overrun=body.allow_overrun,
        )
        return _json({"scan_id": scan_id, "import": _import_payload(progress)})

    @app.get("/api/card/{scan_id}/import/progress")
    def import_progress(scan_id: str) -> Response:
        """Poll the import. Small, so it can be polled often."""
        return _json({
            "scan_id": scan_id,
            "import": _import_payload(app_state.import_progress(scan_id)),
        })

    @app.get("/api/status")
    def status() -> Response:
        """What every page shows above its own content: is the card safe, and
        is the library backed up. Cheap enough to poll — the card is located,
        not scanned, and the backup drive is asked about, not read."""
        # Located now rather than taken from the last scan, so a card plugged
        # in or remounted outside the app shows at once.
        return _json({
            "card": _location_payload(app_state.card_location()),
            "backup": _backup_payload(app_state),
        })

    @app.get("/api/busy")
    def busy() -> Response:
        """What quitting now would cut short. Asked by the Mac app before it
        stops the server, so a backup is never ended by a stray ⌘Q unasked."""
        return _json({"busy": app_state.busy()})

    @app.post("/api/card/remount")
    def card_remount() -> Response:
        """Have macOS mount the card read-only, then rescan it (§4.1)."""
        return _json(_session_payload(app_state, app_state.remount_card()))

    @app.get("/api/backup")
    def backup_state() -> Response:
        return _json(_backup_payload(app_state))

    @app.post("/api/backup")
    def backup_start() -> Response:
        """Snapshot the library to ``backup_root``. Takes no path: where a
        backup goes is configuration, never something a page sends (§4.1)."""
        app_state.start_backup()
        return _json(_backup_payload(app_state))

    @app.get("/api/card/{scan_id}/cull")
    def card_cull(scan_id: str) -> Response:
        """The cull state on its own, small enough to re-fetch freely."""
        session = app_state.require(scan_id)
        return _json(_cull_payload(app_state, session))

    # ------------------------------------------------------------------
    # The library: sessions, tagging, vocabulary (WU-3.4)
    # ------------------------------------------------------------------
    #
    # A library photograph is addressed by its **catalog id**, never by a path
    # (§4.1). An id is a primary key the server itself issued and looks up; a
    # request for one the catalog does not hold is a 404, and there is nothing
    # a crafted id can reach that is not already a row in this library.

    @app.get("/api/library/sessions")
    def library_sessions() -> Response:
        """Every outing, and every day still waiting to become one."""
        with _library(settings) as catalog:
            return _json(_sessions_payload(catalog))

    @app.post("/api/library/sessions")
    def library_group(body: GroupBody) -> Response:
        """Resolve one day into sessions — WU-3.1's accept, merge or ignore."""
        with _library(settings) as catalog:
            return _json(_group(catalog, body))

    @app.get("/api/library/sessions/{session_id}")
    def library_session(session_id: int) -> Response:
        with _library(settings) as catalog:
            return _json(_session_detail(catalog, session_id))

    @app.put("/api/library/sessions/{session_id}/tags")
    def library_session_tags(session_id: int, body: SessionTagBody) -> Response:
        """FR-3.3's one action. Everything sent is applied to every frame."""
        with _library(settings) as catalog:
            if catalog.sessions.get(session_id) is None:
                raise NoSuchRowError(f"No session with id {session_id}")
            touched = _apply_session_tags(catalog, session_id, body)
            return _json(_session_detail(catalog, session_id) | {"applied": touched})

    @app.get("/api/library/triage")
    def library_triage() -> Response:
        """The backlog, as the days it is made of (FR-7.6).

        Every row is a day with something still to say about it, and it
        carries the *one action* that would deal with it — which is not the
        same action for every row, because a day nobody has decided the shape
        of has to be grouped before it can be named (FR-3.2a).
        """
        with _library(settings) as catalog:
            return _json(_triage_payload(catalog))

    @app.get("/api/library/photos")
    def library_photos(
        query: queries.Query = Depends(_filter),
        limit: int = DEFAULT_PAGE,
        offset: int = 0,
    ) -> Response:
        """The library grid and its filter panel, in one answer (FR-7.1–7.4).

        One request rather than two, because the counts and the photographs
        describe the same filter and fetching them separately would let the
        panel and the grid be drawn from different moments.
        """
        with _library(settings) as catalog:
            return _json(
                _grid_payload(catalog, query, limit=_page_size(limit), offset=max(0, offset))
            )

    @app.get("/api/library/photos/{photo_id}")
    def library_photo(
        photo_id: int, query: queries.Query = Depends(_filter)
    ) -> Response:
        """One photograph, everything known about it, and what is either side.

        It takes the **same filter the grid takes** (WU-4.3), because the
        arrow keys in the detail view have to step through the set the user
        asked for rather than through the library. A link to
        ``/library?rating=4&photo=812`` therefore opens knowing it is the
        second of seven, with nothing loaded and nothing inferred from a grid
        that may not be on screen at all.
        """
        with _library(settings) as catalog:
            photo = _row(catalog, photo_id)
            return _json(
                _photo_detail(catalog, photo)
                | {
                    "neighbours": _neighbours_payload(
                        queries.neighbours(catalog, photo, query)
                    ),
                    "params": _query_params(query),
                }
            )

    @app.put("/api/library/photos/{photo_id}/tags")
    def library_photo_tags(photo_id: int, body: PhotoTagBody) -> Response:
        """FR-3.4's exception, plus this frame's own rating and people."""
        with _library(settings) as catalog:
            _row(catalog, photo_id)
            touched = _apply_photo_tags(catalog, photo_id, body)
            return _json(
                _photo_detail(catalog, _row(catalog, photo_id)) | {"applied": touched}
            )

    @app.get("/api/library/photos/{photo_id}/thumbnail")
    def library_thumbnail(photo_id: int) -> Response:
        """A cached ~400 px tile, keyed by content hash.

        The same cache the card grid uses (FR-2.3), which is the point of
        keying it by hash: a photograph imported this morning is already
        thumbnailed. Unlike the card, the library is on a 199 MB/s SSD, so the
        miss path is affordable and the hit path is instant forever after.
        """
        with _library(settings) as catalog:
            return _cached_image(catalog, _row(catalog, photo_id),
                                 app_state.thumbnails, tier="thumbnail")

    @app.get("/api/library/photos/{photo_id}/preview")
    def library_preview(photo_id: int) -> Response:
        """The big one the detail view is looked at on (FR-7.5).

        The *same* render the culling view uses — ``preview_max_px`` on the
        long edge, the size FR-4.1 measured as the largest this decoder hands
        back without resampling — and the same hash-keyed cache, so a frame
        judged on the card is already warm here. The library sits on an SSD
        rather than behind C-4's card reader, so a miss is a decode and not a
        decode plus a 15 MB crawl: ~0.45 s the first time, ~4 ms after.
        """
        with _library(settings) as catalog:
            return _cached_image(catalog, _row(catalog, photo_id),
                                 app_state.previews, tier="preview")

    @app.get("/api/library/vocabulary/{kind}")
    def library_vocabulary(kind: str, q: str = "", limit: int = 10) -> Response:
        """Autocomplete for a half-typed word (FR-5.1, FR-5.2, FR-5.4)."""
        suggest = {
            "locations": vocabulary.suggest_locations,
            "people": vocabulary.suggest_people,
            "tags": vocabulary.suggest_tags,
        }.get(kind)
        if suggest is None:
            raise NoSuchRowError(f"No vocabulary called {kind!r}")
        with _library(settings) as catalog:
            rows = suggest(catalog, q, limit=max(1, min(limit, 50)))
            return _json({"kind": kind, "query": q, "items": [
                {"name": row.name, "use_count": row.use_count,
                 "slug": getattr(row, "slug", None)}
                for row in rows
            ]})

    # ------------------------------------------------------------------
    # The page
    # ------------------------------------------------------------------

    @app.middleware("http")
    async def revalidate_static(request: Request, call_next):
        # Without a Cache-Control the browser guesses how long a script stays
        # fresh, and serves last week's after an update until a hard reload —
        # which cost ten minutes in WU-2.2 and showed the old backup strip
        # here. no-cache keeps the copy but asks first; unchanged is a 304.
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/cull")
    def cull_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "cull.html", media_type="text/html")

    @app.get("/tag")
    def tag_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "tag.html", media_type="text/html")

    @app.get("/library")
    def library_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "library.html", media_type="text/html")

    @app.get("/triage")
    def triage_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "triage.html", media_type="text/html")

    return app


# ----------------------------------------------------------------------
# The library (WU-3.4)
# ----------------------------------------------------------------------


@contextmanager
def _library(settings: Settings) -> Iterator[Catalog]:
    """The catalog, or a 404 with a sentence.

    Unlike the card side, which shows a grid before anything has been imported,
    every one of these endpoints is *about* the library. A missing catalog here
    is not a state to render around — it means there is nothing to tag yet.
    """
    with open_catalog(settings) as catalog:
        if catalog is None:
            raise NoLibraryError(
                "There is no catalog yet — import something from a card first."
            )
        yield catalog


def _row(catalog: Catalog, photo_id: int) -> Photo:
    photo = catalog.photos.get(photo_id)
    if photo is None:
        raise NoSuchRowError(f"No photograph with id {photo_id}")
    return photo


def _apply_session_tags(catalog: Catalog, session_id: int, body: SessionTagBody) -> list[str]:
    """Apply only what was actually sent, in the order that reads best.

    **Absent means "leave alone"; present-and-empty means "clear".** Those are
    different intentions and collapsing them would make a panel that changed
    only the location quietly wipe the people — so the two are told apart by
    which fields the request carried, not by whether their value is falsy.
    """
    sent = body.model_fields_set
    applied: list[str] = []
    if "location" in sent:
        tagging.set_session_location(catalog, session_id, body.location)
        applied.append("location")
    if "event" in sent:
        tagging.set_session_event(catalog, session_id, body.event)
        applied.append("event")
    if "people" in sent:
        tagging.set_session_people(catalog, session_id, body.people or [])
        applied.append("people")
    if "tags" in sent:
        tagging.set_session_tags(catalog, session_id, body.tags or [])
        applied.append("tags")
    return applied


def _apply_photo_tags(catalog: Catalog, photo_id: int, body: PhotoTagBody) -> list[str]:
    sent = body.model_fields_set
    applied: list[str] = []
    if "location" in sent:
        tagging.set_photo_location(catalog, photo_id, body.location)
        applied.append("location")
    if "people" in sent:
        tagging.set_photo_people(catalog, photo_id, body.people or [])
        applied.append("people")
    if "tags" in sent:
        tagging.set_photo_tags(catalog, photo_id, body.tags or [])
        applied.append("tags")
    if "rating" in sent and body.rating is not None:
        tagging.set_photo_rating(catalog, photo_id, body.rating)
        applied.append("rating")
    return applied


def _group(catalog: Catalog, body: GroupBody) -> dict[str, Any]:
    """Turn one day's proposal into sessions (WU-3.1, FR-3.2)."""
    try:
        day = dt.date.fromisoformat(body.day)
    except ValueError as exc:
        raise NoSuchRowError(f"{body.day!r} is not a date like 2026-09-06") from exc

    proposal = next((p for p in propose(catalog) if p.day == day), None)
    if proposal is None:
        raise NoSuchRowError(f"{day} has no ungrouped photographs")

    if body.merge:
        proposal = proposal.merged(*body.merge)
    if body.one_session:
        proposal = proposal.as_one_block()

    created = apply_proposal(catalog, proposal)
    return {
        "day": body.day,
        "created": [_session_summary(catalog, session) for session in created],
    }


def _sessions_payload(catalog: Catalog) -> dict[str, Any]:
    """Outings that exist, and days that have not been looked at yet.

    Both in one response, because they are two halves of the same screen: the
    backlog is the reason to open it, and a day becomes a session by being
    dealt with rather than by being imported (FR-3.2a).
    """
    sessions = [
        _session_summary(catalog, session)
        for session in catalog.sessions.all(newest_first=True)
    ]
    return {
        "sessions": sessions,
        "ungrouped": [_proposal_payload(p) for p in reversed(propose(catalog))],
        "counts": {
            "photographs": catalog.photos.count(),
            "sessions": len(sessions),
            "ungrouped": len(catalog.photos.unassigned()),
        },
    }


def _proposal_payload(proposal: DayProposal) -> dict[str, Any]:
    return {
        "day": proposal.day.isoformat(),
        "photographs": proposal.frame_count,
        "suggests_split": proposal.suggests_split,
        "partly_resolved": proposal.partly_resolved,
        "inferred": proposal.inferred_count,
        "started_at": proposal.started_at.isoformat(timespec="seconds"),
        "ended_at": proposal.ended_at.isoformat(timespec="seconds"),
        "blocks": [
            {
                "photographs": block.count,
                "started_at": block.started_at.isoformat(timespec="seconds"),
                "ended_at": block.ended_at.isoformat(timespec="seconds"),
                "gap_before": None if block.gap_before is None
                else round(block.gap_before.total_seconds()),
            }
            for block in proposal.blocks
        ],
    }


# ----------------------------------------------------------------------
# The grid and its filter panel (WU-4.2)
# ----------------------------------------------------------------------
#
# The whole page is one request. The counts and the photographs are two
# descriptions of one filter, and fetching them separately would let the panel
# say 54 next to a grid drawn from a different moment.


#: Tiles per page. Big enough that the real library is one page and a half,
#: small enough that a 10,000-photograph library does not build ten thousand
#: objects to show a screenful (NFR-1). The grid asks for the next page when
#: the reader reaches the bottom.
DEFAULT_PAGE = 120

#: Above this a single request stops being a page. Nothing needs it; it is here
#: because `limit` arrives from the browser and an unbounded one is an
#: invitation to build the whole library as JSON.
MAX_PAGE = 500


def _page_size(limit: int) -> int:
    return max(1, min(limit, MAX_PAGE))


def _with_none(values: list[Any], unset: bool) -> tuple[Any, ...]:
    """FR-7.2b: "nothing set" is one more value in the dimension."""
    return (*values, None) if unset else tuple(values)


def _filter(
    location: list[int] = QueryParam(default=[]),
    location_none: bool = False,
    event: list[str] = QueryParam(default=[]),
    event_none: bool = False,
    person: list[int] = QueryParam(default=[]),
    person_none: bool = False,
    tag: list[int] = QueryParam(default=[]),
    tag_none: bool = False,
    rating: int | None = None,
    since: dt.date | None = None,
    until: dt.date | None = None,
    raw: bool | None = None,
    untagged: bool | None = None,
) -> queries.Query:
    """FR-7.2's filter, read off a query string.

    One definition shared by the grid and the detail view, as a dependency,
    because those two have to agree about what a filter means or the arrow
    keys would step through a different set from the one the grid drew. Two
    copies of thirteen parameters is a divergence waiting for the fourteenth.

    **"Unset" is its own parameter** — ``location_none=true``, not a magic
    value inside ``location``. Location, person and tag are ids, where a
    reserved word could not collide; *event* is free text, where it could.
    Spelling the four the same way costs four parameter names and removes the
    question of what happens to somebody who runs an event called "none".
    """
    return queries.Query(
        locations=_with_none(location, location_none),
        events=_with_none(event, event_none),
        people=_with_none(person, person_none),
        tags=_with_none(tag, tag_none),
        rating_min=rating,
        since=since,
        until=until,
        has_raw=raw,
        untagged=untagged,
    )


def _cached_image(
    catalog: Catalog, photo: Photo, cache: ThumbnailCache, *, tier: str
) -> Response:
    """A library photograph at one of the two cached sizes.

    The tile and the detail view's preview differ in exactly one thing — which
    cache they come out of — so they are one function. Written twice they
    would drift on the parts that matter and are easy to leave out: that a row
    whose file has gone is a 404 with a sentence rather than a 500, and that
    the response says whether the cache had it, which is how the timings in
    NFR-1 are measurable from outside.
    """
    path = catalog.absolute_path(photo)
    if not path.is_file():
        raise NoSuchRowError(f"{photo.library_path} is in the catalog but not on disk")
    cached = cache.cached(photo.content_hash) is not None
    blob = cache.ensure(path, photo.content_hash).read_bytes()
    return Response(
        blob,
        media_type="image/jpeg",
        headers={
            # Keyed by content hash and the bytes never change, so this one
            # really is immutable — unlike a card tile, which is only
            # immutable for the life of a scan.
            "Cache-Control": _THUMBNAIL_CACHE_CONTROL,
            "X-Thumbnail-Tier": tier,
            "X-Thumbnail-Cache": "hit" if cached else "miss",
        },
    )


def _neighbours_payload(around: queries.Neighbours) -> dict[str, Any]:
    """Where this photograph sits in the filtered set, and what is either side."""
    return {
        "position": around.position,
        "total": around.total,
        "previous": around.previous,
        "next": around.next,
    }


def _query_params(query: queries.Query) -> str:
    """A query as the query string that reproduces it.

    Every facet value carries one of these, so clicking a filter is following
    a link rather than the page reimplementing :meth:`Query.selecting` in
    JavaScript. That matters beyond tidiness: FR-7.4a's rule is that a count
    is the size of what clicking gives, and the thing clicked is built from
    the same :class:`Query` the count was computed from.
    """
    pairs: list[tuple[str, str]] = []
    for name, values in (
        ("location", query.locations),
        ("event", query.events),
        ("person", query.people),
        ("tag", query.tags),
    ):
        for value in values:
            if value is None:
                pairs.append((f"{name}_none", "true"))
            else:
                pairs.append((name, str(value)))
    if query.rating_min is not None:
        pairs.append(("rating", str(query.rating_min)))
    if query.since is not None:
        pairs.append(("since", query.since.isoformat()))
    if query.until is not None:
        pairs.append(("until", query.until.isoformat()))
    if query.has_raw is not None:
        pairs.append(("raw", "true" if query.has_raw else "false"))
    if query.untagged is not None:
        pairs.append(("untagged", "true" if query.untagged else "false"))
    return urlencode(pairs)


def _facet_payload(query: queries.Query, dimension: str,
                   value: Any) -> dict[str, Any]:
    """One row of the panel: what it says, what it costs, and both links.

    ``params`` is the filter you get by clicking it and ``clear`` the one you
    get by taking it off again, because every filter that can go on has to be
    able to come off — and working either of them out in the browser would be
    a second copy of what :class:`Query` already knows.
    """
    return {
        "value": value.value,
        "label": value.label,
        "count": value.count,
        "selected": query.selects(dimension, value.value),
        "params": _query_params(value.query),
        "clear": _query_params(query.clearing(dimension, value.value)),
    }


def _tile_payload(photo: Photo, places: dict[int, str]) -> dict[str, Any]:
    """What a tile draws, and nothing else.

    Deliberately leaner than :func:`_photo_summary`, which asks four questions
    of the catalog per photograph — fine for the 54 frames of an outing, 480
    round trips for a page of this grid. People and tags are not on a tile, so
    they are not fetched; the place is, in one batched query (FR-3.4, through
    :func:`queries.places_for`).
    """
    assert photo.id is not None
    return {
        "id": photo.id,
        "name": Path(photo.library_path).name,
        "captured_at": photo.captured_at.isoformat(timespec="seconds"),
        "captured_at_inferred": photo.captured_at_inferred,
        "rating": photo.rating,
        "has_raw": photo.has_raw,
        "location": places.get(photo.id),
    }


def _grid_payload(catalog: Catalog, query: queries.Query, *,
                  limit: int, offset: int) -> dict[str, Any]:
    results = queries.search(catalog, query, limit=limit, offset=offset)
    ids = [photo.id for photo in results.photos if photo.id is not None]
    places = queries.places_for(catalog, ids)
    panel = queries.facets(catalog, query)

    return {
        "total": results.total,
        "shown": len(results.photos),
        "offset": offset,
        "limit": limit,
        # True when there are more to fetch, so the page does not have to
        # work it out from three numbers and get it wrong at the boundary.
        "more": offset + len(results.photos) < results.total,
        "library": catalog.photos.count(),
        "filtered": not query.is_empty,
        "params": _query_params(query),
        "photos": [_tile_payload(photo, places) for photo in results.photos],
        "facets": {
            dimension: [
                _facet_payload(query, dimension, value)
                for value in panel.of(dimension)
            ]
            for dimension in DIMENSIONS
        },
    }


# ----------------------------------------------------------------------
# The backlog (WU-4.4)
# ----------------------------------------------------------------------
#
# FR-7.6 asks for untagged photographs as a first-class view, and the first
# thing that needed deciding is what "untagged" means — four different things
# were filterable and none of them said which was the backlog. The answer is
# in `queries._UNTAGGED`: nothing said about it at all. The rest of this
# section is about the *other* half of the acceptance criterion, which is that
# tagging from the view removes photographs from it. That means the view has
# to carry an action, and the action has to be the cheap one: a whole day at
# once (FR-3.3), never a photograph at a time.


def _triage_payload(catalog: Catalog) -> dict[str, Any]:
    units = queries.backlog(catalog)
    proposals = {proposal.day: proposal for proposal in propose(catalog)}

    total = catalog.photos.count()
    untagged = queries.count(catalog, queries.Query(untagged=True))
    return {
        "counts": {
            "photographs": total,
            "untagged": untagged,
            "tagged": total - untagged,
            "units": len(units),
            "days": len({unit.day for unit in units}),
        },
        "units": [
            _backlog_unit_payload(unit, None if unit.grouped else proposals.get(unit.day))
            for unit in units
        ],
    }


def _backlog_unit_payload(
    unit: queries.BacklogUnit, proposal: DayProposal | None
) -> dict[str, Any]:
    """One row of the backlog, and what would clear it.

    **Which action it is, is decided here rather than in the browser**, for
    the same reason the facet links are (FR-7.1a): the page should follow what
    the server worked out rather than re-deriving a rule about sessions from
    three counts. There are three, and after the 2026-09-22 refactor all three
    can be finished without leaving the screen.

    ``tag`` — this is already an outing, so naming it is one request.

    ``group-and-tag`` — photographs in no outing yet, and nothing about them
    suggests there is a shape to decide, so they become one outing and are
    named in the same act. That is FR-3.1's default made explicit, which is
    exactly what "one outing" writes.

    ``shape`` — a gap of more than ``session_gap_hours`` sits inside them
    (FR-3.2), so there *is* a decision, and the row asks it: two outings, or
    one. **Asking is not deciding**, which is the distinction this screen used
    to get wrong by refusing to do either. Whichever is chosen, the answer
    becomes ``tag`` rows on the next load — because a day somebody split into
    a morning and an evening is two things to name, and that is the reason
    they split it.

    Anything finer than accept-or-ignore — merging the second and third blocks
    of a three-block day — stays with the tagging screen, which is the only
    place a shape can be edited rather than chosen.
    """
    if unit.grouped:
        action = "tag"
    elif proposal is not None and proposal.suggests_split:
        action = "shape"
    else:
        action = "group-and-tag"

    blocks = [] if proposal is None else [
        {
            "photographs": block.count,
            "started_at": block.started_at.isoformat(timespec="minutes"),
            "ended_at": block.ended_at.isoformat(timespec="minutes"),
            "gap_before": None if block.gap_before is None
            else round(block.gap_before.total_seconds()),
        }
        for block in proposal.blocks
    ]

    return {
        "day": unit.day.isoformat(),
        "session_id": unit.session_id,
        "photographs": unit.photographs,
        "untagged": unit.untagged,
        # Two states worth telling apart at a glance: one nobody has touched,
        # and one where the work was started and left.
        "partly": unit.partly,
        "started_at": unit.started_at.isoformat(timespec="minutes"),
        "ended_at": unit.ended_at.isoformat(timespec="minutes"),
        "covers": list(unit.covers),
        "action": action,
        "blocks": blocks,
        "params": _query_params(
            queries.Query(untagged=True, since=unit.day, until=unit.day)
        ),
    }


def _session_summary(catalog: Catalog, session: Session) -> dict[str, Any]:
    assert session.id is not None
    photos = catalog.photos.in_session(session.id)
    location = None if session.location_id is None else catalog.locations.get(session.location_id)
    overridden = sum(
        1 for photo in photos
        if photo.id is not None and catalog.photos.location_override(photo.id) is not None
    )
    return {
        "id": session.id,
        "day": session.started_at.date().isoformat(),
        "started_at": session.started_at.isoformat(timespec="seconds"),
        "ended_at": None if session.ended_at is None
        else session.ended_at.isoformat(timespec="seconds"),
        "photographs": len(photos),
        "location": None if location is None else location.name,
        "location_slug": None if location is None else location.slug,
        "event": session.event,
        # Shared by every frame in the outing, so the panel can show them as
        # the session's rather than as one photograph's.
        "people": sorted(_common(catalog.photos.people, photos)),
        "tags": sorted(_common(catalog.photos.tags, photos)),
        "overrides": overridden,
        "cover": photos[0].id if photos else None,
        "untagged": location is None and not session.event,
    }


def _common(lookup: Any, photos: list[Photo]) -> set[str]:
    """Names every one of these photographs carries.

    The intersection rather than the union, because the panel offers to set the
    session's people — and showing a name that only two frames of fifty have
    would make pressing Save silently apply it to the other forty-eight.
    """
    shared: set[str] | None = None
    for photo in photos:
        if photo.id is None:
            continue
        here = {row.name for row in lookup(photo.id)}
        shared = here if shared is None else (shared & here)
        if not shared:
            return set()
    return shared or set()


def _session_detail(catalog: Catalog, session_id: int) -> dict[str, Any]:
    session = catalog.sessions.get(session_id)
    if session is None:
        raise NoSuchRowError(f"No session with id {session_id}")
    photos = catalog.photos.in_session(session_id)
    return _session_summary(catalog, session) | {
        "photos": [_photo_summary(catalog, photo) for photo in photos],
    }


def _photo_summary(catalog: Catalog, photo: Photo) -> dict[str, Any]:
    assert photo.id is not None
    override = catalog.photos.location_override(photo.id)
    effective = catalog.photos.location(photo.id)
    return {
        "id": photo.id,
        "name": Path(photo.library_path).name,
        "original_name": photo.original_name,
        "captured_at": photo.captured_at.isoformat(timespec="seconds"),
        "captured_at_inferred": photo.captured_at_inferred,
        "rating": photo.rating,
        "has_raw": photo.has_raw,
        # Two different statements, never collapsed: what applies to this
        # frame, and whether the frame said it for itself (FR-3.4).
        "location": None if effective is None else effective.name,
        "override": None if override is None else override.name,
        "people": [person.name for person in catalog.photos.people(photo.id)],
        "tags": [tag.name for tag in catalog.photos.tags(photo.id)],
    }


def _session_location_name(catalog: Catalog, session: Session | None) -> str | None:
    """The outing's own location, which the frame may be overriding."""
    if session is None or session.location_id is None:
        return None
    location = catalog.locations.get(session.location_id)
    return None if location is None else location.name


def _photo_detail(catalog: Catalog, photo: Photo) -> dict[str, Any]:
    """Everything known about one photograph (FR-7.5).

    Deliberately the opposite of :func:`_tile_payload`: this one is fetched
    once for the frame being looked at, so it can afford the questions a tile
    cannot, and leaving something out would mean a detail view that shows less
    than the catalog holds.

    **What is editable here and what is not is a statement about ownership.**
    Rating, the location override, people and tags belong to the frame. The
    event and the outing's own location belong to the session and reach every
    frame in it (FR-3.3), so they are sent with the number of photographs they
    would touch beside them — visible and editable, but never silently fifty-
    four photographs at once.
    """
    assert photo.id is not None
    session = None if photo.session_id is None else catalog.sessions.get(photo.session_id)
    sidecar = sidecar_path(catalog.absolute_path(photo))
    return _photo_summary(catalog, photo) | {
        "library_path": photo.library_path,
        "raw_path": photo.raw_path,
        "bytes": _size_on_disk(catalog.absolute_path(photo)),
        "width": photo.width,
        "height": photo.height,
        "imported_at": photo.imported_at.isoformat(timespec="seconds"),
        "session_id": photo.session_id,
        "event": None if session is None else session.event,
        "session_location": _session_location_name(catalog, session),
        # So the outing's fields can say what they would cost before they are
        # touched, rather than after.
        "session_photographs": 0 if photo.session_id is None
        else len(catalog.photos.in_session(photo.session_id)),
        "camera_model": photo.camera_model,
        "lens": photo.lens,
        "exposure": photo.exposure,
        "f_number": photo.f_number,
        "iso": photo.iso,
        "focal_length": photo.focal_length,
        # Whether D-1's other half actually exists for this photograph. A UI
        # that claimed "saved" without it would be claiming the wrong thing.
        "sidecar": sidecar.name if sidecar.is_file() else None,
    }


def _size_on_disk(path: Path) -> int | None:
    """How big the file actually is, or None if it is not there.

    None rather than an error: a row whose file has gone is a real state and
    the detail view is exactly where somebody would want to find out about it,
    so it is reported as a gap in one line rather than as a broken page.
    """
    try:
        return path.stat().st_size
    except OSError:
        return None


# ----------------------------------------------------------------------
# Thumbnails
# ----------------------------------------------------------------------


def _thumbnail(state: AppState, photo: CardPhoto) -> tuple[bytes, str, str]:
    """Thumbnail bytes, which tier produced them, and whether it was cached.

    Tier 1 is not cached and is not meant to be (FR-2.3): extracting it costs
    less than asking the filesystem whether a cache entry exists. Tier 2 is,
    and keying that cache by content hash means the hash has to be paid for —
    14 MB off a 25 MB/s reader. That is affordable precisely because it is
    rare: measured on this card, **none of a card's** frames lack an embedded
    thumbnail; in an older library it is a handful (FR-2.1a).
    """
    blob = embedded_thumbnail(photo.primary)
    if blob is not None:
        return blob, "embedded", "uncached"

    digest = photo.content_hash or content_hash(photo.primary)
    hit = state.thumbnails.cached(digest) is not None
    path = state.thumbnails.ensure(photo.primary, digest)
    return path.read_bytes(), "generated", "hit" if hit else "miss"


# ----------------------------------------------------------------------
# Serialisation
# ----------------------------------------------------------------------


def _json(payload: dict[str, Any]) -> Response:
    # no-store because every one of these is a live reading of removable
    # hardware. A cached scan is a lie about what is in the reader.
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


def _session_payload(state: AppState, session: CardSession) -> dict[str, Any]:
    scan = session.scan
    stages = _frame_stages(state, session)
    return {
        "scan_id": session.scan_id,
        "scanned_at": session.scanned_at.isoformat(timespec="seconds"),
        "scan_seconds": round(session.scan_seconds, 3),
        "location": _location_payload(scan.location),
        "counts": _counts_payload(scan),
        "bytes": {"jpeg": scan.jpeg_bytes, "total": scan.total_bytes},
        "confirm": _confirm_payload(session.confirm.snapshot()),
        "cull": _cull_payload(state, session),
        "lifecycle": _lifecycle_payload(stages),
        "photos": [
            {**_photo_payload(session.scan_id, index, photo), **stages[index]}
            for index, photo in enumerate(scan.photos)
        ],
        "ignored": [path.name for path in scan.ignored],
    }


class Stage:
    """Where one frame on the card has got to, in the order it gets there.

    The question this answers is the one a person holding the card asks: is
    this photograph safe anywhere else yet? Nothing in this app deletes from
    the card, so *skip* means "not imported", and the card is only ever
    emptied by formatting it in the camera.
    """

    UNDECIDED = "undecided"
    SKIP = "skip"
    KEEP = "keep"
    #: A library photograph has this filename; the bytes are not yet compared.
    LIKELY_IN_LIBRARY = "likely-in-library"
    IN_LIBRARY = "in-library"
    BACKED_UP = "backed-up"

    ALL = (UNDECIDED, SKIP, KEEP, LIKELY_IN_LIBRARY, IN_LIBRARY, BACKED_UP)


def _frame_stages(state: AppState, session: CardSession) -> list[dict[str, Any]]:
    """Each frame's :class:`Stage`, and whether its library copy is backed up.

    ``backed_up`` is None where it cannot be said — the frame is not in the
    library, or there is no backup to ask, or the drive did not answer — so
    a page never draws "unknown" as "no".
    """
    backed_up = state.backed_up_hashes()
    hashes = state.catalog_hashes()
    stages: list[dict[str, Any]] = []
    for index, photo in enumerate(session.scan.photos):
        library_hash = hashes.get(photo.matched_photo_id) if photo.matched_photo_id else None
        in_backup = (
            None if backed_up is None or library_hash is None else library_hash in backed_up
        )
        if photo.dedup == Dedup.IMPORTED:
            stage = Stage.BACKED_UP if in_backup else Stage.IN_LIBRARY
        elif photo.dedup == Dedup.LIKELY_IMPORTED:
            stage = Stage.LIKELY_IN_LIBRARY
        else:
            decision = session.decisions.get(index)
            verdict = decision.verdict if decision is not None else Verdict.UNDECIDED
            stage = {Verdict.KEEP: Stage.KEEP, Verdict.SKIP: Stage.SKIP}.get(
                verdict, Stage.UNDECIDED
            )
            in_backup = None
        stages.append({"stage": stage, "backed_up": in_backup})
    return stages


def _lifecycle_payload(stages: list[dict[str, Any]]) -> dict[str, Any]:
    """How many frames are at each stage, and whether the card can be
    formatted without losing anything that was not deliberately skipped."""
    counts = {stage: 0 for stage in Stage.ALL}
    for frame in stages:
        counts[frame["stage"]] += 1
    blocking = {
        Stage.UNDECIDED: "not decided yet",
        Stage.KEEP: "kept but not imported yet",
        Stage.LIKELY_IN_LIBRARY: "in the library by name only — confirm by hash",
        Stage.IN_LIBRARY: "in the library but not confirmed in the last backup",
    }
    reasons = [f"{counts[stage]} {why}" for stage, why in blocking.items() if counts[stage]]
    return {
        "counts": counts,
        "safe_to_format": bool(stages) and not reasons,
        "reasons": reasons,
    }


def _location_payload(location: CardLocation) -> dict[str, Any]:
    """The whole of FR-1.1a, carried through to the browser.

    ``status`` and ``message`` are both here and both used: the status drives
    what the page does, the sentence tells the human what to do about it. A UI
    that kept only one of them would be back to an ambiguous empty grid.
    """
    return {
        "status": location.status,
        "found": location.found,
        "message": location.message,
        "dcim_root": None if location.dcim_root is None else str(location.dcim_root),
        "image_folders": [folder.name for folder in location.image_folders],
        "elsewhere": [str(path) for path in location.elsewhere],
        "writable": location.writable,
        "volume": None if location.dcim_root is None else location.dcim_root.parent.name,
    }


def _backup_payload(state: AppState) -> dict[str, Any]:
    status = state.backup_status()
    progress = state.backup_progress()
    result = progress.result
    return {
        "configured": status.configured,
        "attached": status.attached,
        "message": status.message,
        "volume": status.volume,
        "free_bytes": status.free_bytes,
        "last_taken": status.last_taken,
        "last_snapshot": status.last_snapshot,
        "photos_not_backed_up": status.photos_not_backed_up,
        "sidecars_changed": status.sidecars_changed,
        "progress": {
            "status": progress.status,
            "done": progress.done,
            "total": progress.total,
            "current": progress.current,
            "seconds": round(progress.seconds, 1),
            "error": progress.error,
            "result": None if result is None else {
                "snapshot": result.snapshot.name,
                "files": result.files,
                "copied": result.copied,
                "linked": result.linked,
                "bytes_copied": result.bytes_copied,
                "bytes_linked": result.bytes_linked,
                "incremental_from": result.incremental_from,
            },
        },
    }


def _counts_payload(scan: CardScan) -> dict[str, Any]:
    return {
        "photos": scan.photo_count,
        "pairs": scan.pair_count,
        "unpaired": len(scan.unpaired),
        "ignored": len(scan.ignored),
        "to_cull": len(scan.to_cull),
        "dedup": dedup_counts(scan),
    }


def _photo_payload(scan_id: str, index: int, photo: CardPhoto) -> dict[str, Any]:
    """One tile's worth of card photo.

    No filesystem path goes in here. The browser gets an index into a named
    scan and a URL built from it, which is the only handle the API accepts.
    """
    return {
        "index": index,
        "name": photo.primary.name,
        "folder": photo.folder.name,
        "has_raw": photo.has_raw,
        "unpaired": photo.unpaired,
        "bytes": photo.total_bytes,
        "dedup": photo.dedup,
        "confirmed": not photo.needs_confirming,
        "matched_photo_id": photo.matched_photo_id,
        "thumbnail_url": f"/api/card/{scan_id}/photos/{index}/thumbnail",
        "preview_url": f"/api/card/{scan_id}/photos/{index}/preview",
    }


def _cull_payload(state: AppState, session: CardSession) -> dict[str, Any]:
    """Everything the culling view needs that is not the photo list itself.

    ``order`` is indices into ``photos``, so the page never has to work out for
    itself which frames are on offer — the server already withheld the
    already-imported ones (C-3) and re-derives the list whenever the confirming
    pass changes the answer.
    """
    return {
        "order": list(session.cull_order),
        "decisions": {
            str(index): _decision_payload(decision)
            for index, decision in sorted(session.decisions.items())
        },
        "resume_at": session.resume_at,
        "resumed": _resume_payload(session.resume),
        "tally": _tally_payload(state.tally(session)),
        "import": _import_payload(session.importing.snapshot()),
    }


def _decision_payload(decision: Decision) -> dict[str, Any]:
    return {
        "verdict": decision.verdict,
        "rating": decision.rating,
        "keep_raw": decision.keep_raw,
        "decided": decision.decided,
    }


def _resume_payload(resume: Resume | None) -> dict[str, Any]:
    """What was carried over from a previous sitting, including what was not.

    ``stale`` is the interesting one: a stored decision whose filename matched
    a frame on the card but whose size did not. It is reported by name rather
    than applied or dropped, because applying it could silently skip a
    photograph the user meant to keep.
    """
    if resume is None:
        return {"resumed": 0, "stale": [], "absent": 0, "unreadable": 0}
    return {
        "resumed": resume.resumed,
        "stale": list(resume.stale),
        "absent": resume.absent,
        "unreadable": resume.unreadable,
    }


def _tally_payload(tally: Tally) -> dict[str, Any]:
    """FR-4.5, with the free-space figure measured at the moment of asking."""
    return {
        "keepers": tally.keepers,
        "skipped": tally.skipped,
        "undecided": tally.undecided,
        "raw_keepers": tally.raw_keepers,
        "jpeg_bytes": tally.jpeg_bytes,
        "raw_bytes": tally.raw_bytes,
        "total_bytes": tally.total_bytes,
        "free_bytes": tally.free_bytes,
        "headroom_bytes": tally.headroom_bytes,
        "over_capacity": tally.over_capacity,
    }


def _plan_payload(plan: ImportPlan) -> dict[str, Any]:
    return {
        "photos": len(plan.photos),
        "with_raw": plan.raw_count,
        "total_bytes": plan.total_bytes,
        "free_bytes": plan.free_bytes,
        "headroom_bytes": plan.headroom_bytes,
        "fits": plan.fits,
        "directories": list(plan.target_directories),
        "collisions": [photo.target for photo in plan.collisions],
        "unplannable": [
            {"name": entry.name, "reason": entry.reason} for entry in plan.unplannable
        ],
        # A sample rather than all 925: the point is to recognise the shape of
        # the names before committing to them, not to read every one.
        "sample": [photo.target for photo in plan.photos[:12]],
    }


def _import_payload(progress: ImportProgress) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": progress.status,
        "done": progress.done,
        "total": progress.total,
        "current": progress.current,
        "seconds": round(progress.seconds, 2),
        "error": progress.error,
        "outcome": None,
    }
    if progress.outcome is not None:
        payload["outcome"] = _outcome_payload(progress.outcome)
    return payload


def _outcome_payload(outcome: ImportOutcome) -> dict[str, Any]:
    """FR-6.6: imported, skipped as duplicate, failed — and why, for each."""
    return {
        "imported": len(outcome.imported),
        "skipped": len(outcome.skipped),
        "failed": len(outcome.failed),
        "unplannable": len(outcome.unplannable),
        "bytes_written": outcome.bytes_written,
        "complete": outcome.complete,
        "targets": [entry.planned.target for entry in outcome.imported[:12]],
        "problems": (
            [
                {"name": entry.planned.original_name, "reason": entry.reason}
                for entry in outcome.failed
            ]
            + [
                {"name": entry.name, "reason": entry.reason}
                for entry in outcome.unplannable
            ]
        )[:20],
        "duplicates": [
            {"name": entry.planned.original_name, "existing": entry.existing_path}
            for entry in outcome.skipped[:20]
        ],
    }


def _confirm_payload(progress: ConfirmProgress) -> dict[str, Any]:
    return {
        "status": progress.status,
        "done": progress.done,
        "total": progress.total,
        "current": progress.current,
        "seconds": round(progress.seconds, 2),
        "error": progress.error,
    }
