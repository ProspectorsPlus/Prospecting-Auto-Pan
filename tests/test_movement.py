"""The movement actuator: can it press a key, and does it ever press twice?

The path this replaces failed a test nobody had written: *does an OS edge ever
get posted at all*. In every runtime trace from the owner's machine the answer
was no, and every test in the suite passed anyway, because they all asserted on
the layer above the one that was broken.

So the first test here is the stupid one, and it is the most important: hand
the actuator a fake port and check that a down edge reaches it. The last test
is the same question against the **real** macOS port and the real window
server, using an inert keycode, and is marked ``native`` because it genuinely
emits OS input.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from prospector_engine.contracts import InputKey
from prospector_engine.movement import (
    IDLE,
    DesiredMovement,
    MovementActuator,
    MovementBlock,
    MovementLimits,
    desired_from_command,
)


class RecordingPort:
    """The smallest thing that can be pressed. Records edges, nothing else."""

    def __init__(self) -> None:
        self.edges: list[tuple[str, str]] = []
        self.deltas: list[int] = []
        self.fail_on: set[str] = set()

    def key_code(self, key: InputKey) -> int:
        return {"w": 13, "a": 0, "s": 1, "d": 2, "left": 123, "right": 124, "space": 49}.get(
            key.value, 99
        )

    def _name(self, code: int) -> str:
        return {13: "w", 0: "a", 1: "s", 2: "d", 123: "left", 124: "right", 49: "space"}.get(
            code, "?"
        )

    def raw_key_down(self, code: int) -> None:
        if "down" in self.fail_on:
            raise OSError("injected down failure")
        self.edges.append(("down", self._name(code)))

    def raw_key_up(self, code: int) -> None:
        if "up" in self.fail_on:
            raise OSError("injected up failure")
        self.edges.append(("up", self._name(code)))

    def raw_pointer_delta(self, dx: int, dy: int, held: Any = None) -> None:
        self.deltas.append(dx)

    # -- helpers the tests read -------------------------------------------
    def downs(self, name: str) -> int:
        return sum(1 for kind, key in self.edges if kind == "down" and key == name)

    def ups(self, name: str) -> int:
        return sum(1 for kind, key in self.edges if kind == "up" and key == name)


class Deadman:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy
        self.registered: list[str] = []
        self.released: list[str] = []

    def register(self, lease_id: int, generation: int, target: str, ms: int) -> bool:
        self.registered.append(target)
        return True

    def release_all(self, reason: str) -> bool:
        self.released.append(reason)
        return True


def _actuator(
    port: RecordingPort | None = None,
    *,
    focus: Any = True,
    deadman: Deadman | None = None,
    limits: MovementLimits | None = None,
    lines: list[tuple[str, str]] | None = None,
) -> tuple[MovementActuator, RecordingPort]:
    used = port or RecordingPort()
    actuator = MovementActuator(
        used,
        deadman=deadman if deadman is not None else Deadman(),
        focus_probe=lambda: focus,
        narrate=(lambda verdict, text: lines.append((verdict, text)))
        if lines is not None
        else None,
        limits=limits,
    )
    actuator.start_watchdog()
    actuator.arm("test")
    return actuator, used


# ---------------------------------------------------------------------------
# The test that was missing
# ---------------------------------------------------------------------------


def test_asking_for_forward_actually_posts_a_key_down() -> None:
    """The stupid test, and the one whose absence cost weeks.

    Everything above this layer was tested. This layer - "does a down edge
    reach the platform at all" - was not, and it was the broken one.
    """
    actuator, port = _actuator()
    try:
        outcome = actuator.apply(DesiredMovement(forward=1, reason="walking"))
    finally:
        actuator.stop_watchdog()

    assert ("down", "w") in port.edges, f"nothing was pressed: {port.edges}"
    assert outcome.pressed == (InputKey.W,)
    assert outcome.held == frozenset({InputKey.W})
    assert not outcome.block.blocking


def test_walking_and_turning_press_both_keys() -> None:
    actuator, port = _actuator()
    try:
        outcome = actuator.apply(DesiredMovement(forward=1, turn=1, reason="follow"))
    finally:
        actuator.stop_watchdog()

    assert port.downs("w") == 1
    assert port.downs("right") == 1
    assert outcome.held == frozenset({InputKey.W, InputKey.RIGHT})


def test_a_yaw_request_posts_a_signed_delta_and_keeps_forward_down() -> None:
    actuator, port = _actuator()
    try:
        actuator.apply(DesiredMovement(forward=1, yaw_px=14))
        actuator.apply(DesiredMovement(forward=1, yaw_px=-14))
    finally:
        actuator.stop_watchdog()

    assert port.deltas == [14, -14]
    assert port.downs("w") == 1, "corrective yaw re-pressed forward"
    assert port.ups("w") == 0, "corrective yaw dropped the forward hold"


# ---------------------------------------------------------------------------
# One press, one release - by construction
# ---------------------------------------------------------------------------


def test_a_hundred_identical_ticks_are_one_press() -> None:
    """A rattle is not something this interface can express."""
    actuator, port = _actuator()
    try:
        for _ in range(100):
            actuator.apply(DesiredMovement(forward=1))
    finally:
        actuator.stop_watchdog()

    assert port.downs("w") == 1, f"{port.downs('w')} presses for one continuous hold"
    assert port.ups("w") == 0


def test_letting_go_is_exactly_one_release() -> None:
    actuator, port = _actuator()
    try:
        for _ in range(10):
            actuator.apply(DesiredMovement(forward=1))
        actuator.apply(IDLE)
    finally:
        actuator.stop_watchdog()

    assert port.downs("w") == 1
    assert port.ups("w") == 1
    assert actuator.empty


def test_a_deliberate_change_of_direction_is_a_second_press() -> None:
    actuator, port = _actuator()
    try:
        actuator.apply(DesiredMovement(forward=1))
        actuator.apply(IDLE)
        actuator.apply(DesiredMovement(forward=1))
    finally:
        actuator.stop_watchdog()

    assert port.downs("w") == 2
    assert port.ups("w") == 1


def test_opposite_keys_are_never_held_at_once() -> None:
    actuator, port = _actuator()
    try:
        actuator.apply(DesiredMovement(turn=1))
        assert actuator.held == frozenset({InputKey.RIGHT})
        actuator.apply(DesiredMovement(turn=-1))
    finally:
        actuator.stop_watchdog()

    assert actuator.held == frozenset({InputKey.LEFT})
    assert port.ups("right") == 1
    # The release precedes the press, in the edge stream itself.
    assert port.edges.index(("up", "right")) < port.edges.index(("down", "left"))


def test_the_hold_duration_is_the_press_not_the_tick() -> None:
    actuator, _port = _actuator()
    try:
        actuator.apply(DesiredMovement(forward=1))
        time.sleep(0.15)
        for _ in range(5):
            actuator.apply(DesiredMovement(forward=1))
        held = actuator.forward_held_s(__import__("time").monotonic())
    finally:
        actuator.stop_watchdog()

    assert held >= 0.15, f"the hold clock restarted: {held}"


# ---------------------------------------------------------------------------
# Everything releases
# ---------------------------------------------------------------------------


def test_another_window_in_front_releases_everything() -> None:
    focus: dict[str, bool | None] = {"ok": True}
    port = RecordingPort()
    actuator = MovementActuator(port, deadman=Deadman(), focus_probe=lambda: focus["ok"])
    actuator.start_watchdog()
    actuator.arm()
    try:
        actuator.apply(DesiredMovement(forward=1))
        assert port.downs("w") == 1
        focus["ok"] = False
        time.sleep(MovementActuator.FOCUS_CACHE_S * 2)
        actuator.poll()
    finally:
        actuator.stop_watchdog()

    assert actuator.empty
    assert port.ups("w") >= 1


def test_an_unknown_focus_reading_does_not_refuse_a_press() -> None:
    """The most expensive mistake in the path this replaces, pinned.

    macOS's frontmost probe is a window-list scan that returns ``None`` on any
    error or ambiguity. The old authority wrote ``if focus is not True:
    refuse``, so every ambiguous scan became a refused keypress. Lite has
    driven a character on this same machine for months with the opposite rule.
    """
    focus: dict[str, bool | None] = {"ok": None}
    port = RecordingPort()
    actuator = MovementActuator(port, deadman=Deadman(), focus_probe=lambda: focus["ok"])
    actuator.start_watchdog()
    actuator.arm()
    try:
        outcome = actuator.apply(DesiredMovement(forward=1))
    finally:
        actuator.stop_watchdog()
        actuator.release_all("test")

    assert not outcome.block.blocking, f"an unknown focus refused a press: {outcome.block}"
    assert port.downs("w") == 1


def test_a_probe_that_raises_is_treated_as_unknown_not_as_lost() -> None:
    def angry() -> bool:
        raise OSError("CGWindowList failed")

    port = RecordingPort()
    actuator = MovementActuator(port, deadman=Deadman(), focus_probe=angry)
    actuator.start_watchdog()
    actuator.arm()
    try:
        outcome = actuator.apply(DesiredMovement(forward=1))
    finally:
        actuator.stop_watchdog()
        actuator.release_all("test")

    assert not outcome.block.blocking
    assert port.downs("w") == 1


def test_an_unhealthy_helper_releases_and_refuses_to_press() -> None:
    deadman = Deadman()
    actuator, port = _actuator(deadman=deadman)
    try:
        actuator.apply(DesiredMovement(forward=1))
        deadman.healthy = False
        assert actuator.poll() is MovementBlock.DEADMAN_UNHEALTHY
        blocked = actuator.apply(DesiredMovement(forward=1))
    finally:
        actuator.stop_watchdog()

    assert actuator.empty
    assert blocked.block is MovementBlock.DEADMAN_UNHEALTHY
    assert port.downs("w") == 1, "it pressed again while the helper was unhealthy"


def test_a_worker_that_stops_calling_loses_the_keys() -> None:
    """The 'stalled inside a native grab, still holding W' case."""
    # The thread is parked so this test drives the same check by hand and gets
    # a deterministic verdict instead of racing the watchdog it is testing.
    actuator, port = _actuator(
        limits=MovementLimits(heartbeat_timeout_ms=60, watchdog_interval_ms=60_000)
    )
    try:
        actuator.apply(DesiredMovement(forward=1))
        time.sleep(0.12)
        assert actuator.poll() is MovementBlock.HEARTBEAT_LOST
    finally:
        actuator.stop_watchdog()

    assert actuator.empty
    assert port.ups("w") >= 1


def test_a_hold_has_a_ceiling_even_while_the_worker_is_healthy() -> None:
    actuator, port = _actuator(
        limits=MovementLimits(
            max_hold_ms=80, heartbeat_timeout_ms=5000, watchdog_interval_ms=60_000
        )
    )
    try:
        actuator.apply(DesiredMovement(forward=1))
        time.sleep(0.14)
        assert actuator.poll() is MovementBlock.HOLD_CEILING
    finally:
        actuator.stop_watchdog()

    assert actuator.empty
    assert port.ups("w") >= 1


def test_nothing_presses_without_a_running_watchdog() -> None:
    """Lite raises LeaseRefused here, and for the same reason."""
    port = RecordingPort()
    actuator = MovementActuator(port, deadman=Deadman(), focus_probe=lambda: True)
    actuator.arm()  # deliberately no start_watchdog()

    outcome = actuator.apply(DesiredMovement(forward=1))

    assert outcome.block is MovementBlock.NO_WATCHDOG
    assert port.downs("w") == 0


def test_a_stopped_actuator_presses_nothing() -> None:
    actuator, port = _actuator()
    try:
        actuator.disarm("stop")
        outcome = actuator.apply(DesiredMovement(forward=1))
    finally:
        actuator.stop_watchdog()

    assert outcome.block is MovementBlock.STOPPED
    assert port.downs("w") == 0


def test_release_all_lifts_the_whole_vocabulary_not_just_what_it_thinks() -> None:
    """The ledger can be wrong in exactly one direction, and this covers it."""
    actuator, port = _actuator()
    try:
        actuator.apply(DesiredMovement(forward=1))
        port.edges.clear()
        actuator.release_all("stop")
    finally:
        actuator.stop_watchdog()

    lifted = {key for kind, key in port.edges if kind == "up"}
    assert {"w", "a", "s", "d", "left", "right", "space"} <= lifted


def test_a_failed_release_latches_and_blocks_every_new_press() -> None:
    actuator, port = _actuator()
    try:
        actuator.apply(DesiredMovement(forward=1))
        port.fail_on.add("up")
        actuator.release_all("stop")
        assert actuator.release_uncertain
        port.fail_on.clear()
        blocked = actuator.apply(DesiredMovement(forward=1))
    finally:
        actuator.stop_watchdog()

    assert blocked.block is MovementBlock.RELEASE_UNCERTAIN


def test_a_platform_that_raises_on_press_is_reported_not_believed() -> None:
    port = RecordingPort()
    port.fail_on.add("down")
    lines: list[tuple[str, str]] = []
    actuator, _ = _actuator(port, lines=lines)
    try:
        outcome = actuator.apply(DesiredMovement(forward=1))
    finally:
        actuator.stop_watchdog()

    assert outcome.pressed == ()
    assert actuator.empty, "it believes it holds a key the platform refused"
    assert any(verdict == "fail" for verdict, _ in lines)


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def test_a_navigation_command_becomes_the_keys_it_means() -> None:
    from prospector_engine.contracts import NavigationCommand

    def _command(**fields: Any) -> NavigationCommand:
        base: dict[str, Any] = {
            "generation": 1,
            "source_frame_sequence": 1,
            "source_captured_at_s": 0.0,
            "forward_axis": 0,
            "lateral_axis": 0,
            "jump": False,
            "yaw_delta_px": 0,
            "turn_axis": 0,
            "issued_at_s": 0.0,
            "valid_until_s": 1.0,
            "reason": "follow",
        }
        base.update(fields)
        return NavigationCommand(**base)  # type: ignore[arg-type]

    # A command may use the turn keys or mouse yaw, never both - the contract
    # refuses it, because two actuators asking for one rotation would double it.
    keys = desired_from_command(_command(forward_axis=1, turn_axis=1))
    assert keys.keys == frozenset({InputKey.W, InputKey.RIGHT})
    assert keys.yaw_px == 0

    mouse = desired_from_command(_command(forward_axis=1, yaw_delta_px=9))
    assert mouse.keys == frozenset({InputKey.W})
    assert mouse.yaw_px == 9

    assert desired_from_command(None) is IDLE


# ---------------------------------------------------------------------------
# The real window server
# ---------------------------------------------------------------------------


@pytest.mark.native
def test_the_real_platform_port_actually_presses_a_key() -> None:
    """End to end against macOS, with a keycode no application binds.

    F13 produces no text, is unbound in Roblox, and is unbound in every app
    this is likely to run under - so this is safe to run with anything
    frontmost. What it proves is the one thing no other test could: that an
    edge asked for at this interface arrives at the window server.
    """
    import sys

    if sys.platform != "darwin":
        pytest.skip("macOS only")
    import Quartz

    from prospector_engine.platform_mac import MacPlatformPort

    F13 = 105

    class InertPort(MacPlatformPort):  # type: ignore[misc]
        """The real port, with the vocabulary pointed at an inert keycode."""

        def key_code(self, key: InputKey) -> int:
            return F13

    def down() -> bool:
        return bool(
            Quartz.CGEventSourceKeyState(Quartz.kCGEventSourceStateCombinedSessionState, F13)
        )

    actuator = MovementActuator(InertPort(), deadman=None, focus_probe=lambda: True)
    actuator.start_watchdog()
    actuator.arm()
    try:
        assert not down(), "F13 was already down before the test"
        actuator.apply(DesiredMovement(forward=1))
        time.sleep(0.08)
        assert down(), "the actuator did not reach the window server"
        actuator.release_all("test")
        time.sleep(0.08)
        assert not down(), "the key was never released"
    finally:
        actuator.release_all("test-cleanup")
        actuator.stop_watchdog()
