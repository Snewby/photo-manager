"""The Applications app (tools/make_mac_app.py).

The applet is AppleScript around three shell commands, so what is tested here
is the part that went wrong once: how the server is started. Backgrounding
``cd X && cmd`` puts the whole list in a subshell that keeps ``do shell
script``'s output pipe open, and the applet then waits on it for ever —
never opening the browser and never answering Quit.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "make_mac_app.py"


def _tool():
    spec = importlib.util.spec_from_file_location("make_mac_app", TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_the_project_and_log_paths_are_written_into_the_applet(tmp_path: Path) -> None:
    source = _tool().applescript(tmp_path / "photo-manager", log=tmp_path / "pm.log")
    assert f'property projectDir : "{tmp_path / "photo-manager"}"' in source
    assert f'property logPath : "{tmp_path / "pm.log"}"' in source
    assert "%{http_code}" in source


def test_a_path_that_would_break_the_applescript_string_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _tool().applescript(tmp_path / 'a"b')


def test_only_the_server_is_backgrounded_never_a_subshell(tmp_path: Path) -> None:
    source = _tool().applescript(tmp_path)
    [start] = [line for line in source.splitlines() if "tools/serve.py" in line]
    assert '" || exit 1; nohup ' in start
    assert "&&" not in start
    assert "< /dev/null & echo $!" in start


def test_quitting_asks_the_app_what_it_would_cut_short(tmp_path: Path) -> None:
    source = _tool().applescript(tmp_path)
    quit_handler = source[source.index("on quit"):source.index("end quit")]
    assert "api/busy" in quit_handler
    assert '"Keep Running"' in quit_handler
    assert quit_handler.index("api/busy") < quit_handler.index("kill ")


def test_the_busy_answer_reads_as_one_phrase(tmp_path: Path) -> None:
    """The one-liner the quit handler pipes /api/busy through, run for real."""
    source = _tool().applescript(tmp_path)
    program = re.search(r"-c '(.+?)'\"", source).group(1).replace('\\"', '"')
    run = lambda busy: subprocess.run(  # noqa: E731
        [sys.executable, "-c", program], input=json.dumps({"busy": busy}),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert run([]) == ""
    assert run(["a backup", "an import"]) == "a backup and an import"


@pytest.mark.skipif(shutil.which("osacompile") is None, reason="osacompile is macOS only")
def test_the_applet_compiles(tmp_path: Path) -> None:
    script = tmp_path / "applet.applescript"
    script.write_text(_tool().applescript(tmp_path), encoding="utf-8")
    subprocess.run(["osacompile", "-s", "-o", str(tmp_path / "Test.app"), str(script)],
                   check=True, capture_output=True)
    assert (tmp_path / "Test.app" / "Contents" / "MacOS" / "applet").exists()
