"""Content hashing.

The hash is a photo's identity, and three separate requirements lean on it:
duplicate detection when the card is re-inserted (C-3), verify-after-copy before
an import commits (FR-6.4), and the thumbnail cache key (FR-2.3). So the two
properties worth testing hard are that it is *stable* and that it *discriminates*.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import fixtures
import pytest

from photo_manager.hashing import content_hash

#: SHA-256 of nothing at all. Pinned rather than computed, so a change to the
#: algorithm shows up as a failure instead of quietly agreeing with itself.
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_hash_is_lowercase_hex_of_the_expected_width() -> None:
    """The shape the catalog's content_hash column expects."""
    digest = content_hash(fixtures.BURST_FIRST)

    assert len(digest) == 64
    assert digest == digest.lower()
    assert set(digest) <= set("0123456789abcdef")


def test_an_empty_file_hashes_to_the_known_value(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")

    assert content_hash(empty) == EMPTY_SHA256


def test_hash_is_stable_across_runs() -> None:
    """Called once during culling and again after copying — they must agree."""
    assert content_hash(fixtures.BURST_FIRST) == content_hash(fixtures.BURST_FIRST)


@pytest.mark.parametrize("chunk_size", [1, 7, 4096, 1 << 20, 1 << 24])
def test_chunk_size_does_not_change_the_hash(chunk_size: int) -> None:
    """A 33 MB RAF is read in chunks; the boundaries must not be visible."""
    expected = hashlib.sha256(fixtures.BURST_FIRST.read_bytes()).hexdigest()

    assert content_hash(fixtures.BURST_FIRST, chunk_size=chunk_size) == expected


def test_a_large_file_hashes_in_chunks() -> None:
    """The 33 MB RAF, to prove nothing assumes a file fits comfortably in memory."""
    assert content_hash(fixtures.RAW_PAIR_RAF) == hashlib.sha256(
        fixtures.RAW_PAIR_RAF.read_bytes()
    ).hexdigest()


def test_visually_similar_frames_hash_differently() -> None:
    """Two frames 2 seconds apart in the same burst. Near-identical, not identical.

    If dedup collapsed these, culling would silently lose frames — which is why
    C-3's check is a content hash and not a timestamp or a filename.
    """
    assert fixtures.BURST_GAP.total_seconds() == 2
    assert content_hash(fixtures.BURST_FIRST) != content_hash(fixtures.BURST_SECOND)


def test_every_fixture_has_a_distinct_hash() -> None:
    digests = {photo.name: content_hash(photo) for photo in fixtures.ALL_PHOTOS}

    assert len(set(digests.values())) == len(fixtures.ALL_PHOTOS)


def test_a_byte_identical_copy_hashes_the_same(tmp_path: Path) -> None:
    """This is exactly FR-6.4: an import commits only when the copy matches."""
    destination = tmp_path / "2026-07-13_DSCF0807.jpg"
    shutil.copy2(fixtures.BURST_FIRST, destination)

    assert content_hash(destination) == content_hash(fixtures.BURST_FIRST)


def test_a_single_changed_byte_changes_the_hash(tmp_path: Path) -> None:
    """A half-written copy must never pass verification."""
    original = fixtures.BURST_FIRST.read_bytes()
    damaged = tmp_path / "damaged.jpg"
    damaged.write_bytes(original[:-1] + bytes([original[-1] ^ 0xFF]))

    assert content_hash(damaged) != content_hash(fixtures.BURST_FIRST)


def test_a_truncated_copy_does_not_hash_the_same(tmp_path: Path) -> None:
    """The truncated fixture is the first 64 KB of a real frame."""
    assert content_hash(fixtures.TRUNCATED) != content_hash(fixtures.FULL_SIZE_ORIGINAL)


def test_metadata_is_part_of_the_hash(tmp_path: Path) -> None:
    """Whole-file, not pixels.

    Two files with identical image data but different EXIF are different files
    as far as FR-6.4 is concerned, and must not be confused for one another.
    """
    from PIL import Image

    with Image.open(fixtures.BURST_FIRST) as image:
        pixels = image.copy()

    without_exif = tmp_path / "plain.jpg"
    with_exif = tmp_path / "tagged.jpg"
    pixels.save(without_exif, "JPEG", quality=95)
    exif = Image.Exif()
    exif[0x010E] = "a description that changes nothing visible"
    pixels.save(with_exif, "JPEG", quality=95, exif=exif)

    assert content_hash(without_exif) != content_hash(with_exif)


def test_a_missing_file_raises_rather_than_returning_a_hash(tmp_path: Path) -> None:
    """A hash of a file that isn't there would be worse than an error."""
    with pytest.raises(FileNotFoundError):
        content_hash(tmp_path / "not-here.jpg")


@pytest.mark.parametrize("chunk_size", [0, -1])
def test_a_nonsense_chunk_size_is_refused(chunk_size: int) -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        content_hash(fixtures.BURST_FIRST, chunk_size=chunk_size)
