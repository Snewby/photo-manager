"""Content hashing.

The hash is a photo's identity. Three things depend on it:

* **Duplicate detection** (C-3). The card is not wiped between sessions, so
  re-inserting it re-presents photos that are already in the library. The hash
  is what makes "already imported" a fact rather than a guess about filenames.
* **Verify-after-copy** (§4.1, FR-6.4). An import is only committed once the
  bytes at the destination hash to the same value as the bytes on the card.
* **The thumbnail cache key** (FR-2.3), so the card view and the library view
  never thumbnail the same photo twice.

It is a hash of the **whole file**, metadata included, not of the decoded pixels.
That is deliberate: FR-6.4 needs to prove a copy is byte-identical, and a
pixel hash would happily call a re-encoded file identical to its original.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

__all__ = ["content_hash", "hash_stream", "DEFAULT_CHUNK_SIZE"]

#: Read size. Large enough that a 33 MB RAF is a few dozen reads, small enough
#: that nothing loads a whole file into memory.
DEFAULT_CHUNK_SIZE = 1 << 20  # 1 MiB


def content_hash(path: str | Path, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """SHA-256 of a file's bytes, as lowercase hex.

    Raises ``OSError`` if the file cannot be read — a hash of a file that was
    only partly readable would be worse than no hash at all.
    """
    with Path(path).open("rb") as stream:
        return hash_stream(stream, chunk_size=chunk_size)


def hash_stream(stream: BinaryIO, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """SHA-256 of everything left in an open binary stream."""
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be positive, got {chunk_size}")
    digest = hashlib.sha256()
    while chunk := stream.read(chunk_size):
        digest.update(chunk)
    return digest.hexdigest()
