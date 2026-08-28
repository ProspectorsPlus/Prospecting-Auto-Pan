"""Every offline command line mode is exclusive, bounded, and leaves nothing.

``treasure.py --detector-report`` was once observed alive for over four hours
inside a Tk lifetime. These tests run the offline modes as real subprocesses
with a deadline and read the lifecycle line the entry point prints under
``TREASURE_LIFECYCLE_PROBE=1``: no Tk, no dashboard module, no input
authority (and therefore no deadman child), no surviving child processes, a
report on stdout, and a meaningful exit status.

Nothing here touches a window or emits input; the native modes
(``--capture-probe``, ``--setup-probe``, ``--shadow-bench``, ``--calibrate``)
need a Roblox session and are exercised by the owner, not here. What *is*
checked for them is that they are registered as exclusive modes, because a
mode that falls through the dispatch opens the dashboard instead - which for
``--setup-probe`` would be a window nobody asked for.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "treasure.py"
CORPUS = ROOT / "tests" / "corpus" / "real"


def _run(arguments: list[str], timeout_s: float) -> tuple[int, str, float]:
    env = dict(os.environ, TREASURE_LIFECYCLE_PROBE="1")
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, str(ENTRY), *arguments],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
        cwd=str(ROOT),
        check=False,
    )
    return (
        completed.returncode,
        completed.stdout + completed.stderr,
        time.monotonic() - started,
    )


def _lifecycle(output: str) -> dict[str, str]:
    line = next((row for row in output.splitlines() if row.startswith("lifecycle:")), "")
    assert line, f"no lifecycle line in:\n{output[-800:]}"
    return dict(item.split("=", 1) for item in line.split()[1:])


def _assert_clean(fields: dict[str, str]) -> None:
    assert fields["tkinter"] == "False", "an offline mode imported Tk"
    assert fields["treasure_gui"] == "False", "an offline mode built the dashboard"
    # The deadman helper is a child process; an offline mode never has one.
    assert fields["children"] == "0", "an offline mode left a child process behind"
    assert int(fields["threads"]) <= 2, f"threads survived: {fields['threads']}"


def test_self_test_is_bounded_and_gui_free() -> None:
    code, output, elapsed = _run(["--self-test"], timeout_s=120.0)
    assert code == 0, output[-800:]
    assert "No OS input was emitted" in output
    assert elapsed < 90.0
    _assert_clean(_lifecycle(output))


@pytest.mark.skipif(not (CORPUS / "labels.json").exists(), reason="corpus not checked out")
def test_detector_report_on_the_corpus_exits_with_a_report(tmp_path: Path) -> None:
    target = tmp_path / "report.json"
    code, output, elapsed = _run(
        ["--detector-report", "--corpus", str(CORPUS), "--json", str(target)], timeout_s=300.0
    )
    assert code == 0, output[-800:]
    assert "== split: eval ==" in output
    assert "Regression evidence on real frames" in output
    assert target.exists() and target.stat().st_size > 1000
    assert elapsed < 240.0
    _assert_clean(_lifecycle(output))


def test_soak_is_bounded_and_gui_free() -> None:
    code, output, elapsed = _run(["--soak", "0.05"], timeout_s=180.0)
    assert code == 0, output[-800:]
    assert "frames processed" in output
    assert elapsed < 120.0
    _assert_clean(_lifecycle(output))


def test_modes_are_mutually_exclusive() -> None:
    code, output, _elapsed = _run(["--soak", "0.05", "--detector-report"], timeout_s=60.0)
    assert code == 2
    assert "Choose one mode" in output


def test_setup_probe_is_an_exclusive_mode_not_a_dashboard() -> None:
    """An unregistered flag falls through to ``gui_main()`` and opens Tk."""
    import treasure

    assert "--setup-probe" in treasure._MODES
    code, output, _elapsed = _run(["--setup-probe", "--capture-probe"], timeout_s=60.0)
    assert code == 2
    assert "Choose one mode" in output


def test_a_missing_replay_directory_is_a_usage_error_not_a_hang() -> None:
    code, output, elapsed = _run(["--replay"], timeout_s=60.0)
    assert code == 2
    assert "needs a recorded session directory" in output
    assert elapsed < 30.0
