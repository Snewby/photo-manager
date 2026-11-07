"""Shared test setup.

Two standing rules the whole suite obeys (EXECUTION_PLAN.md §4.2):

* No test depends on the memory card being inserted, or on the real library
  existing. Tests that want either are marked and skipped when it's absent.
* No test writes inside the real library. Disk-writing tests use ``tmp_path``.
* No test writes to the real catalog either — not even by opening it, which
  migrates it. Tests that look at the real machine get :func:`machine_settings`,
  whose catalog is a throwaway copy.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from photo_manager.catalog import Catalog  # noqa: E402
import make_fixtures  # noqa: E402
from photo_manager.config import ENV_PREFIX, Settings  # noqa: E402


@pytest.fixture(scope="session")
def machine_settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """This machine's config, with the catalog swapped for a throwaway copy.

    For the tests that look at the real library or the real card. Opening a
    catalog migrates it (catalog.Catalog.open), so a test that only meant to
    read the real one could still rewrite it — and did, the day schema 2
    landed, leaving the running app unable to open its own catalog. A copy
    taken through SQLite's backup API (consistent even mid-write) makes
    "read, and only read" true: whatever these tests open, migrate or cache
    lands in a temporary folder.

    Skips when this machine has no usable config.
    """
    try:
        settings = Settings.load()
    except Exception as exc:  # noqa: BLE001 — no config on this machine is fine
        pytest.skip(f"no usable config.toml: {exc}")

    state = tmp_path_factory.mktemp("machine-state")
    copy = state / "catalog.sqlite3"
    real = settings.database_path
    if real is not None and Path(real).exists():
        source = sqlite3.connect(f"file:{Path(real).as_posix()}?mode=ro", uri=True)
        try:
            target = sqlite3.connect(copy)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    return replace(
        settings, state_dir=state, database_path=copy,
        thumbnail_cache_dir=state / "thumbnails",
    )


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


@pytest.fixture
def pyexiv2():
    """exiv2, the third-party reader D-1's interoperability claim is checked against.

    Skipped when absent — but ``importorskip`` only catches ImportError, and a
    wheel that installs and then cannot load its compiled library (on macOS,
    Homebrew's gettext missing) raises OSError instead, which would turn three
    interoperability checks into failures that say nothing about sidecars. The
    skip reason carries the loader's message, so the check going absent is
    visible in ``-rs`` rather than silent.
    """
    try:
        import pyexiv2 as module
    except ImportError as error:
        pytest.skip(f"pyexiv2 is not installed: {error}")
    except OSError as error:
        pytest.skip(f"pyexiv2 is installed but exiv2 will not load: {error}")
    return module


def pytest_sessionstart(session: pytest.Session) -> None:
    """Make the synthetic fixture photographs if they are not there yet.

    They are generated rather than committed so the repository holds no
    photographs at all; the first run takes about ten seconds longer.
    """
    make_fixtures.ensure()
