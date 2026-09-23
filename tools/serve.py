"""Run the web app.

    .venv\\Scripts\\python.exe tools/serve.py
    .venv\\Scripts\\python.exe tools/serve.py --card tests/fixtures/card

Binds to **127.0.0.1** and nothing else. This is a local-only application
(REQUIREMENTS.md §2, NFR-5) and it serves the contents of a photo library; a
default of ``0.0.0.0`` would put that on every network the machine is attached
to, which is not a thing anyone would have asked for. ``--host`` exists, so it
is still a decision that can be made, but it has to be made explicitly.

``--card`` forces a DCIM path instead of probing the attached volumes, which is
how the app gets exercised with no card in the reader.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import uvicorn  # noqa: E402

from photo_manager.config import ConfigError, Settings  # noqa: E402
from photo_manager.web import create_app  # noqa: E402

OK = 0
ERROR = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.toml.")
    parser.add_argument("--card", help="Force a DCIM path instead of probing for one.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Interface to bind. Local only by default, on purpose.")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    try:
        settings = Settings.load(args.config)
        if args.card:
            settings = replace(settings, card_dcim_root=Path(args.card).expanduser())
        app = create_app(settings)
    except ConfigError as exc:
        # Includes LibraryRootError: a config that has not caught up with a
        # moved library fails here, with a sentence, rather than at the first
        # click (REQUIREMENTS.md §4.2).
        print(f"error: {exc}", file=sys.stderr)
        return ERROR

    print(f"Library : {settings.library_root}")
    print(f"Catalog : {settings.database_path}")
    print(f"Card    : {settings.card_dcim_root or 'probing the attached volumes'}")
    print(f"\n  http://{args.host}:{args.port}/\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
