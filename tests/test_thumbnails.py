"""The two-tier thumbnail strategy.

The test that matters most here is the byte-counting one. FR-2.1's whole claim —
and with it the viability of culling a thousand-frame card — rests on the embedded
thumbnail being reachable from the file's header. If that ever stops being true
the grid goes from seconds to minutes, so it is measured rather than asserted.

Nothing in this file touches the memory card or the real library.
"""

from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import BinaryIO

import fixtures
import pytest
from PIL import Image

from photo_manager import thumbnails
from photo_manager.config import Settings
from photo_manager.thumbnails import (
    DEFAULT_HEADER_WINDOW,
    ThumbnailCache,
    ThumbnailError,
    ThumbnailWorker,
    embedded_thumbnail,
    embedded_thumbnail_from,
    generate_thumbnail,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


class CountingReader:
    """A file object that remembers how much was read through it."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        self.bytes_read += len(data)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._stream.seek(offset, whence)

    def tell(self) -> int:
        return self._stream.tell()

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True


def size_of(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.size


# ----------------------------------------------------------------------
# Tier 1 — the embedded EXIF thumbnail (FR-2.1)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "photo",
    [fixtures.FULL_SIZE_ORIGINAL, fixtures.BURST_FIRST, fixtures.RENAMED_EVENT],
    ids=lambda p: p.name,
)
def test_camera_written_files_yield_an_embedded_thumbnail(photo: Path) -> None:
    data = embedded_thumbnail(photo)

    assert data is not None
    assert data.startswith(b"\xff\xd8")  # a real JPEG, servable as-is
    assert size_of(data) == (160, 120)


def test_extraction_reads_only_the_header() -> None:
    """FR-2.1, measured. This is the basis of the whole culling workflow.

    Decoding the frame would mean 14.5 MB; the thumbnail is in the first 128 KB.
    Across a card that is the difference between ~150 MB and ~16 GB.
    """
    size = fixtures.FULL_SIZE_ORIGINAL.stat().st_size

    with fixtures.FULL_SIZE_ORIGINAL.open("rb") as stream:
        counting = CountingReader(stream)
        data = embedded_thumbnail_from(counting)

    assert data is not None
    assert counting.bytes_read <= DEFAULT_HEADER_WINDOW + 64
    assert counting.bytes_read < size * 0.01


def test_extraction_never_decodes_the_full_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(self: Image.Image, *args: object, **kwargs: object) -> object:
        raise AssertionError("tier 1 must not decode image data")

    monkeypatch.setattr(Image.Image, "load", explode)

    assert embedded_thumbnail(fixtures.FULL_SIZE_ORIGINAL) is not None


def test_a_file_without_an_embedded_thumbnail_returns_none() -> None:
    """FR-2.1a. Three files in the real library are like this.

    Absence must be a None the caller can fall through on, never an exception
    and never a hole in the grid.
    """
    assert embedded_thumbnail(fixtures.WITHOUT_EMBEDDED_THUMBNAIL) is None


def test_a_raf_yields_its_preview_thumbnail() -> None:
    """A RAF's embedded preview is itself a JPEG with a thumbnail inside it.

    So tier 1 stays a header read here too — the 5 MB preview body is skipped.
    """
    data = embedded_thumbnail(fixtures.RAW_PAIR_RAF)

    assert data is not None
    assert data.startswith(b"\xff\xd8")


def test_raf_extraction_also_reads_only_headers() -> None:
    size = fixtures.RAW_PAIR_RAF.stat().st_size

    with fixtures.RAW_PAIR_RAF.open("rb") as stream:
        counting = CountingReader(stream)
        data = embedded_thumbnail_from(counting)

    assert data is not None
    assert counting.bytes_read < size * 0.01


def test_a_truncated_file_still_gives_the_grid_something() -> None:
    """Worth knowing: the thumbnail is in front of the damage.

    The same file makes the EXIF reader raise, but its embedded thumbnail is
    intact, so a broken frame can still be shown rather than left blank.
    """
    assert embedded_thumbnail(fixtures.TRUNCATED) is not None


def test_a_window_too_small_for_the_exif_block_falls_through() -> None:
    """Not an error — FR-2.1a's path, reached for a different reason."""
    assert embedded_thumbnail(fixtures.FULL_SIZE_ORIGINAL, window=2048) is None


@pytest.mark.parametrize(
    "contents",
    [b"", b"not an image", b"\xff\xd8\xff\xe1\x00\x04rubbish", b"\xff\xd8" + b"\x00" * 500],
    ids=["empty", "text", "bad-app1", "jpeg-magic-only"],
)
def test_nonsense_input_returns_none_rather_than_raising(
    tmp_path: Path, contents: bytes
) -> None:
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(contents)

    assert embedded_thumbnail(broken) is None


# ----------------------------------------------------------------------
# Tier 2 — generated thumbnails (FR-2.2)
# ----------------------------------------------------------------------


def test_generated_thumbnail_has_the_configured_long_edge() -> None:
    data = generate_thumbnail(fixtures.FULL_SIZE_ORIGINAL, max_px=400)

    width, height = size_of(data)
    assert max(width, height) == 400


def test_generated_thumbnail_keeps_the_aspect_ratio() -> None:
    original = fixtures.FULL_SIZE_DIMENSIONS
    width, height = size_of(generate_thumbnail(fixtures.FULL_SIZE_ORIGINAL, max_px=400))

    assert width / height == pytest.approx(original[0] / original[1], rel=0.01)


def test_generation_uses_draft_to_avoid_a_full_decode(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-2.2 names draft() specifically, and for good reason.

    It scales in the DCT domain during decode, so a 26 MP frame is never
    materialised. Without it, a thousand frames is minutes of work and a lot of RAM.
    """
    # JpegImageFile overrides draft() — patching Image.Image would only catch
    # thumbnail()'s own internal call and miss the one that does the work.
    from PIL.JpegImagePlugin import JpegImageFile

    seen: list[tuple[object, tuple[int, int]]] = []
    original = JpegImageFile.draft

    def spy(self: JpegImageFile, mode: object, size: object) -> object:
        result = original(self, mode, size)
        seen.append((size, self.size))
        return result

    monkeypatch.setattr(JpegImageFile, "draft", spy)
    generate_thumbnail(fixtures.FULL_SIZE_ORIGINAL, max_px=400)

    assert seen, "draft() was never called on the JPEG"
    requested, decoded_size = seen[0]
    # The *actual* target box, not a square one. draft() accepts a
    # power-of-two reduction only while both dimensions stay at or above what
    # it was asked for, so a square request measures against the short edge and
    # settles for one step less than it could (WU-2.3).
    width, height = fixtures.FULL_SIZE_DIMENSIONS
    assert requested == (400, round(400 * height / width))
    # The decoder really did hand back something smaller than 6240 x 4160.
    assert max(decoded_size) < max(fixtures.FULL_SIZE_DIMENSIONS)


def test_draft_is_asked_for_the_real_box_so_it_can_take_the_bigger_reduction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measured half-second, pinned.

    At 1560 px a 6240 x 4160 frame is exactly a quarter-size decode, so the
    decoder can hand back the finished preview with no resampling at all. Ask
    for a square (1560, 1560) instead and the 1040-pixel short edge fails the
    test, the decoder halves instead of quartering, and 3120 x 2080 has to be
    resampled down: **956 ms against 453 ms**, for a pixel-identical result.
    """
    from PIL.JpegImagePlugin import JpegImageFile

    decoded: list[tuple[int, int]] = []
    original = JpegImageFile.draft

    def spy(self: JpegImageFile, mode: object, size: object) -> object:
        result = original(self, mode, size)
        decoded.append(self.size)
        return result

    monkeypatch.setattr(JpegImageFile, "draft", spy)
    generate_thumbnail(fixtures.FULL_SIZE_ORIGINAL, max_px=1560)

    width, height = fixtures.FULL_SIZE_DIMENSIONS
    assert decoded[0] == (width // 4, height // 4)


def test_a_smaller_source_is_not_upscaled() -> None:
    """The downscaled fixtures are 1600 px; a 400 px thumbnail is still 400 px."""
    width, height = size_of(generate_thumbnail(fixtures.BURST_FIRST, max_px=400))

    assert max(width, height) == 400


def test_orientation_is_applied(tmp_path: Path) -> None:
    """A sideways thumbnail is a defect, and no fixture is rotated.

    Orientation 6 means 'rotate 90° clockwise to display', so a landscape
    source must come back portrait.
    """
    rotated = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.new("RGB", (800, 400), "red").save(rotated, "JPEG", exif=exif)

    width, height = size_of(generate_thumbnail(rotated, max_px=400))

    assert height > width


def test_a_file_with_no_exif_thumbnails_fine(tmp_path: Path) -> None:
    plain = tmp_path / "plain.jpg"
    Image.new("RGB", (1200, 900), "blue").save(plain, "JPEG")

    assert max(size_of(generate_thumbnail(plain, max_px=400))) == 400


def test_a_raf_can_be_thumbnailed() -> None:
    """Via its embedded preview. Keeps a lone RAF from being a hole in the grid."""
    assert max(size_of(generate_thumbnail(fixtures.RAW_PAIR_RAF, max_px=400))) == 400


def test_an_unreadable_file_raises_a_handled_error() -> None:
    with pytest.raises(ThumbnailError, match=fixtures.TRUNCATED.name):
        generate_thumbnail(fixtures.TRUNCATED, max_px=400)


@pytest.mark.parametrize("max_px", [0, -1])
def test_a_nonsense_size_is_refused(max_px: int) -> None:
    with pytest.raises(ValueError, match="max_px"):
        generate_thumbnail(fixtures.BURST_FIRST, max_px=max_px)


# ----------------------------------------------------------------------
# The disk cache (FR-2.3)
# ----------------------------------------------------------------------


@pytest.fixture
def cache(tmp_path: Path) -> ThumbnailCache:
    return ThumbnailCache(tmp_path / "thumbnails", max_px=400)


def test_cache_paths_are_sharded_and_size_stamped(cache: ThumbnailCache) -> None:
    path = cache.path_for(HASH_A)

    assert path.parent.name == HASH_A[:2]
    assert path.name == f"{HASH_A}-400.jpg"


def test_changing_the_configured_size_changes_the_cache_key(tmp_path: Path) -> None:
    """Raising thumbnail_max_px must regenerate, not serve the old smaller tiles."""
    small = ThumbnailCache(tmp_path / "t", max_px=400)
    large = ThumbnailCache(tmp_path / "t", max_px=800)

    assert small.path_for(HASH_A) != large.path_for(HASH_A)


def test_cached_returns_none_until_something_is_stored(cache: ThumbnailCache) -> None:
    assert cache.cached(HASH_A) is None

    cache.store(HASH_A, b"pretend jpeg")

    assert cache.cached(HASH_A) == cache.path_for(HASH_A)


def test_store_writes_the_bytes_and_leaves_no_temporary_files(
    cache: ThumbnailCache,
) -> None:
    """Atomic, so a crash never leaves a half-thumbnail to be served forever."""
    path = cache.store(HASH_A, b"pretend jpeg")

    assert path.read_bytes() == b"pretend jpeg"
    assert list(cache.cache_dir.rglob("*.tmp")) == []


def test_ensure_generates_once_and_then_hits_the_cache(
    cache: ThumbnailCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'Cache hits skip regeneration' — the acceptance criterion, directly."""
    calls: list[Path] = []
    real = thumbnails.generate_thumbnail

    def counting(path: Path, *, max_px: int) -> bytes:
        calls.append(Path(path))
        return real(path, max_px=max_px)

    monkeypatch.setattr(thumbnails, "generate_thumbnail", counting)

    first = cache.ensure(fixtures.BURST_FIRST, HASH_A)
    second = cache.ensure(fixtures.BURST_FIRST, HASH_A)

    assert first == second
    assert len(calls) == 1
    assert max(size_of(first.read_bytes())) == 400


def test_the_cache_is_shared_between_the_card_and_the_library(
    cache: ThumbnailCache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-2.3: keyed by content, so importing a photo does not re-thumbnail it.

    Same bytes at two different paths — 'on the card' and 'in the library' — is
    one cache entry and one decode.
    """
    on_the_card = tmp_path / "card" / "DSCF1029.JPG"
    in_the_library = tmp_path / "library" / "2026-09-06_DSCF1029.jpg"
    for destination in (on_the_card, in_the_library):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(fixtures.BURST_FIRST.read_bytes())

    calls: list[Path] = []
    real = thumbnails.generate_thumbnail
    monkeypatch.setattr(
        thumbnails,
        "generate_thumbnail",
        lambda path, *, max_px: (calls.append(Path(path)), real(path, max_px=max_px))[1],
    )

    from_card = cache.ensure(on_the_card, HASH_A)
    from_library = cache.ensure(in_the_library, HASH_A)

    assert from_card == from_library
    assert len(calls) == 1


def test_different_photos_get_different_entries(cache: ThumbnailCache) -> None:
    first = cache.ensure(fixtures.BURST_FIRST, HASH_A)
    second = cache.ensure(fixtures.BURST_SECOND, HASH_B)

    assert first != second
    assert first.read_bytes() != second.read_bytes()


def test_two_threads_storing_the_same_photo_both_succeed() -> None:
    """Identical frames are not a hypothetical — a card holds bursts.

    The temporary filename used to be keyed on the process id alone, which was
    safe only because the one caller deduped by hash before submitting. When
    WU-2.3's preview prefetch started reading files whose hashes it did not
    know yet, three threads landed on one hash and two of them deleted the
    winner's temporary file on the way out.
    """
    import tempfile
    from concurrent.futures import ThreadPoolExecutor

    with tempfile.TemporaryDirectory() as directory:
        cache = ThumbnailCache(directory, max_px=400)
        payload = generate_thumbnail(fixtures.BURST_FIRST, max_px=400)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: cache.store(HASH_A, payload), range(4)))

    assert len({str(path) for path in results}) == 1
    assert all(path.name.endswith(".jpg") for path in results)


def test_an_unreadable_source_does_not_leave_a_cache_entry(
    cache: ThumbnailCache,
) -> None:
    with pytest.raises(ThumbnailError):
        cache.ensure(fixtures.TRUNCATED, HASH_A)

    assert cache.cached(HASH_A) is None


def test_clear_removes_everything_it_made(cache: ThumbnailCache) -> None:
    cache.ensure(fixtures.BURST_FIRST, HASH_A)
    cache.ensure(fixtures.BURST_SECOND, HASH_B)

    assert cache.clear() == 2
    assert cache.cached(HASH_A) is None


def test_clear_on_a_cache_that_was_never_used(cache: ThumbnailCache) -> None:
    assert cache.clear() == 0


def test_an_empty_hash_is_refused(cache: ThumbnailCache) -> None:
    with pytest.raises(ValueError, match="content_hash"):
        cache.path_for("")


def test_cache_takes_its_location_and_size_from_settings(settings: Settings) -> None:
    cache = ThumbnailCache.from_settings(settings)

    assert cache.cache_dir == settings.thumbnail_cache_dir
    assert cache.max_px == settings.thumbnail_max_px


# ----------------------------------------------------------------------
# Background generation (FR-2.2, FR-2.4)
# ----------------------------------------------------------------------


def test_the_worker_generates_into_the_cache(cache: ThumbnailCache) -> None:
    with ThumbnailWorker(cache) as worker:
        result = worker.submit(fixtures.BURST_FIRST, HASH_A).result(timeout=30)

    assert result == cache.path_for(HASH_A)
    assert max(size_of(result.read_bytes())) == 400


def test_submitting_does_not_block_the_caller(
    cache: ThumbnailCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-2.4: the grid keeps painting while thumbnails are being made.

    The generator is held open until this thread says so. If submit() waited on
    it, the test would deadlock rather than fail — so there is a timeout.
    """
    released = threading.Event()
    started = threading.Event()

    def slow(path: Path, *, max_px: int) -> bytes:
        started.set()
        assert released.wait(timeout=30), "generator was never released"
        return b"pretend jpeg"

    monkeypatch.setattr(thumbnails, "generate_thumbnail", slow)

    with ThumbnailWorker(cache) as worker:
        future = worker.submit(fixtures.BURST_FIRST, HASH_A)

        # The caller is still running while the worker is stuck mid-generation.
        assert started.wait(timeout=10)
        assert not future.done()
        work_done_meanwhile = sum(range(1000))

        released.set()
        assert future.result(timeout=30) == cache.path_for(HASH_A)

    assert work_done_meanwhile == 499500


def test_asking_twice_for_one_photo_does_not_decode_it_twice(
    cache: ThumbnailCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grid scrolled back over itself must not re-queue what it already has."""
    released = threading.Event()
    calls: list[int] = []

    def slow(path: Path, *, max_px: int) -> bytes:
        calls.append(1)
        assert released.wait(timeout=30)
        return b"pretend jpeg"

    monkeypatch.setattr(thumbnails, "generate_thumbnail", slow)

    with ThumbnailWorker(cache, max_workers=4) as worker:
        first = worker.submit(fixtures.BURST_FIRST, HASH_A)
        second = worker.submit(fixtures.BURST_FIRST, HASH_A)

        assert first is second
        released.set()
        first.result(timeout=30)

    assert len(calls) == 1


def test_one_bad_file_does_not_stop_the_others(cache: ThumbnailCache) -> None:
    """A card with one corrupt frame still fills the rest of the grid."""
    with ThumbnailWorker(cache, max_workers=2) as worker:
        broken = worker.submit(fixtures.TRUNCATED, HASH_A)
        good = worker.submit(fixtures.BURST_FIRST, HASH_B)

        with pytest.raises(ThumbnailError):
            broken.result(timeout=30)
        assert good.result(timeout=30).is_file()


def test_the_worker_runs_jobs_off_the_calling_thread(cache: ThumbnailCache) -> None:
    caller = threading.current_thread().name
    seen: list[str] = []

    with ThumbnailWorker(cache) as worker:
        def record(path: Path, *, max_px: int) -> bytes:
            seen.append(threading.current_thread().name)
            return b"pretend jpeg"

        original = thumbnails.generate_thumbnail
        thumbnails.generate_thumbnail = record  # type: ignore[assignment]
        try:
            worker.submit(fixtures.BURST_FIRST, HASH_A).result(timeout=30)
        finally:
            thumbnails.generate_thumbnail = original  # type: ignore[assignment]

    assert seen and seen[0] != caller
