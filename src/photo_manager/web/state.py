"""What the server remembers between requests.

A card scan is not a thing you recompute per request. It takes about a second
on a full card (WU-2.1), it hands out indices the browser then addresses
photos by, and the confirming pass that settles the cheap dedup verdicts takes
**78 seconds** on this reader (C-4, FR-1.4). So one :class:`CardSession` is
held in memory, the browser is given its ``scan_id``, and every later request
names that id.

Three decisions worth the space:

**Photos are addressed by index into a named scan, never by path.** A browser
is a browser even on localhost — any page the user has open can issue requests
here — so there is deliberately no endpoint that takes a filesystem path and
reads it. ``/api/card/<scan_id>/photos/<index>/thumbnail`` can only ever reach
a file the scan itself found on the card. The ``scan_id`` is in the URL as well
as the index because indices are only meaningful within one scan: after a
rescan, index 400 may be a different frame, and serving the wrong photo
silently is precisely the class of bug WU-2.1 was about.

**The catalog is opened per operation, not held open.** ``sqlite3``
connections belong to the thread that created them, and this app has several:
FastAPI runs synchronous endpoints on a worker thread pool, and the confirming
pass runs on a thread of its own. WAL mode (NFR-2) makes concurrent readers
free and opening cheap, so a short-lived connection per operation is simpler
and harder to get wrong than one shared connection behind a mutex.

**The confirming pass runs in the background, and is cancellable.** §8 of the
execution plan left this to WU-2.2 or WU-2.3; it lands here, because a grid
that stalls for 78 seconds on first paint is not a skeleton of anything worth
building on. It is also stopped when the scan it belongs to is replaced —
otherwise a rescan leaves a thread reading 3 GB off the card for an answer
nobody will ever see, competing for the reader with the grid that replaced it.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from .. import archive, backup, offload
from ..archive import ArchiveError, ArchiveResult, ArchiveStatus
from ..backup import BackupError, BackupResult
from ..offload import OffloadError, OffloadResult
from ..cards import (
    CardError, CardLocation, CardPhoto, CardScan, Dedup, card_writable, confirm_imports,
    detect_card, remount_read_only, scan_card,
)
from ..catalog import Catalog
from ..config import Settings
from ..culling import (
    CullStore, Decision, PhotoKey, Resume, Tally, card_key, known_serials, tally,
)
from ..importing import ImportFailed, ImportOutcome, ImportPlan, execute_import, plan_import
from ..thumbnails import ThumbnailCache, ThumbnailError, render_and_hash

__all__ = [
    "AppState",
    "BackupProgress",
    "BusyError",
    "CardSession",
    "CardWritableError",
    "ConfirmProgress",
    "ConfirmStatus",
    "ImportProgress",
    "ImportStatus",
    "NotCullableError",
    "StaleScanError",
    "UnknownPhotoError",
    "dedup_counts",
    "open_catalog",
    "PREFETCH_DEPTH",
]

#: How long the status strip waits on a drive before saying it is not
#: responding. A USB drive can stop answering altogether — every call into it
#: blocks, ``ls`` included — and a page that polls every few seconds must not
#: turn that into a frozen app.
DRIVE_TIMEOUT = 3.0

#: How many frames ahead of the one on screen to render previews for.
#:
#: A preview costs ~0.5 s of card read plus ~0.45 s of decode, so a cold one is
#: about a second — long enough to be the thing you notice. Culling is
#: sequential, so the next few are knowable, and three deep on three threads
#: keeps a reader running at 20-37 MB/s (C-4) comfortably ahead of a human
#: looking at photographs. Deeper buys nothing: the reader, not the queue, is
#: the limit, and frames prefetched past where the user stops are pure waste.
PREFETCH_DEPTH = 3


def dedup_counts(scan: CardScan) -> dict[str, int]:
    """Dedup counts with the states that are absent reported as zero.

    Filled out rather than sparse so the UI can say "0 imported" — which is a
    fact about the card — instead of leaving a gap that reads the same as "not
    checked yet".
    """
    counts = {
        Dedup.NEW: 0,
        Dedup.LIKELY_IMPORTED: 0,
        Dedup.IMPORTED: 0,
        Dedup.NOT_IMPORTED: 0,
    }
    counts.update(scan.dedup_counts())
    return counts


class StaleScanError(LookupError):
    """The browser named a scan that is no longer the current one.

    Almost always harmless — a rescan happened in another tab, or the server
    restarted — but it must never be papered over by falling back to "the
    current scan", because that would answer a request for photo 400 of one
    card with photo 400 of another.
    """


class UnknownPhotoError(LookupError):
    """No photo at that index in that scan."""


class NotCullableError(LookupError):
    """That photo is not one of the ones being offered for culling.

    Which is not the same as it not existing. An already-imported frame is on
    the card, in the grid, and deliberately withheld from the cull (C-3,
    success criterion 2) — so asking to decide on it is answered with what it
    is, not with a 404.
    """


class ConfirmStatus:
    """Where the background confirming pass has got to (FR-1.4)."""

    #: Never started for this scan.
    IDLE = "idle"
    #: Hashing candidates now.
    RUNNING = "running"
    #: Every candidate settled as imported or not-imported.
    DONE = "done"
    #: Abandoned because the scan it belonged to was replaced.
    CANCELLED = "cancelled"
    #: Something went wrong; :attr:`ConfirmProgress.error` says what.
    FAILED = "failed"


@dataclass
class ConfirmProgress:
    """Progress of one confirming pass. Mutated only under the state lock."""

    status: str = ConfirmStatus.IDLE
    done: int = 0
    total: int = 0
    #: Filename currently being hashed, for a progress line that shows movement.
    current: str | None = None
    seconds: float = 0.0
    error: str | None = None

    @property
    def running(self) -> bool:
        return self.status == ConfirmStatus.RUNNING

    def snapshot(self) -> ConfirmProgress:
        """A copy safe to serialise outside the lock."""
        return ConfirmProgress(
            status=self.status,
            done=self.done,
            total=self.total,
            current=self.current,
            seconds=self.seconds,
            error=self.error,
        )


class ImportStatus:
    """Where the import has got to (FR-6.6)."""

    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class ImportProgress:
    """Progress of one import. Mutated only under the state lock."""

    status: str = ImportStatus.IDLE
    done: int = 0
    total: int = 0
    current: str | None = None
    seconds: float = 0.0
    error: str | None = None
    outcome: ImportOutcome | None = None

    @property
    def running(self) -> bool:
        return self.status == ImportStatus.RUNNING

    def snapshot(self) -> ImportProgress:
        return ImportProgress(
            status=self.status, done=self.done, total=self.total,
            current=self.current, seconds=self.seconds, error=self.error,
            outcome=self.outcome,
        )


@dataclass
class BackupProgress:
    """Progress of one backup. Mutated only under the state lock."""

    status: str = ImportStatus.IDLE
    done: int = 0
    total: int = 0
    current: str | None = None
    seconds: float = 0.0
    error: str | None = None
    result: BackupResult | None = None

    @property
    def running(self) -> bool:
        return self.status == ImportStatus.RUNNING

    def snapshot(self) -> BackupProgress:
        return BackupProgress(
            status=self.status, done=self.done, total=self.total,
            current=self.current, seconds=self.seconds, error=self.error,
            result=self.result,
        )


@dataclass
class ArchiveProgress:
    """Progress of one archive run. Mutated only under the state lock."""

    status: str = ImportStatus.IDLE
    done: int = 0
    total: int = 0
    current: str | None = None
    seconds: float = 0.0
    error: str | None = None
    result: ArchiveResult | None = None

    @property
    def running(self) -> bool:
        return self.status == ImportStatus.RUNNING

    def snapshot(self) -> ArchiveProgress:
        return ArchiveProgress(
            status=self.status, done=self.done, total=self.total,
            current=self.current, seconds=self.seconds, error=self.error,
            result=self.result,
        )


@dataclass
class OffloadProgress:
    """Progress of one *Free up Mac space*. Mutated only under the state lock."""

    status: str = ImportStatus.IDLE
    done: int = 0
    total: int = 0
    current: str | None = None
    seconds: float = 0.0
    error: str | None = None
    result: OffloadResult | None = None

    @property
    def running(self) -> bool:
        return self.status == ImportStatus.RUNNING

    def snapshot(self) -> OffloadProgress:
        return OffloadProgress(
            status=self.status, done=self.done, total=self.total,
            current=self.current, seconds=self.seconds, error=self.error,
            result=self.result,
        )


class CardWritableError(PermissionError):
    """The card is mounted read-write, so culling and importing wait (§4.1).

    Neither writes to the card — the decisions go to ``state/`` and the import
    reads — but every minute it stays mounted that way is a minute macOS and
    Finder can add files to it, and the fix is one button.
    """


class BusyError(RuntimeError):
    """An import, a backup and an archive run would be reading and writing the
    same library, so only one of them runs at a time."""


@dataclass
class CardSession:
    """One scan of the card, the background pass over it, and the cull."""

    scan_id: str
    scan: CardScan
    scanned_at: dt.datetime
    scan_seconds: float
    confirm: ConfirmProgress = field(default_factory=ConfirmProgress)

    #: Where this card's decisions are written and read back (FR-4.4).
    store: CullStore | None = None
    #: Indices into ``scan.photos``, in shooting order, of the frames actually
    #: being offered. Already-imported ones are not in it (C-3).
    cull_order: tuple[int, ...] = ()
    #: Decisions by scan index. Loaded from the store on scan, so a restart
    #: resumes rather than restarts.
    decisions: dict[int, Decision] = field(default_factory=dict)
    #: What the store had to say when it was matched against this card.
    resume: Resume | None = None
    #: Content hashes learned by rendering a preview, which reads the whole
    #: file anyway. Kept here rather than on the frozen CardPhoto, so the card
    #: scanner's type stays the card scanner's.
    hashes: dict[int, str] = field(default_factory=dict)

    #: The import, if one has been asked for on this scan.
    importing: ImportProgress = field(default_factory=ImportProgress)

    def photo(self, index: int) -> CardPhoto:
        if index < 0 or index >= len(self.scan.photos):
            raise UnknownPhotoError(
                f"No photo at index {index}; this scan holds "
                f"{len(self.scan.photos)}."
            )
        return self.scan.photos[index]

    def decision(self, index: int) -> Decision:
        return self.decisions.get(index, Decision())

    @property
    def resume_at(self) -> int | None:
        """The first frame with no decision — FR-4.4's "where you left off".

        Not the last one decided: coming back to the frame after the last one
        you judged is what "resume" means to someone who closed the app
        halfway through a card. Returns None when the cull is finished.
        """
        for index in self.cull_order:
            if not self.decision(index).decided:
                return index
        return None


class _Cancelled(Exception):
    """Raised out of the progress callback to stop a confirming pass.

    :func:`photo_manager.cards.confirm_imports` calls its progress callback
    once per candidate, immediately before the 14 MB read that settles it, so
    raising from there stops the pass within one file rather than at the end of
    all 209. Stopping the loop from the callback it already has is a smaller
    change than teaching the scanner about cancellation, and the card is the
    only resource at stake.
    """


@contextmanager
def open_catalog(settings: Settings, *, create: bool = False) -> Iterator[Catalog | None]:
    """The catalog for the duration of one operation.

    Absent and *not* creating it is the right answer for reading: before
    anything has been imported there is nothing to deduplicate against, and the
    app should still show the card rather than conjuring an empty database to
    answer a question about a memory card.

    ``create=True`` is for the import, where the absence means something else
    entirely — this is the first photograph anyone has ever imported. Refusing
    that would make a fresh install depend on having run the one-off library
    migration, which a new user has no reason to have done.
    """
    assert settings.database_path is not None  # filled in by Settings
    if not create and not settings.database_path.exists():
        yield None
        return
    catalog = Catalog.open(settings)
    try:
        yield catalog
    finally:
        catalog.close()


class AppState:
    """Everything the app holds between requests. Thread-safe."""

    def __init__(self, settings: Settings, *, thumbnails: ThumbnailCache | None = None) -> None:
        self.settings = settings
        self.thumbnails = thumbnails or ThumbnailCache.from_settings(settings)
        # The same cache directory, a different size. ThumbnailCache puts the
        # size in the filename (FR-2.3), so a grid tile and a culling preview
        # of the same photograph coexist without either shadowing the other.
        assert settings.thumbnail_cache_dir is not None  # filled in by Settings
        self.previews = ThumbnailCache(
            settings.thumbnail_cache_dir, max_px=settings.preview_max_px
        )
        self._lock = threading.RLock()
        self._session: CardSession | None = None
        self._confirm_thread: threading.Thread | None = None
        self._import_thread: threading.Thread | None = None
        self._backup = BackupProgress()
        self._backup_thread: threading.Thread | None = None
        self._archive = ArchiveProgress()
        self._archive_thread: threading.Thread | None = None
        self._offload = OffloadProgress()
        self._offload_thread: threading.Thread | None = None
        # One worker per question asked of a drive, and never more than one of
        # each in flight: if a drive hangs, one thread waits on it rather than
        # every request the page sends.
        self._probes: dict[str, tuple[ThreadPoolExecutor, Future | None]] = {}
        # Three, because the work is one card read plus one decode and the
        # reader is the limit (C-4). More threads queue on the same bandwidth.
        self._previews = ThreadPoolExecutor(max_workers=3, thread_name_prefix="preview")
        self._prefetching: set[tuple[str, int]] = set()

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    @property
    def session(self) -> CardSession | None:
        """The current scan, or None if the card has not been scanned yet."""
        with self._lock:
            return self._session

    def scan(self, *, force: bool = False) -> CardSession:
        """Scan the card, or return the scan already in hand.

        The lock is held across the scan itself. A scan is about a second and
        the alternative — two requests scanning the same card at once and one
        of them winning — is worse than a request waiting for one.
        """
        with self._lock:
            if self._session is not None and not force:
                return self._session

            self._cancel_confirm()
            started = time.perf_counter()
            with open_catalog(self.settings) as catalog:
                scan = scan_card(self.settings, catalog=catalog)
            elapsed = time.perf_counter() - started

            session = CardSession(
                scan_id=uuid.uuid4().hex,
                scan=scan,
                scanned_at=dt.datetime.now(),
                scan_seconds=elapsed,
            )
            if scan.location.found and scan.location.dcim_root is not None:
                session.store = CullStore.for_card(
                    self.settings,
                    card_key(scan.location.dcim_root, known=known_serials(self.settings)),
                )
                session.resume = session.store.resume(scan)
                session.decisions = dict(session.resume.decisions)
            self._retarget(session)
            self._session = session
            return self._session

    @staticmethod
    def _retarget(session: CardSession) -> None:
        """Recompute which frames the cull offers.

        Called after a scan and again after the confirming pass, because
        confirming *changes the answer*: a likely-imported frame settles into
        `imported` and leaves the cull, or into `not-imported` and joins it.
        Decisions are keyed by scan index and indices do not move, so nothing
        the user has already judged is disturbed by the list changing under it.
        """
        offered = {id(photo) for photo in session.scan.to_cull}
        session.cull_order = tuple(
            index for index, photo in enumerate(session.scan.photos)
            if id(photo) in offered
        )

    def require(self, scan_id: str) -> CardSession:
        """The session with that id, or :class:`StaleScanError`."""
        with self._lock:
            session = self._session
            if session is None:
                raise StaleScanError("The card has not been scanned yet.")
            if session.scan_id != scan_id:
                raise StaleScanError(
                    "That scan has been replaced by a newer one. Reload the page."
                )
            return session

    def photo(self, scan_id: str, index: int) -> CardPhoto:
        """One photo from a named scan. The only way to reach a file on the card."""
        return self.require(scan_id).photo(index)

    # ------------------------------------------------------------------
    # The confirming pass (FR-1.4, tier 2)
    # ------------------------------------------------------------------

    def start_confirm(self, scan_id: str) -> ConfirmProgress:
        """Start hashing this scan's likely-imported candidates, in the background.

        Idempotent while a pass is running: asking twice does not start a
        second one, because the expensive thing here is the card reader and
        two passes over it would each be half as fast.
        """
        with self._lock:
            session = self.require(scan_id)
            if session.confirm.running:
                return session.confirm.snapshot()

            candidates = [p for p in session.scan.photos if p.needs_confirming]
            session.confirm = ConfirmProgress(
                status=ConfirmStatus.DONE if not candidates else ConfirmStatus.RUNNING,
                total=len(candidates),
            )
            if not candidates:
                return session.confirm.snapshot()

            thread = threading.Thread(
                target=self._confirm,
                args=(scan_id,),
                name="confirm-imports",
                daemon=True,
            )
            self._confirm_thread = thread
            thread.start()
            return session.confirm.snapshot()

    def _confirm(self, scan_id: str) -> None:
        """The background pass. Runs off the request threads entirely."""
        started = time.perf_counter()

        def progress(done: int, total: int, photo: CardPhoto) -> None:
            with self._lock:
                session = self._session
                if session is None or session.scan_id != scan_id:
                    raise _Cancelled  # the scan was replaced under us
                if session.confirm.status != ConfirmStatus.RUNNING:
                    raise _Cancelled  # shutdown, or cancelled in place
                session.confirm.done = done - 1  # `done` counts the one starting now
                session.confirm.total = total
                session.confirm.current = photo.primary.name
                session.confirm.seconds = time.perf_counter() - started

        try:
            with self._lock:
                session = self._session
                if session is None or session.scan_id != scan_id:
                    return
                scan = session.scan
            with open_catalog(self.settings) as catalog:
                if catalog is None:
                    raise RuntimeError(
                        "There is no catalog yet, so nothing can be confirmed "
                        "as already imported."
                    )
                settled = confirm_imports(scan, catalog, progress=progress)
        except _Cancelled:
            self._finish(scan_id, status=ConfirmStatus.CANCELLED, seconds=time.perf_counter() - started)
        except Exception as exc:  # noqa: BLE001 — a failed pass must not kill the app
            self._finish(
                scan_id,
                status=ConfirmStatus.FAILED,
                seconds=time.perf_counter() - started,
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            with self._lock:
                session = self._session
                if session is None or session.scan_id != scan_id:
                    return  # replaced while we were finishing; the answer is stale
                # confirm_imports preserves order, so every index the browser
                # already holds still points at the same frame.
                session.scan = settled
                self._retarget(session)
            self._finish(scan_id, status=ConfirmStatus.DONE, seconds=time.perf_counter() - started)

    def _finish(self, scan_id: str, *, status: str, seconds: float, error: str | None = None) -> None:
        with self._lock:
            session = self._session
            if session is None or session.scan_id != scan_id:
                return
            session.confirm.status = status
            session.confirm.seconds = seconds
            session.confirm.error = error
            session.confirm.current = None
            if status == ConfirmStatus.DONE:
                session.confirm.done = session.confirm.total

    def _cancel_confirm(self) -> None:
        """Signal any running pass to stop. Called with the lock held.

        Signalling is enough: the pass notices at its next candidate, because
        the progress callback checks whether its scan is still the current one.
        We do not join the thread — it is holding no lock we need and joining
        would make a rescan wait out a 14 MB card read.
        """
        session = self._session
        if session is not None and session.confirm.running:
            session.confirm.status = ConfirmStatus.CANCELLED

    # ------------------------------------------------------------------
    # Culling (REQUIREMENTS.md 6.4)
    # ------------------------------------------------------------------

    def decide(self, scan_id: str, index: int, decision: Decision) -> Decision:
        """Record one decision, on disk before this returns.

        On disk *first*, then in memory: if the write fails, the UI must not
        show a decision that will not survive the next restart. It is one
        appended line and an fsync, well under a millisecond, so there is
        nothing to gain by being clever about it and a card's worth of work to
        lose by getting it wrong.
        """
        with self._lock:
            session = self.require(scan_id)
            self._refuse_if_writable(session)
            photo = session.photo(index)
            if index not in session.cull_order:
                raise NotCullableError(
                    f"{photo.primary.name} is not being offered for culling "
                    f"(it is {photo.dedup}). Nothing to decide."
                )
            if session.store is not None:
                session.store.record(PhotoKey.of(photo), decision)
            session.decisions[index] = decision
            return decision

    def tally(self, session: CardSession) -> Tally:
        """Keepers and what they will cost, against free space measured now.

        Scoped to the frames on offer, so "undecided" counts the work actually
        left rather than including the ones the cull already withheld.
        """
        return tally(
            session.scan.photos, session.decisions, self.settings,
            offered=frozenset(session.cull_order),
        )

    # ------------------------------------------------------------------
    # Previews, and staying ahead of the user
    # ------------------------------------------------------------------

    def preview(self, scan_id: str, index: int) -> tuple[bytes, str]:
        """The large preview for one frame, and whether the cache had it.

        Three paths, cheapest first. The cache key is the content hash
        (FR-2.3) and a card photo does not come with one, so:

        * hash known and cached - serve it, no card read at all;
        * hash known, not cached - render from the file, streaming;
        * hash unknown - :func:`render_and_hash`, which reads the 15 MB once
          and answers both questions instead of reading it twice (C-4).

        The hash learned on that third path is remembered on the session, so
        coming back to a frame is a cache hit rather than another second.
        """
        with self._lock:
            session = self.require(scan_id)
            photo = session.photo(index)
            known = session.hashes.get(index) or photo.content_hash

        data, digest, cache_state = self._render(photo, known)
        with self._lock:
            session = self._session
            if session is not None and session.scan_id == scan_id:
                if digest is not None:
                    session.hashes[index] = digest
                self._prefetch_after(session, index)
        return data, cache_state

    def _render(self, photo: CardPhoto, known: str | None) -> tuple[bytes, str | None, str]:
        cache = self.previews
        if known is not None:
            cached = cache.cached(known)
            if cached is not None:
                return cached.read_bytes(), known, "hit"
            return cache.ensure(photo.primary, known).read_bytes(), known, "miss"

        data, digest = render_and_hash(photo.primary, max_px=cache.max_px)
        cache.store(digest, data)
        return data, digest, "miss"

    def _prefetch_after(self, session: CardSession, index: int) -> None:
        """Queue the next few frames in cull order. Called with the lock held."""
        order = session.cull_order
        try:
            position = order.index(index)
        except ValueError:
            return  # not part of the cull, so nothing follows it
        for following in order[position + 1:position + 1 + PREFETCH_DEPTH]:
            self._submit_prefetch(session, following)

    def _submit_prefetch(self, session: CardSession, index: int) -> None:
        key = (session.scan_id, index)
        if key in self._prefetching:
            return
        digest = session.hashes.get(index) or session.photo(index).content_hash
        if digest is not None and self.previews.cached(digest) is not None:
            return  # already warm
        self._prefetching.add(key)
        self._previews.submit(self._prefetch, session.scan_id, index)

    def _prefetch(self, scan_id: str, index: int) -> None:
        """One background preview. Failure here is silence, on purpose.

        This is speculative work for a frame nobody has asked for yet. If it
        cannot be rendered, the request that eventually asks for it raises
        properly and the page draws one broken frame (FR-1.3c). Reporting a
        failed guess as well would be noise about something that may never be
        looked at.
        """
        try:
            with self._lock:
                session = self._session
                if session is None or session.scan_id != scan_id:
                    return
                photo = session.photo(index)
                known = session.hashes.get(index) or photo.content_hash
            _, digest, _ = self._render(photo, known)
            with self._lock:
                session = self._session
                if session is not None and session.scan_id == scan_id and digest:
                    session.hashes[index] = digest
        except (ThumbnailError, OSError, LookupError):
            pass
        finally:
            self._prefetching.discard((scan_id, index))

    # ------------------------------------------------------------------
    # Importing (REQUIREMENTS.md 6.6)
    # ------------------------------------------------------------------

    def plan(self, scan_id: str, *, location_slug: str | None = None) -> ImportPlan:
        """What the import would do. Reads EXIF headers; copies nothing.

        Cheap enough to serve on a request — a header read per keeper — and
        expensive enough to be worth showing before anything is written.
        """
        with self._lock:
            session = self.require(scan_id)
            scan, decisions = session.scan, dict(session.decisions)
        return plan_import(scan, decisions, self.settings, location_slug=location_slug)

    def start_import(
        self, scan_id: str, *, location_slug: str | None = None,
        allow_overrun: bool = False,
    ) -> ImportProgress:
        """Copy the keepers into the library, on a thread of its own.

        A full card is ~15 GB at 20-37 MB/s — nine minutes or so (C-4) — which
        is not something a request can hold open. Unlike the confirming pass
        this is **not cancelled by a rescan**: it is writing photographs, and
        stopping it half way to serve a fresher grid would be trading the thing
        that matters for the thing that is convenient. It finishes.
        """
        with self._lock:
            session = self.require(scan_id)
            if session.importing.running:
                return session.importing.snapshot()
            self._refuse_if_writable(session)
            if self._backup.running:
                raise BusyError(
                    "A backup is reading the library. Import when it has finished."
                )
            if self._archive.running:
                raise BusyError(
                    "The library is being archived. Import when it has finished."
                )
            if self._offload.running:
                raise BusyError(
                    "Photographs are being moved off the Mac. Import when that has finished."
                )
            session.importing = ImportProgress(status=ImportStatus.RUNNING)

        thread = threading.Thread(
            target=self._import,
            args=(scan_id, location_slug, allow_overrun),
            name="import",
            daemon=False,  # deliberately not a daemon: see start_import
        )
        with self._lock:
            self._import_thread = thread
        thread.start()
        return self.import_progress(scan_id)

    def import_progress(self, scan_id: str) -> ImportProgress:
        with self._lock:
            return self.require(scan_id).importing.snapshot()

    def _import(self, scan_id: str, location_slug: str | None, allow_overrun: bool) -> None:
        started = time.perf_counter()

        def progress(done: int, total: int, planned: object) -> None:
            with self._lock:
                session = self._session
                if session is None or session.scan_id != scan_id:
                    return
                session.importing.done = done - 1
                session.importing.total = total
                session.importing.current = getattr(planned, "original_name", None)
                session.importing.seconds = time.perf_counter() - started

        try:
            plan = self.plan(scan_id, location_slug=location_slug)
            # create=True: a first import into a library that has never had
            # one is an ordinary thing to do, not an error.
            with open_catalog(self.settings, create=True) as catalog:
                assert catalog is not None
                outcome = execute_import(
                    plan, catalog, allow_overrun=allow_overrun, progress=progress,
                )
        except Exception as exc:  # noqa: BLE001 — a failed import must not kill the app
            self._finish_import(
                scan_id, status=ImportStatus.FAILED,
                seconds=time.perf_counter() - started,
                error=f"{type(exc).__name__}: {exc}",
            )
            return

        self._finish_import(
            scan_id, status=ImportStatus.DONE,
            seconds=time.perf_counter() - started, outcome=outcome,
        )

    def _finish_import(
        self, scan_id: str, *, status: str, seconds: float,
        error: str | None = None, outcome: ImportOutcome | None = None,
    ) -> None:
        with self._lock:
            session = self._session
            if session is None or session.scan_id != scan_id:
                return
            session.importing.status = status
            session.importing.seconds = seconds
            session.importing.error = error
            session.importing.outcome = outcome
            session.importing.current = None
            if outcome is not None:
                session.importing.done = len(outcome.imported) + len(outcome.skipped) \
                    + len(outcome.failed)

    # ------------------------------------------------------------------
    # The card's mount (§4.1, macOS)
    # ------------------------------------------------------------------

    @staticmethod
    def _refuse_if_writable(session: CardSession) -> None:
        """Asked of the mount as it is now, not as it was at scan time."""
        dcim = session.scan.location.dcim_root
        if dcim is not None and card_writable(dcim):
            raise CardWritableError(
                "The card is mounted read-write, so macOS can write to it. "
                "Use Remount read-only at the top of the page first."
            )

    def remount_card(self) -> CardSession:
        """Have macOS mount the card read-only, then read it again.

        Anything reading the card would make the unmount fail, so the
        confirming pass is stopped first and an import refuses it outright.
        """
        with self._lock:
            session = self._session
            if session is not None and session.importing.running:
                raise BusyError("An import is reading the card. Remount when it has finished.")
            self._cancel_confirm()
            location = session.scan.location if session is not None else None
        if location is None or location.dcim_root is None:
            location = detect_card(self.settings)
        if not location.found or location.dcim_root is None:
            raise CardError("No card is attached.")
        confirming = self._confirm_thread
        if confirming is not None and confirming.is_alive():
            confirming.join(timeout=30.0)
        remount_read_only(location.dcim_root)
        return self.scan(force=True)

    # ------------------------------------------------------------------
    # Backing up (photo_manager.backup)
    # ------------------------------------------------------------------

    def start_backup(self) -> BackupProgress:
        """Take a snapshot to ``backup_root``, on a thread of its own.

        What would be refused — no destination, the drive not attached — is
        refused here, before the request returns, so the page can say so at
        once rather than report a failure a moment later.
        """
        with self._lock:
            if self._backup.running:
                return self._backup.snapshot()
            session = self._session
            if session is not None and session.importing.running:
                raise BusyError(
                    "An import is writing to the library. Back up when it has finished."
                )
            if self._archive.running:
                raise BusyError(
                    "The library is being archived. Back up when it has finished."
                )
            if self._offload.running:
                raise BusyError(
                    "Photographs are being moved off the Mac. Back up when that has finished."
                )
        # Outside the lock, and bounded: asking a hung drive must not hang this.
        drive = self.backup_status()
        if not drive.configured or not drive.attached:
            raise BackupError(drive.message)
        with self._lock:
            if self._backup.running:
                return self._backup.snapshot()
            self._backup = BackupProgress(status=ImportStatus.RUNNING)
            thread = threading.Thread(
                target=self._run_backup, name="backup",
                daemon=False,  # it is writing to a drive; let it finish
            )
            self._backup_thread = thread
        thread.start()
        return self.backup_progress()

    def backup_progress(self) -> BackupProgress:
        with self._lock:
            return self._backup.snapshot()

    # ------------------------------------------------------------------
    # Archiving (photo_manager.archive)
    # ------------------------------------------------------------------

    def start_archive(self) -> ArchiveProgress:
        """Copy whatever the archive drive is missing, on a thread of its own.

        Refused at once — before the request returns — when there is no archive
        drive configured, it is not attached, or an import or backup is busy
        with the same library.
        """
        with self._lock:
            if self._archive.running:
                return self._archive.snapshot()
            self._refuse_if_library_busy("Archive")
        drive = self.archive_status()
        if not drive.configured or not drive.attached:
            raise ArchiveError(drive.message)
        with self._lock:
            if self._archive.running:
                return self._archive.snapshot()
            self._refuse_if_library_busy("Archive")
            self._archive = ArchiveProgress(status=ImportStatus.RUNNING)
            thread = threading.Thread(
                target=self._run_archive, name="archive",
                daemon=False,  # it is writing to a drive; let it finish
            )
            self._archive_thread = thread
        thread.start()
        return self.archive_progress()

    def archive_progress(self) -> ArchiveProgress:
        with self._lock:
            return self._archive.snapshot()

    def archive_status(self) -> ArchiveStatus:
        """As :func:`archive.archive_status`, but never stuck on a hung drive."""
        def ask() -> ArchiveStatus:
            with open_catalog(self.settings) as catalog:
                return archive.archive_status(self.settings, catalog)

        try:
            return self._bounded("archive", ask)
        except FutureTimeout:
            return ArchiveStatus(
                configured=True, attached=False,
                destination=self.settings.archive_root,
                message=(
                    "The archive drive is not responding. Eject it — or unplug it if "
                    "ejecting hangs too — and plug it back in."
                ),
            )

    def archived_hashes(self) -> frozenset[str] | None:
        """The content hashes of every photograph verified on the archive drive.

        From the catalog, not the drive, so it is known with the drive in a
        drawer. None when there is no archive configured or no catalog yet.
        """
        if self.settings.archive_root is None:
            return None
        with open_catalog(self.settings) as catalog:
            return None if catalog is None else catalog.archive.archived_hashes()

    def _refuse_if_library_busy(self, doing: str) -> None:
        """Called under the lock: one writer to the library at a time."""
        session = self._session
        if session is not None and session.importing.running:
            raise BusyError(f"An import is writing to the library. {doing} when it has finished.")
        if self._backup.running:
            raise BusyError(f"A backup is reading the library. {doing} when it has finished.")
        if self._archive.running:
            raise BusyError(f"The library is being archived. {doing} when it has finished.")
        if self._offload.running:
            raise BusyError(
                f"Photographs are being moved off the Mac. {doing} when that has finished."
            )

    # ------------------------------------------------------------------
    # Freeing up Mac space (photo_manager.offload)
    # ------------------------------------------------------------------

    def offload_plan(self) -> offload.OffloadPlan | None:
        """What *Free up Mac space* would do now; None without an archive or
        a catalog. Reads the catalog and the Mac only — never the drive."""
        if self.settings.archive_root is None:
            return None
        with open_catalog(self.settings) as catalog:
            return None if catalog is None else offload.plan_offload(self.settings, catalog)

    def start_offload(self) -> OffloadProgress:
        """Take the plan's photographs off the Mac, on a thread of its own.

        The plan is made afresh on that thread rather than taken from the
        page, so what is removed is what the rule says *now*.
        """
        with self._lock:
            if self._offload.running:
                return self._offload.snapshot()
            self._refuse_if_library_busy("Free up space")
        drive = self.archive_status()
        if not drive.configured or not drive.attached:
            raise OffloadError(f"Nothing was removed. {drive.message}")
        with self._lock:
            if self._offload.running:
                return self._offload.snapshot()
            self._refuse_if_library_busy("Free up space")
            self._offload = OffloadProgress(status=ImportStatus.RUNNING)
            thread = threading.Thread(target=self._run_offload, name="offload", daemon=False)
            self._offload_thread = thread
        thread.start()
        return self.offload_progress()

    def offload_progress(self) -> OffloadProgress:
        with self._lock:
            return self._offload.snapshot()

    def _run_offload(self) -> None:
        started = time.perf_counter()

        def progress(done: int, total: int, name: str) -> None:
            with self._lock:
                self._offload.done = done - 1
                self._offload.total = total
                self._offload.current = name
                self._offload.seconds = time.perf_counter() - started

        try:
            with open_catalog(self.settings) as catalog:
                if catalog is None:
                    raise OffloadError("There is no catalog yet, so nothing to free up.")
                plan = offload.plan_offload(self.settings, catalog)
                result = offload.execute_offload(
                    self.settings, catalog, plan, progress=progress
                )
        except (OffloadError, OSError) as exc:
            status, error, result = ImportStatus.FAILED, str(exc), None
        else:
            if result.ok:
                status, error = ImportStatus.DONE, None
            else:
                status = ImportStatus.FAILED
                error = (
                    f"{len(result.failures)} photograph(s) were left on the Mac. "
                    f"First: {result.failures[0]}"
                )
        with self._lock:
            self._offload.status = status
            self._offload.error = error
            self._offload.result = result
            self._offload.current = None
            self._offload.seconds = time.perf_counter() - started
            if result is not None:
                self._offload.done = self._offload.total

    def _run_archive(self) -> None:
        started = time.perf_counter()

        def progress(done: int, total: int, name: str) -> None:
            with self._lock:
                self._archive.done = done - 1
                self._archive.total = total
                self._archive.current = name
                self._archive.seconds = time.perf_counter() - started

        try:
            with open_catalog(self.settings, create=True) as catalog:
                assert catalog is not None
                result = archive.run_archive(self.settings, catalog, progress=progress)
        except (ArchiveError, OSError) as exc:
            status, error, result = ImportStatus.FAILED, str(exc), None
        else:
            if not result.failures:
                status = ImportStatus.DONE
                error = result.snapshot_problem
            else:
                status = ImportStatus.FAILED
                error = (
                    f"{len(result.failures)} file(s) did not reach the archive drive "
                    f"intact; they are still owed and will be tried again next time. "
                    f"First: {result.failures[0]}"
                )
        with self._lock:
            self._archive.status = status
            self._archive.error = error
            self._archive.result = result
            self._archive.current = None
            self._archive.seconds = time.perf_counter() - started
            if result is not None:
                self._archive.done = self._archive.total

    def busy(self) -> list[str]:
        """What stopping the app now would cut short, in words for a dialog.

        A backup left unfinished is a ``.partial`` folder and an import is
        transactional per file, so neither loses anything — but both would
        have to be run again, and somebody quitting should know that first.
        """
        with self._lock:
            session, backing_up = self._session, self._backup.running
            archiving = self._archive.running
            offloading = self._offload.running
        work: list[str] = []
        if backing_up:
            work.append("a backup")
        if archiving:
            work.append("an archive to the drive")
        if offloading:
            work.append("freeing up Mac space")
        if session is not None and session.importing.running:
            work.append("an import")
        if session is not None and session.confirm.running:
            work.append("a hash check of the card")
        return work

    def backup_status(self) -> backup.BackupStatus:
        """As :func:`backup.status`, but never stuck on a drive that has hung."""
        def ask() -> backup.BackupStatus:
            with open_catalog(self.settings) as catalog:
                hashes = [] if catalog is None else [
                    row[0] for row in catalog.connection.execute("SELECT content_hash FROM photo")
                ]
            return backup.status(self.settings, hashes)

        try:
            return self._bounded("backup", ask)
        except FutureTimeout:
            return backup.BackupStatus(
                configured=True, attached=False,
                destination=str(self.settings.backup_root),
                message=(
                    "The backup drive is not responding. Eject it — or unplug it if "
                    "ejecting hangs too — and plug it back in."
                ),
            )

    def backed_up_hashes(self) -> frozenset[str] | None:
        """What the last backup holds, by content hash — or None when there is
        no backup to ask about or the drive does not answer in time, which a
        page must show as *unknown* rather than as *not backed up*."""
        try:
            return self._bounded("backup-hashes", lambda: backup.backed_up_hashes(self.settings))
        except FutureTimeout:
            return None

    def catalog_hashes(self) -> dict[int, str]:
        """Every catalogued photograph's content hash, by its row id."""
        with open_catalog(self.settings) as catalog:
            if catalog is None:
                return {}
            return dict(catalog.connection.execute("SELECT id, content_hash FROM photo"))

    def card_location(self) -> CardLocation:
        """Where the card is now, located rather than scanned, within a time limit."""
        try:
            return self._bounded("card", lambda: detect_card(self.settings))
        except FutureTimeout:
            return CardLocation(
                status="not-responding",
                message="A drive attached to this Mac is not responding, so the card "
                        "cannot be looked for. Unplug and replug whatever was last attached.",
            )

    def _bounded(self, name: str, question):
        """Ask *question* on this name's own worker; raise FutureTimeout if it
        does not answer in DRIVE_TIMEOUT. A question still stuck from an earlier
        call is waited on again rather than asked twice."""
        with self._lock:
            pool, pending = self._probes.get(name, (None, None))
            if pool is None:
                pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"probe-{name}")
            if pending is None or pending.done():
                pending = pool.submit(question)
            self._probes[name] = (pool, pending)
        return pending.result(timeout=DRIVE_TIMEOUT)

    def _run_backup(self) -> None:
        started = time.perf_counter()

        def progress(done: int, total: int, name: str) -> None:
            with self._lock:
                self._backup.done = done - 1
                self._backup.total = total
                self._backup.current = name
                self._backup.seconds = time.perf_counter() - started

        try:
            result = backup.take_snapshot(self.settings, progress=progress)
        except (BackupError, OSError) as exc:
            status, error, result = ImportStatus.FAILED, str(exc), None
        else:
            if result.ok:
                status, error = ImportStatus.DONE, None
            else:
                status = ImportStatus.FAILED
                error = (
                    f"{len(result.failures)} file(s) did not verify, so the snapshot "
                    f"was left unfinished at {result.snapshot.name}. First: "
                    f"{result.failures[0]}"
                )
        with self._lock:
            self._backup.status = status
            self._backup.error = error
            self._backup.result = result
            self._backup.current = None
            self._backup.seconds = time.perf_counter() - started
            if result is not None and result.ok:
                self._backup.done = self._backup.total = result.files

    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        with self._lock:
            self._cancel_confirm()
        thread = self._confirm_thread
        if thread is not None:
            thread.join(timeout=5.0)
        # An import in flight is writing photographs. It gets longer, and it is
        # not a daemon thread, so the process waits for it rather than tearing
        # it out mid-copy.
        importer = self._import_thread
        if importer is not None and importer.is_alive():
            importer.join(timeout=300.0)
        backing_up = self._backup_thread
        if backing_up is not None and backing_up.is_alive():
            backing_up.join(timeout=600.0)
        archiving = self._archive_thread
        if archiving is not None and archiving.is_alive():
            archiving.join(timeout=600.0)
        offloading = self._offload_thread
        if offloading is not None and offloading.is_alive():
            offloading.join(timeout=600.0)
        self._previews.shutdown(wait=False)
        for pool, _ in self._probes.values():
            pool.shutdown(wait=False, cancel_futures=True)
