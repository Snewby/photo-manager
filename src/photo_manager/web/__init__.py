"""The local web app (D-3).

:func:`photo_manager.web.app.create_app` builds the FastAPI application;
``tools/serve.py`` runs it. Everything in here is UI plumbing over the modules
that do the real work — nothing in this package reads EXIF, hashes a file or
decides what a duplicate is.
"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
