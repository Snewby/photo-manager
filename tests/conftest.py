"""Shared test setup.

Two standing rules the whole suite obeys (EXECUTION_PLAN.md §4.2):

* No test depends on the memory card being inserted, or on the real library
  existing. Tests that want either are marked and skipped when it's absent.
* No test writes inside the real library. Disk-writing tests use ``tmp_path``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from photo_manager.catalog import Catalog  # noqa: E402
import make_fixtures  # noqa: E402
from photo_manager.config import ENV_PREFIX, Settings  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip PHOTO_MANAGER_* from the environment for every test.

    Without this, whatever the owner has configured locally leaks into config tests
    and they pass or fail depending on the machine.
    """
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def library_root(tmp_path: Path) -> Path:
    """A throwaway library root. Never the real one."""
    root = tmp_path / "library"
    root.mkdir()
    return root


@pytest.fixture
def settings(library_root: Path, tmp_path: Path) -> Settings:
    """Settings pointed entirely at temporary directories."""
    return Settings(
        library_root=library_root,
        card_dcim_root=tmp_path / "card" / "DCIM",
        state_dir=tmp_path / "state",
    )


@pytest.fixture
def catalog(settings: Settings) -> Iterator[Catalog]:
    """An open, migrated catalog in a temporary state directory."""
    with Catalog.open(settings) as open_catalog:
        yield open_catalog


def pytest_sessionstart(session: pytest.Session) -> None:
    """Make the synthetic fixture photographs if they are not there yet.

    They are generated rather than committed so the repository holds no
    photographs at all; the first run takes about ten seconds longer.
    """
    make_fixtures.ensure()
