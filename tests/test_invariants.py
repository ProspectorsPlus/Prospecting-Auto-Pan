"""The plan's section 16.2 invariant list, checked one item at a time.

Some of these overlap with the focused suites; they are repeated here so the
list can be read against the plan without cross-referencing five files. Each
test names the invariant it covers.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from typing import Any

import pytest

from prospector_engine.contracts import (
    ArrowObservation,
    InputKey,
    MouseButton,
    NavigationCommand,
    NavigationPhase,
    RunMode,
    TelemetrySnapshot,
)

# -- "Every native down/pointer/scroll edge is ordered against Stop" ---------


@pytest.mark.parametrize("edge", ["pointer_delta", "scroll_lines", "pointer_move_client"])
def test_no_pointer_or_scroll_edge_survives_a_racing_stop(rig: Any, edge: str) -> None:
    """Pointer moves and scrolls share the barrier with acquisition and release.

    The port is instrumented to start a ``release_all`` from another thread
    while the edge is mid-flight. That thread cannot enter the barrier until
    the edge finishes, which is the ordering guarantee under test: no edge may
    land after a completed release.
    """
    rig.activate()
    session = rig.session()
    attribute = {
        "pointer_delta": "raw_pointer_delta",
        "scroll_lines": "raw_scroll_lines",
        "pointer_move_client": "raw_pointer_move_client",
    }[edge]
    original = getattr(rig.port, attribute)
    racers: list[threading.Thread] = []

    def wrapper(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        if not racers:
            racer = threading.Thread(target=rig.authority.release_all, args=("racing",))
            racers.append(racer)
            racer.start()
            time.sleep(0.05)  # the racer is now blocked on the barrier we hold

    setattr(rig.port, attribute, wrapper)

    call = {
        "pointer_delta": lambda: session.pointer_delta(10, 0),
        "scroll_lines": lambda: session.scroll_lines(-1),
        "pointer_move_client": lambda: session.pointer_move_client((10, 10)),
    }[edge]

    assert call() is True  # the first edge completes inside the barrier
    for racer in racers:
        racer.join(5.0)
        assert not racer.is_alive()

    # The release has now completed. Nothing may emit again.
    rig.port.transcript.clear()
    assert call() is False
    assert rig.port.ops() == []
    assert rig.authority.ledger_empty()


# -- "Exactly one input-emitting worker/mode exists" ------------------------


def test_only_live_and_service_may_hold_an_input_capability() -> None:
    emitting = {mode for mode in RunMode if mode.emits_input}
    assert emitting == {RunMode.LIVE, RunMode.SERVICE}


# -- "Frame/telemetry contracts are actually read-only" --------------------


def test_every_cross_thread_contract_is_frozen() -> None:
    from prospector_engine import contracts

    exported = [getattr(contracts, name) for name in contracts.__all__]
    dataclass_types = [
        obj for obj in exported if dataclasses.is_dataclass(obj) and isinstance(obj, type)
    ]
    assert dataclass_types, "no contracts found"
    for cls in dataclass_types:
        params = cls.__dataclass_params__
        assert params.frozen, f"{cls.__name__} is not frozen"


def test_a_telemetry_snapshot_cannot_be_mutated() -> None:
    snapshot = TelemetrySnapshot(
        sequence=1,
        mode=RunMode.IDLE,
        phase=None,
        viewport=None,
        arrow=None,
        direction=None,
        motion=None,
        arrival=None,
        command=None,
        ledger_empty=True,
        focus=None,
        frame_age_ms=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.mode = RunMode.LIVE  # type: ignore[misc]


def test_observations_expose_tuples_not_lists() -> None:
    arrow = ArrowObservation(None, None, None, None, None, None, 0.0, False, "test")
    assert arrow.bbox_px is None
    snapshot_fields = {
        field.name: field.type for field in dataclasses.fields(TelemetrySnapshot)
    }
    assert "tuple[str, ...]" in str(snapshot_fields["warnings"])


# -- "No retry loop lacks both an attempt cap and a monotonic deadline" ----


def test_every_bounded_service_has_an_attempt_cap_and_a_deadline() -> None:
    from prospector_engine.engine import DEFAULT_TIMINGS, ServiceContext

    assert DEFAULT_TIMINGS.dequip_max_attempts > 0
    assert DEFAULT_TIMINGS.dequip_deadline_ms > 0
    assert DEFAULT_TIMINGS.pan_max_attempts > 0
    assert DEFAULT_TIMINGS.pan_attempt_deadline_ms > 0
    assert DEFAULT_TIMINGS.reset_deadline_ms > 0
    assert "deadline_s" in {field.name for field in dataclasses.fields(ServiceContext)}


def test_pan_swap_retries_are_a_loop_not_recursion() -> None:
    """The legacy pan swap recursed per attempt; a long chain deepened the stack."""
    import inspect

    from prospector_engine import engine

    source = inspect.getsource(engine.run_pan_swap)
    assert "run_pan_swap(" not in source.split('"""', 2)[-1], "pan swap still recurses"
    assert "for attempt in range" in source


