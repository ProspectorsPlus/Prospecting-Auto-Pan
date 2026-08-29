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
from typing import Any

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


def test_the_hotkey_self_test_is_an_exclusive_bounded_mode() -> None:
    """It listens. It never arms, never presses, and always returns.

    The submit callback is a list rather than the coordinator, so a chord
    recognized during the self-test cannot start a mode or spend an arm token.
    """
    import treasure

    assert "--hotkey-test" in treasure._MODES
    code, output, _elapsed = _run(["--hotkey-test", "0.5", "--soak"], timeout_s=60.0)
    assert code == 2
    assert "Choose one mode" in output

    code, output, elapsed = _run(["--hotkey-test", "1"], timeout_s=60.0)
    # Exit 1 with no chord pressed is the honest answer, not a failure of the
    # mode; what matters is that it is bounded and says what it saw.
    assert code in (0, 1), output
    assert elapsed < 30.0
    assert "Nothing here arms or presses anything" in output
    assert "intents submitted" in output


def test_the_tracking_report_is_an_exclusive_bounded_mode() -> None:
    """Rendered stress, printed as a before/after table. No window, no input."""
    import treasure

    assert "--tracking-report" in treasure._MODES
    code, output, _elapsed = _run(["--tracking-report", "--soak"], timeout_s=60.0)
    assert code == 2
    assert "Choose one mode" in output


def test_the_forward_probe_is_an_exclusive_mode() -> None:
    """The one mode that sends input must never fall through into a window."""
    import treasure

    assert "--forward-probe" in treasure._MODES
    code, output, _elapsed = _run(["--forward-probe", "--soak"], timeout_s=60.0)
    assert code == 2
    assert "Choose one mode" in output


def test_the_native_control_probe_is_an_exclusive_mode() -> None:
    """The second mode that sends input, and it must refuse without focus.

    Passing the flag *is* the authorization, so the bound that matters is the
    other one: it does nothing at all unless Roblox is the positively
    identified frontmost window. A test machine's Roblox is not frontmost, so
    the honest answer here is the refusal.
    """
    import treasure

    assert "--native-control-probe" in treasure._MODES
    code, output, _elapsed = _run(["--native-control-probe", "--soak"], timeout_s=60.0)
    assert code == 2
    assert "Choose one mode" in output


def test_the_native_control_probe_needs_a_positively_frontmost_roblox(
    monkeypatch: Any, capsys: Any
) -> None:
    """It refuses rather than posting an edge into whatever *is* in front.

    Run in-process against a port that says "not frontmost", deliberately.
    The obvious version of this test spawns the real CLI and asserts on its
    refusal - and on a machine where Roblox *is* frontmost that version starts
    posting keys and dragging the right mouse button across a live client from
    inside ``pytest``. A default test run may never emit an OS edge
    (``CLAUDE.md`` rule 2), and "it usually will not" is not that guarantee.
    """
    import treasure
    from prospector_engine.ports import PlatformUnavailable

    class NotFrontmost:
        def focus_state(self) -> bool:
            return False

        def __getattr__(self, name: str) -> Any:
            raise PlatformUnavailable(f"the probe reached {name} after refusing")

    monkeypatch.setattr(
        "prospector_engine.ports.create_platform_port", lambda *a, **k: NotFrontmost()
    )
    code = treasure.main(["--native-control-probe", "--countdown", "0"])

    assert code == 2
    assert "not the positively identified frontmost window" in capsys.readouterr().out


def test_the_forward_probe_worker_is_not_wired_into_the_application() -> None:
    """It exists only in a process launched to run it.

    ``build_application`` is the one composition root, and it deliberately
    registers no worker for ``FORWARD_PROBE``. In the dashboard the intent
    therefore resolves to "no worker" and cannot emit an edge, whatever
    submits it (D-064).
    """
    import inspect

    from prospector_engine.application import build_application
    from prospector_engine.contracts import IntentType

    source = inspect.getsource(build_application)
    workers = source.split("workers: dict[IntentType, WorkerFactory] = {", 1)[1]
    body = workers.split("}", 1)[0]

    assert "FORWARD_PROBE" not in body
    assert IntentType.FORWARD_PROBE.name == "FORWARD_PROBE"


def test_only_the_forward_probe_cli_mode_enables_it() -> None:
    """One caller, and it is the named flag."""
    import inspect

    import treasure
    import treasure_gui
    from prospector_engine import application

    assert "enable_forward_probe" in inspect.getsource(treasure._run_forward_probe)
    assert "enable_forward_probe" not in inspect.getsource(treasure_gui)
    # Defined on Application, and called by nothing inside the engine.
    engine_calls = inspect.getsource(application).count(".enable_forward_probe(")
    assert engine_calls == 0


# ---------------------------------------------------------------------------
# The shadow bench's arrow-readability measurement
# ---------------------------------------------------------------------------


def _readability(samples: list[tuple[float, bool]]) -> dict:
    from treasure import _arrow_readability

    return _arrow_readability(samples)


def test_an_unreadable_arrow_gap_is_measured_between_readable_frames() -> None:
    """The quantity the coast grace is chosen against, taken from real frames."""
    samples = [(0.0, True), (0.1, False), (0.2, False), (0.3, True), (0.4, True)]

    found = _readability(samples)

    assert found["gaps"] == 1
    assert found["gap_ms"]["max"] == pytest.approx(200.0, abs=1.0)
    assert found["gaps_inside_coast_grace"] == 1
    assert found["open_gap_ms"] is None


def test_a_gap_still_open_at_the_end_is_not_counted_as_a_closed_one() -> None:
    """Its length is unknown. Counting it would understate every percentile."""
    samples = [(0.0, True), (0.1, False), (5.0, False)]

    found = _readability(samples)

    assert found["gaps"] == 0
    assert found["open_gap_ms"] == pytest.approx(4900.0, abs=1.0)


def test_a_run_with_no_readable_frame_at_all_says_so_rather_than_reading_clean() -> None:
    """Zero closed gaps looks like a perfect result and is the opposite of one."""
    from treasure import _describe_readability

    found = _readability([(index * 0.1, False) for index in range(50)])

    assert found["readable_fraction"] == 0.0
    line = _describe_readability(found)
    assert "never readable" in line
    assert "Equip a treasure map" in line


def test_gaps_past_the_search_budget_are_counted_separately() -> None:
    from prospector_engine.steering import SteeringLimits

    limits = SteeringLimits()
    long_gap = limits.search_budget_s + 2.0
    samples = [(0.0, True), (0.1, False), (0.1 + long_gap, True)]

    found = _readability(samples)

    assert found["gaps_past_search_budget"] == 1
    assert found["gaps_inside_coast_grace"] == 0
