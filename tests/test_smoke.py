"""The trivial test WU-0.1 asks for: proof the harness itself works."""

from __future__ import annotations

import photo_manager


def test_package_imports() -> None:
    assert photo_manager.__version__ == "0.1.0"


def test_pytest_runs() -> None:
    assert 2 + 2 == 4