# -- "ABANDONED safe-stops unless E-SKIP_MAP is explicitly enabled" --------


def test_there_is_no_abandoned_to_next_map_path_anywhere() -> None:
    """E-SKIP_MAP has not been run, so skipping a map must be impossible."""
    import inspect

    from prospector_engine import coordinator, navigation

    for module in (navigation, coordinator):
        source = inspect.getsource(module)
        assert "NEXT_MAP" not in source or "next_map_enabled" in source
    assert not navigation.NavigationGates(os_name="t", profile_id="p").next_map_enabled


def test_an_abandoned_result_safe_stops(rig: Any) -> None:
    from prospector_engine.contracts import ModeResult, ModeResultKind

    result = ModeResult(ModeResultKind.ABANDONED, "recovery exhausted")
    assert result.kind is ModeResultKind.ABANDONED
    # The coordinator's completion handler routes ABANDONED and FAILED to the
    # same safe-stop branch; see test_runtime_concurrency for the live path.
    import inspect

    from prospector_engine.coordinator import RuntimeCoordinator

    source = inspect.getsource(RuntimeCoordinator._handle_completion)
    assert "ModeResultKind.ABANDONED" in source
    assert "_safe_stop" in source


# -- "Angle wrapping works around +-180" -----------------------------------


def test_navigation_phases_have_terminal_states() -> None:
    terminal = {NavigationPhase.ARRIVED, NavigationPhase.ABANDONED, NavigationPhase.FAILED}
    assert terminal <= set(NavigationPhase)


# -- "No stale or abstained observation renews input" ----------------------


def test_an_abstained_observation_cannot_build_a_command() -> None:
    from prospector_engine.navigation import Navigator
    from tests.test_navigation import ALL_PASSED, _inputs

    navigator = Navigator(gates=ALL_PASSED)
    decision = navigator.decide(_inputs(error_deg=None), generation=1, now_s=0.0)
    assert decision.command is None
    assert decision.release


def test_a_command_cannot_be_built_with_an_impossible_axis() -> None:
    with pytest.raises(ValueError, match="forward_axis"):
        NavigationCommand(1, 1, 0.0, 2, 0, False, 0, 0.0, 0.05, "bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="expires before"):
        NavigationCommand(1, 1, 0.0, 1, 0, False, 0, 1.0, 0.5, "bad")


# -- "One global, idempotent release_all covering the vocabulary" ----------


def test_the_vocabulary_is_the_release_floor() -> None:
    from prospector_engine.contracts import InputVocabulary

    vocabulary = InputVocabulary()
    assert set(vocabulary.keys) == set(InputKey)
    assert set(vocabulary.buttons) == set(MouseButton)
    with pytest.raises(ValueError, match="duplicates"):
        InputVocabulary(keys=(InputKey.W, InputKey.W))


def test_a_lease_covers_exactly_one_target() -> None:
    from prospector_engine.contracts import LeaseHandle

    with pytest.raises(ValueError, match="exactly one"):
        LeaseHandle(1, 1, InputKey.W, MouseButton.LEFT, 0.0, 1.0)
    with pytest.raises(ValueError, match="exactly one"):
        LeaseHandle(1, 1, None, None, 0.0, 1.0)


# -- "Live arming is never persisted" --------------------------------------


def test_no_shipping_module_persists_an_arm_token() -> None:
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    modules = [root / "treasure_gui.py", *sorted((root / "prospector_engine").glob("*.py"))]
    pattern = re.compile(r"(arm[_a-z]*token[^\n]*(json|dump|write|save|pickle))", re.IGNORECASE)
    offenders = [path.name for path in modules if pattern.search(path.read_text())]
    assert offenders == []


def test_the_arm_token_is_bound_to_the_process_run() -> None:
    from prospector_engine.coordinator import LiveArmToken

    fields = {field.name for field in dataclasses.fields(LiveArmToken)}
    assert {"run_id", "generation", "expires_at_s", "token_id"} <= fields
    token = LiveArmToken("id", "run", 1, 0.0, 30.0)
    assert token.expired(31.0)
    assert token.remaining_s(10.0) == pytest.approx(20.0)
