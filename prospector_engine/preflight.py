"""What this process is actually allowed and able to do, checked before Live.

The failure this exists to stop being possible: *"it says APPLIED and the
character does not move."* Several very different faults produce that sentence,
and until they were separated the only available diagnosis was a guess.

* The process may not hold the OS permissions to post an event at all.
* It may hold them, post the edge, and have the game ignore it.
* ``CGEventPost`` returning without raising is **not** evidence that anything
  moved. It is evidence that the call returned.

So the pipeline is named end to end, and each stage is checked by whoever can
actually answer for it:

``REQUESTED`` - the policy asked for a command.
``OS_EDGE_POSTED`` - the platform port says the edge went to the OS.
``AUTHORITY_APPLIED`` - the input authority holds the leases it reports.
``GAME_MOTION_CONFIRMED`` - perception saw the world move. **This** is success.
``REJECTED`` / ``RELEASED`` - refused or deliberately let go.

Nothing in this module emits input or grants a permission. It reports.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from prospector_engine.bindings import chord_label
from prospector_engine.contracts import CommandStage, IntentType

#: The start chord, spelled for this OS, read from the one binding registry so
#: a rebinding cannot leave a stale key name in a preflight sentence.
_START_CHORD = chord_label(IntentType.START_LIVE, sys.platform)

__all__ = [
    "Capability",
    "CapabilityId",
    "CapabilityKind",
    "CapabilityState",
    "CommandStage",
    "InputPreflight",
    "PreflightInputs",
    "run_preflight",
]


class CapabilityKind(Enum):
    """Whether a failing check is broken, or simply not satisfied yet.

    The distinction is the difference between "something is wrong, go and fix
    it in System Settings" and "you have not clicked into Roblox yet". Showing
    both as red faults trains people to ignore the red.
    """

    #: Something is wrong and no amount of normal use will fix it.
    FAULT = "fault"
    #: A normal precondition the user satisfies in the course of starting.
    PRECONDITION = "precondition"


class CapabilityState(Enum):
    OK = "ok"
    DENIED = "denied"
    #: The check could not be run here - a probe raised, or this OS has no
    #: equivalent. Never reported as OK and never as a hard failure.
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "n/a"


class CapabilityId(Enum):
    """One id per thing that can independently be wrong."""

    EVENT_POST = "event_post"
    INPUT_LISTEN = "input_listen"
    SCREEN_CAPTURE = "screen_capture"
    HOTKEY_LISTENER = "hotkey_listener"
    ROBLOX_FOCUS = "roblox_focus"
    #: Retained id: the *gesture* did not go away, it merged into the chord.
    #: The check is now "will a chord be heard", not "was a button clicked"
    #: (D-062), because there is no separate arming state to report.
    ARM_TOKEN = "start_chord"
    CADENCE = "cadence"
    RELEASE_HEALTH = "release_health"


@dataclass(frozen=True)
class Capability:
    """One checked capability, with the exact thing to do when it is not OK."""

    id: CapabilityId
    label: str
    state: CapabilityState
    detail: str = ""
    remedy: str = ""
    #: The System Settings pane to open, when the fix is a permission. Named
    #: exactly, because "grant permissions" is not an actionable sentence.
    settings_pane: str = ""
    kind: CapabilityKind = CapabilityKind.FAULT

    @property
    def ok(self) -> bool:
        return self.state in (CapabilityState.OK, CapabilityState.NOT_APPLICABLE)

    @property
    def blocks_live(self) -> bool:
        """Whether Live can start right now. Preconditions count here too."""
        return self.state is CapabilityState.DENIED

    @property
    def is_fault(self) -> bool:
        """Whether this is *broken*, as opposed to merely not satisfied yet.

        UNKNOWN is never a fault: a probe that could not run is not evidence
        that a permission is missing, and sending someone to a settings pane
        that was never the problem is worse than saying nothing.
        """
        return self.state is CapabilityState.DENIED and self.kind is CapabilityKind.FAULT


@dataclass(frozen=True)
class PreflightInputs:
    """Everything the preflight needs, gathered by the caller.

    Passed in rather than probed here so this module stays free of platform
    imports and is testable without an OS - and so the caller takes *one*
    coherent reading rather than eight that drift apart between checks.
    """

    os_name: str
    #: The identity that owns the permissions. On macOS these are granted to
    #: the launching application - Terminal, iTerm, or a packaged app - so a
    #: differently packaged copy has to be granted them again.
    launcher: str
    event_post: bool | None
    input_listen: bool | None
    screen_capture: bool | None
    hotkey_listener_running: bool
    roblox_focused: bool | None
    processed_fps: float
    min_processed_fps: float
    release_uncertain: bool
    ledger_empty: bool


def _permission(
    identifier: CapabilityId,
    label: str,
    granted: bool | None,
    *,
    pane: str,
    launcher: str,
    what: str,
) -> Capability:
    if granted is None:
        return Capability(
            identifier,
            label,
            CapabilityState.UNKNOWN,
            detail="this permission could not be read on this system",
            remedy=f"If {what} does not work, check {pane}.",
            settings_pane=pane,
        )
    if granted:
        return Capability(
            identifier, label, CapabilityState.OK, detail=f"granted to {launcher}"
        )
    return Capability(
        identifier,
        label,
        CapabilityState.DENIED,
        detail=f"not granted to {launcher}",
        remedy=(
            f"Open {pane} and enable {launcher}, then restart it. "
            "The permission belongs to whichever application launched this "
            "process, so a differently packaged copy needs granting again."
        ),
        settings_pane=pane,
    )


def run_preflight(inputs: PreflightInputs) -> InputPreflight:
    """Check everything Live depends on, in one coherent pass."""
    mac = inputs.os_name == "darwin"
    accessibility = (
        "System Settings > Privacy & Security > Accessibility"
        if mac
        else "Windows: run as the same user as Roblox"
    )
    listening = (
        "System Settings > Privacy & Security > Input Monitoring"
        if mac
        else "Windows: no separate input-listening permission"
    )
    recording = (
        "System Settings > Privacy & Security > Screen Recording"
        if mac
        else "Windows: no separate screen-recording permission"
    )

    checks: list[Capability] = [
        _permission(
            CapabilityId.EVENT_POST,
            "Send keys and mouse to Roblox",
            inputs.event_post,
            pane=accessibility,
            launcher=inputs.launcher,
            what="the character",
        ),
        _permission(
            CapabilityId.INPUT_LISTEN,
            "Hear the global hotkey chords",
            inputs.input_listen,
            pane=listening,
            launcher=inputs.launcher,
            what="the start chord",
        ),
        _permission(
            CapabilityId.SCREEN_CAPTURE,
            "See the Roblox window",
            inputs.screen_capture,
            pane=recording,
            launcher=inputs.launcher,
            what="the preview",
        ),
    ]

    # A listener that is not running means no chord does anything at all. The
    # remedy depends on *why*: blaming Input Monitoring when it is already
    # granted sends someone to a pane that is not the problem.
    listen_granted = inputs.input_listen is not False
    checks.append(
        Capability(
            CapabilityId.HOTKEY_LISTENER,
            "Hotkey listener",
            CapabilityState.OK if inputs.hotkey_listener_running else CapabilityState.DENIED,
            detail="running" if inputs.hotkey_listener_running else "not running",
            remedy=(
                ""
                if inputs.hotkey_listener_running
                else (
                    "No chord will do anything. Restart the application."
                    if listen_granted
                    else "No chord will do anything, because Input Monitoring is not "
                    f"granted to {inputs.launcher}. Enable it there and restart."
                )
            ),
            settings_pane="" if inputs.hotkey_listener_running or listen_granted else listening,
        )
    )

    focused = inputs.roblox_focused
    checks.append(
        Capability(
            CapabilityId.ROBLOX_FOCUS,
            "Roblox is focused",
            CapabilityState.OK
            if focused is True
            else (CapabilityState.UNKNOWN if focused is None else CapabilityState.DENIED),
            detail={True: "frontmost", False: "another window is frontmost", None: "unknown"}[
                focused
            ],
            # Expected rather than broken: the user is about to click into the
            # game. It is stated so nobody hunts for a fault that is not one.
            remedy="" if focused is True else "Click into Roblox before pressing the chord.",
            kind=CapabilityKind.PRECONDITION,
        )
    )
    checks.append(
        Capability(
            CapabilityId.ARM_TOKEN,
            "Start chord will be heard",
            CapabilityState.OK if inputs.hotkey_listener_running else CapabilityState.DENIED,
            detail=(
                f"{_START_CHORD} both authorizes and starts Live"
                if inputs.hotkey_listener_running
                else "the chord listener is not running, so no chord can authorize Live"
            ),
            remedy=""
            if inputs.hotkey_listener_running
            else "Grant Input Monitoring and restart the application.",
            kind=CapabilityKind.PRECONDITION,
        )
    )

    fast_enough = inputs.processed_fps >= inputs.min_processed_fps
    checks.append(
        Capability(
            CapabilityId.CADENCE,
            "Frame rate is high enough to steer",
            CapabilityState.OK if fast_enough else CapabilityState.DENIED,
            detail=f"{inputs.processed_fps:.0f} processed fps, "
            f"need {inputs.min_processed_fps:.0f}",
            remedy=""
            if fast_enough
            else "Close other heavy applications, or lower Roblox's graphics settings.",
            kind=CapabilityKind.PRECONDITION,
        )
    )

    release_ok = not inputs.release_uncertain and inputs.ledger_empty
    checks.append(
        Capability(
            CapabilityId.RELEASE_HEALTH,
            "No input is stuck down",
            CapabilityState.OK if release_ok else CapabilityState.DENIED,
            detail="nothing held, last release confirmed"
            if release_ok
            else (
                "a previous release could not be confirmed"
                if inputs.release_uncertain
                else "the lease ledger is not empty"
            ),
            # Naming the right control matters: an unconfirmed release latches,
            # and it can outlive the run that caused it - a previous session
            # that shut down without a deadman acknowledgement writes a
            # recovery record, and every later run inherits it. Stop & Release
            # does not clear that; only the recovery handshake does, because
            # clearing it requires a release that was positively acknowledged.
            remedy=""
            if release_ok
            else (
                "Press Recover Release: a previous run could not confirm it let go, "
                "and that is cleared by an acknowledged release, not by Stop."
                if inputs.release_uncertain
                else "Press Stop & Release, then try again."
            ),
        )
    )
    return InputPreflight(tuple(checks))


@dataclass(frozen=True)
class InputPreflight:
    """The whole answer, and the one sentence to show when it is no."""

    capabilities: tuple[Capability, ...]

    @property
    def ok(self) -> bool:
        """Nothing is *broken*. Preconditions may still be unsatisfied."""
        return not self.faults

    @property
    def can_start_live(self) -> bool:
        """Everything - faults and preconditions alike - is satisfied now."""
        return not any(c.blocks_live for c in self.capabilities)

    @property
    def faults(self) -> tuple[Capability, ...]:
        return tuple(c for c in self.capabilities if c.is_fault)

    @property
    def blocking(self) -> tuple[Capability, ...]:
        return tuple(c for c in self.capabilities if c.blocks_live)

    def get(self, identifier: CapabilityId) -> Capability | None:
        return next((c for c in self.capabilities if c.id is identifier), None)

    @property
    def summary(self) -> str:
        """One actionable sentence, not a checklist."""
        # Faults first: a missing permission outranks an unmet precondition,
        # because nothing downstream can work until it is fixed.
        ranked = self.faults or self.blocking
        if not ranked:
            return f"Ready. Press {_START_CHORD} with Roblox focused."
        first = ranked[0]
        extra = f" (+{len(ranked) - 1} more)" if len(ranked) > 1 else ""
        return f"{first.label}: {first.detail}. {first.remedy}{extra}".strip()

    def describe(self) -> tuple[str, ...]:
        return tuple(f"{c.label}: {c.state.value} - {c.detail}" for c in self.capabilities)


def gather(
    *,
    os_name: str,
    launcher: str,
    accessibility_probe: Callable[[], bool] | None,
    listen_probe: Callable[[], bool] | None,
    capture_probe: Callable[[], bool] | None,
    hotkey_running: bool,
    roblox_focused: bool | None,
    processed_fps: float,
    min_processed_fps: float,
    release_uncertain: bool,
    ledger_empty: bool,
) -> InputPreflight:
    """Run the probes defensively and hand the readings to ``run_preflight``.

    A probe that raises reports UNKNOWN rather than DENIED. Claiming a
    permission is missing because a call failed would send someone to a
    settings pane that was never the problem.
    """

    def read(probe: Callable[[], bool] | None) -> bool | None:
        if probe is None:
            return None
        try:
            return bool(probe())
        except Exception:
            return None

    return run_preflight(
        PreflightInputs(
            os_name=os_name,
            launcher=launcher,
            event_post=read(accessibility_probe),
            input_listen=read(listen_probe),
            screen_capture=read(capture_probe),
            hotkey_listener_running=hotkey_running,
            roblox_focused=roblox_focused,
            processed_fps=processed_fps,
            min_processed_fps=min_processed_fps,
            release_uncertain=release_uncertain,
            ledger_empty=ledger_empty,
        )
    )


def blocking_labels(preflight: InputPreflight) -> Sequence[str]:
    return [f"{c.label} - {c.remedy}" for c in preflight.blocking]
