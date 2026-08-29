"""Immutable contracts shared across every Treasure Navigator thread.

Everything that crosses a thread boundary lives here and is frozen. A frozen
dataclass holding a mutable NumPy array is not actually immutable, so arrays
are passed through :func:`freeze_array` and checked in ``__post_init__``
(plan section 5).

Conventions enforced by review and by ``mypy --strict``:

* Units are part of the name: ``_px``, ``_ms``, ``_s``, ``_deg``, ``_norm``.
* Internal time is ``time.monotonic()`` seconds. Milliseconds appear only at
  boundaries (configuration, logs, UI text).
* Nothing here imports an OS module, OpenCV, or Tk, so both platform test
  suites can import it on either OS.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from prospector_engine.geometry import Affine2D, ViewportGeometry
from prospector_engine.trace import PerceptionTiming

__all__ = [
    "ArrivalObservation",
    "ArrowCandidateRecord",
    "ArrowObservation",
    "CadenceMode",
    "CadenceReport",
    "Cancellation",
    "CancellationToken",
    "CaptureMetrics",
    "CapturedFrame",
    "CommandKind",
    "ControlState",
    "CueReading",
    "DiagnosticObservation",
    "DigEvidence",
    "DigHandoffResult",
    "DigOutcome",
    "EvidenceStatus",
    "EvidenceToken",
    "FitPhase",
    "FocusState",
    "FrameEnvelope",
    "GovernorState",
    "InputKey",
    "InputVocabulary",
    "IntentType",
    "LeaseHandle",
    "MotionObservation",
    "MouseButton",
    "NavigationApplyResult",
    "NavigationCommand",
    "NavigationPhase",
    "NextMapOutcome",
    "PacketKind",
    "PanSwapOutcome",
    "PanSwapResult",
    "PerformanceTier",
    "PinResult",
    "Provenance",
    "PursuitTelemetry",
    "RateSummary",
    "RawFrame",
    "ResetOutcome",
    "ResetResult",
    "RunMode",
    "RuntimeIntent",
    "RuntimeKey",
    "SafetyFault",
    "SafetyFaultKind",
    "ServiceKind",
    "SetupFailure",
    "SetupFailureKind",
    "SetupProgress",
    "SetupStage",
    "TelemetrySnapshot",
    "ViewportFit",
    "WorkerCompletion",
    "freeze_array",
    "monotonic_s",
]


def monotonic_s() -> float:
    """The one clock every duration in this application is measured against."""
    return time.monotonic()


def freeze_array(array: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """Return ``array`` marked non-writeable so a frozen dataclass is honest.

    If the buffer is not ours to freeze (a view onto someone else's writeable
    memory), a copy is frozen instead. Callers must use the returned array.
    """
    try:
        array.flags.writeable = False
        return array
    except ValueError:
        copied = array.copy()
        copied.flags.writeable = False
        return copied


# ---------------------------------------------------------------------------
# Evidence vocabulary
# ---------------------------------------------------------------------------


class EvidenceStatus(Enum):
    """How much is actually known about a value, a feature, or a gate.

    Used verbatim in configuration provenance, telemetry, and UI text so a
    provisional starting value can never be read as a measurement (plan 2.2).
    """

    OBSERVED_FACT = "observed_fact"
    PROVISIONAL = "provisional"
    VALIDATED = "validated"
    GUARDED_BETA = "guarded_beta"
    PENDING = "pending"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Provenance:
    """Why a tuned number has the value it has.

    Every configuration constant that could otherwise read as a magic number
    carries one of these (plan 17).
    """

    status: EvidenceStatus
    source: str
    note: str = ""

    def __str__(self) -> str:
        suffix = f" - {self.note}" if self.note else ""
        return f"{self.status.value} ({self.source}){suffix}"


# ---------------------------------------------------------------------------
# Platform-level value types
# ---------------------------------------------------------------------------

FocusState = bool | None
"""Whether the Roblox client is frontmost.

``True`` positively focused, ``False`` positively not focused, ``None``
unknown. The distinction matters: ``None`` forbids new presses and renewals
but is not by itself proof of anything, while a release never consults focus
at all (plan 4.3).
"""


class MouseButton(Enum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


class InputKey(Enum):
    """Every key Treasure is allowed to inject.

    This enum *is* the release floor: :class:`InputVocabulary` is built from
    it, so a key that can be pressed is always a key ``release_all()`` will
    try to lift (plan 4.4, bug B5).
    """

    W = "w"
    A = "a"
    S = "s"
    D = "d"
    SPACE = "space"
    SHIFT = "shift"
    ESCAPE = "escape"
    DIGIT_1 = "1"
    DIGIT_2 = "2"
    #: Camera yaw. These are a *turn* actuator, not a lateral one: they rotate
    #: the camera, where A/D strafe the character. Keeping them as separate
    #: vocabulary entries is what lets the release floor lift a turn key even
    #: when the strafe axis is idle (D-038).
    LEFT = "left"
    RIGHT = "right"

    @property
    def is_turn(self) -> bool:
        return self in (InputKey.LEFT, InputKey.RIGHT)


@dataclass(frozen=True)
class InputVocabulary:
    """The immutable set of edges this application can ever emit."""

    keys: tuple[InputKey, ...] = tuple(InputKey)
    buttons: tuple[MouseButton, ...] = tuple(MouseButton)

    def __post_init__(self) -> None:
        if len(set(self.keys)) != len(self.keys):
            raise ValueError("InputVocabulary.keys contains duplicates")
        if len(set(self.buttons)) != len(self.buttons):
            raise ValueError("InputVocabulary.buttons contains duplicates")


@dataclass(frozen=True)
class PinResult:
    """Outcome of a pin attempt, always carrying what was actually achieved.

    A refusal is as informative as a success: when the OS clamps the request,
    ``geometry`` describes the window we really have and ``requested`` says what
    was asked for, so the UI can say both without the caller retrying forever.
    """

    ok: bool
    message: str
    geometry: ViewportGeometry | None = None
    requested_client_logical: tuple[float, float] | None = None
    #: The request was accepted and the OS or the game answered with a
    #: different size. Not a refusal: ``ok`` stays True and the viewport
    #: guard classifies the settled read-back.
    clamped: bool = False
    #: The mechanism that produced the result, for the fit message.
    mechanism: str = ""


class FitPhase(Enum):
    """Where a *Fit & Lock Viewport* attempt has got to.

    Fitting is asynchronous and bounded, because a resize is a request to
    another process that may be answered late, partially, or not at all. Three
    of these are terminal, and ``ACHIEVED_CLAMPED`` is a truthful success: the
    OS or the game refused the exact size, we adopted what we actually got, and
    nothing downstream is told it is canonical (plan 4.1).
    """

    IDLE = "idle"
    REQUESTED = "requested"
    SETTLING = "settling"
    CANONICAL_VERIFIED = "canonical_verified"
    ACHIEVED_CLAMPED = "achieved_clamped"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in (
            FitPhase.CANONICAL_VERIFIED,
            FitPhase.ACHIEVED_CLAMPED,
            FitPhase.FAILED,
            FitPhase.IDLE,
        )


@dataclass(frozen=True)
class ViewportFit:
    """One bounded fit attempt, with what was asked for and what was achieved.

    Both sizes are recorded in logical units *and* backing pixels because a
    Retina client that is 1280x720 points is 2560x1440 pixels, and reporting
    only one of the two is how the original coordinate bug survived review.
    """

    phase: FitPhase
    attempt: int
    stable_readbacks: int
    required_readbacks: int
    requested_client_logical: tuple[float, float] | None
    achieved_client_logical: tuple[float, float] | None
    achieved_client_backing_px: tuple[int, int] | None
    geometry: ViewportGeometry | None
    detail: str
    started_at_s: float = 0.0
    settled_at_s: float | None = None

    @property
    def ok(self) -> bool:
        """Whether the client is usable, canonical or not."""
        return self.phase in (FitPhase.CANONICAL_VERIFIED, FitPhase.ACHIEVED_CLAMPED)

    @property
    def clamped(self) -> bool:
        return self.phase is FitPhase.ACHIEVED_CLAMPED

    def describe(self) -> str:
        if self.requested_client_logical is None:
            return f"{self.phase.value}: {self.detail}"
        want = self.requested_client_logical
        got = self.achieved_client_logical
        backing = self.achieved_client_backing_px
        achieved = (
            "not read back"
            if got is None
            else (
                f"{got[0]:g}x{got[1]:g} pt"
                + (f" / {backing[0]}x{backing[1]} px" if backing else "")
            )
        )
        return (
            f"{self.phase.value}: requested {want[0]:g}x{want[1]:g} pt, achieved "
            f"{achieved} ({self.stable_readbacks}/{self.required_readbacks} stable) - "
            f"{self.detail}"
        )

    @classmethod
    def idle(cls) -> ViewportFit:
        return cls(
            phase=FitPhase.IDLE,
            attempt=0,
            stable_readbacks=0,
            required_readbacks=0,
            requested_client_logical=None,
            achieved_client_logical=None,
            achieved_client_backing_px=None,
            geometry=None,
            detail="no fit requested; the client was adopted as it is",
        )


# ---------------------------------------------------------------------------
# Capture and evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapturedFrame:
    """One coherent stamped observation of the client area (bug B12).

    ``captured_at_s`` is when acquisition *began*: it is the conservative
    origin for pixel age. ``completed_at_s`` is throughput diagnostics only
    and must never be used to make old pixels look young (plan 5).
    """

    sequence: int
    captured_at_s: float
    completed_at_s: float
    duration_ms: float
    geometry: ViewportGeometry
    bgr: NDArray[np.uint8]
    duplicate: bool = False
    capture_error: str | None = None
    content_id: int | None = None
    backend: str = ""

    def __post_init__(self) -> None:
        if self.bgr.flags.writeable:
            raise ValueError("CapturedFrame.bgr must be non-writeable; use freeze_array()")

    @property
    def canonical_size_px(self) -> tuple[int, int]:
        return (self.bgr.shape[1], self.bgr.shape[0])

    @property
    def supports_calibrated_pixels(self) -> bool:
        """Calibrated constants only mean anything on a canonical client."""
        return self.geometry.state.supports_calibrated_pixels

    def age_s(self, now_s: float) -> float:
        """Age measured from the start of acquisition, computed at read time.

        There is deliberately no stored ``stale`` flag: a boolean written when
        the frame was queued becomes false information while it waits.
        """
        return now_s - self.captured_at_s

    def sample_mean_rgb(
        self, point_px: tuple[int, int], box_px: int
    ) -> tuple[float, float, float]:
        """Mean RGB of a ``box_px`` square centred on a client-relative point.

        Mirrors the legacy detector's sampling exactly (mean over an NxN box,
        BGR source) so migrated pixel constants keep their meaning.
        """
        half = box_px // 2
        x, y = point_px
        top = max(0, y - half)
        left = max(0, x - half)
        patch = self.bgr[top : top + box_px, left : left + box_px, :3]
        if patch.size == 0:
            return (0.0, 0.0, 0.0)
        b, g, r = (float(v) for v in patch.reshape(-1, 3).mean(0))
        return (r, g, b)


@dataclass(frozen=True)
class RawFrame:
    """One delivery from a capture backend, already in the canonical raster.

    Backends normalize (crop the client, letterbox, scale) before handing a
    frame over, because every backend can do that far more cheaply than a
    per-frame CPU resize - ScreenCaptureKit does it on the GPU.

    ``content_id`` is the *source's* own notion of frame identity. When the
    backend can supply one, uniqueness is authoritative rather than guessed
    from pixel comparison.
    """

    bgr: NDArray[np.uint8]
    geometry: ViewportGeometry
    captured_at_s: float
    presented_at_s: float
    content_id: int | None
    backend: str
    #: CPU time spent cropping, letterboxing, and scaling into the canonical
    #: raster. Zero for backends that do it on the GPU (ScreenCaptureKit).
    normalize_ms: float = 0.0

    @property
    def size_px(self) -> tuple[int, int]:
        return (self.bgr.shape[1], self.bgr.shape[0])


class PerformanceTier(Enum):
    """Cadence tiers the governor selects between.

    ``DEGRADED`` is a truthful state, not a fallback that pretends to be fine:
    below 30 unique frames per second the application says so rather than
    reporting a healthy-looking number produced from stale or duplicate frames.
    """

    DEGRADED = 15
    MINIMUM = 30
    STANDARD = 60
    HIGH = 90
    MAXIMUM = 120

    @property
    def fps(self) -> int:
        return int(self.value)

    @property
    def interval_s(self) -> float:
        return 1.0 / float(self.value)

    @property
    def acceptable(self) -> bool:
        """At least near-real-time. Below this the UI shows a degraded state."""
        return self.fps >= PerformanceTier.MINIMUM.fps


@dataclass(frozen=True)
class LatencySummary:
    """p50/p95/p99 of one measured stage, in milliseconds."""

    label: str
    samples: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float

    def describe(self) -> str:
        return (
            f"{self.label} p50 {self.p50_ms:.1f} p95 {self.p95_ms:.1f} p99 {self.p99_ms:.1f} ms"
        )


class CadenceMode(Enum):
    """The cadence choice a person makes, in words rather than in hertz.

    "120 Hz" is a number about the machine; "High" is a decision about what to
    spend on this. Each mode is a *ceiling* and a starting point - the governor
    still refuses to hold a tier it is not achieving, so choosing High on a
    machine that cannot sustain it produces an honest downshift rather than a
    misleading label.
    """

    EFFICIENT = "Efficient"
    BALANCED = "Balanced"
    HIGH = "High"
    AUTO = "Auto"

    @property
    def start_tier(self) -> PerformanceTier:
        """Where the governor begins.

        ``AUTO`` starts at the preferred 60 and probes upward, rather than
        starting at 120 and discovering it cannot hold it: an honest climb
        costs one probe, a hopeful start costs a downshift on every session.
        """
        return {
            CadenceMode.EFFICIENT: PerformanceTier.MINIMUM,
            CadenceMode.BALANCED: PerformanceTier.STANDARD,
            CadenceMode.HIGH: PerformanceTier.MAXIMUM,
            CadenceMode.AUTO: PerformanceTier.STANDARD,
        }[self]

    @property
    def max_tier(self) -> PerformanceTier:
        return {
            CadenceMode.EFFICIENT: PerformanceTier.MINIMUM,
            CadenceMode.BALANCED: PerformanceTier.STANDARD,
            CadenceMode.HIGH: PerformanceTier.MAXIMUM,
            CadenceMode.AUTO: PerformanceTier.MAXIMUM,
        }[self]

    @property
    def description(self) -> str:
        return {
            CadenceMode.EFFICIENT: (
                "30 Hz. The lowest cadence Live will accept, and the least CPU."
            ),
            CadenceMode.BALANCED: (
                "60 Hz. The preferred operating target: fast enough for Live "
                "with room to spare."
            ),
            CadenceMode.HIGH: (
                "Up to 120 Hz. Opportunistic - held only while the source "
                "supplies unique frames and perception keeps up."
            ),
            CadenceMode.AUTO: (
                "Starts at 60 and climbs only if the machine proves it can "
                "sustain more. Recommended."
            ),
        }[self]


class GovernorState(Enum):
    """The cadence governor's explicit state.

    ``PROBE`` and ``COOLDOWN`` exist so an upward attempt that fails is
    *remembered*: without them a source capped at 60 Hz oscillates forever
    between 60 and 90, and every oscillation costs a measurement epoch.
    """

    WARMUP = "warmup"
    STABLE = "stable"
    PROBE = "probe"
    COOLDOWN = "cooldown"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class CadenceReport:
    """Why the governor is where it is, with the evidence it used.

    ``live_eligible`` is the field Live gating reads. It is deliberately not
    ``state is STABLE``: a tier can be stable and still too slow, and a healthy
    probe is not yet proven.
    """

    state: GovernorState
    tier: PerformanceTier
    requested_hz: int
    samples: int
    processed_ratio: float
    observation_loss: float
    p95_age_ms: float | None
    live_eligible: bool
    reason: str
    changes: int = 0
    probes: int = 0
    failed_probes: int = 0

    def describe(self) -> str:
        return (
            f"{self.state.value} @ {self.tier.fps} Hz "
            f"(processed {self.processed_ratio * 100:.0f}% of tier, "
            f"loss {self.observation_loss * 100:.1f}%, n={self.samples}) - {self.reason}"
        )


@dataclass(frozen=True)
class RateSummary:
    """One counter reported as both a session total and a rate.

    A bare cumulative number is unreadable after an hour: "7055 dropped" reads
    as a catastrophe whether it happened in the last second or over a whole
    session. Counters here are per **session**; anything lifetime says so.
    """

    label: str
    session_total: int
    per_second: float
    lifetime_total: int = 0

    def describe(self) -> str:
        return f"{self.session_total} ({self.per_second:.1f}/s)"


@dataclass(frozen=True)
class CaptureMetrics:
    """What the pipeline is actually doing, as opposed to what it was asked to.

    Six independent rates, because conflating them is how "15 fps" and "73 fps"
    were both true at once in the same session:

    ``requested_hz``   what the source was asked for
    ``source_fps``     deliveries per second, duplicates included
    ``unique_fps``     deliveries whose content actually changed
    ``processed_fps``  frames perception turned into an observation
    ``control_fps``    observations that produced a control decision
    ``preview_fps``    frames the dashboard drew

    Every count is per **session** and resets with the measurement epoch;
    lifetime totals are carried separately and labelled as such.
    """

    backend: str
    tier: PerformanceTier
    requested_hz: int
    source_fps: float
    unique_fps: float
    processed_fps: float
    control_fps: float
    preview_fps: float
    duplicate_frames: RateSummary
    superseded_frames: RateSummary
    dropped_observations: RateSummary
    stale_frames: RateSummary
    pool_exhausted: RateSummary
    slot_depth: int
    reacquisitions: int
    frame_age_ms: float | None
    capture: LatencySummary
    normalize: LatencySummary
    perception: LatencySummary
    decision: LatencySummary
    preview: LatencySummary
    end_to_end: LatencySummary
    cpu_percent: float
    rss_current_mb: float
    rss_peak_mb: float
    governor: CadenceReport
    epoch: int = 0
    degraded_reason: str | None = None
    #: Capture-to-observation latency over the **recent** window the governor
    #: and Live eligibility judge on; ``end_to_end`` keeps the longer history
    #: for diagnostics so one old outlier can neither hide nor block.
    end_to_end_recent: LatencySummary | None = None
    #: The pipeline is inside a settling period after a cadence, source,
    #: geometry or profile change, or waiting for the backend to acknowledge
    #: a reconfiguration; samples taken now are tagged, not judged.
    settling: bool = False
    #: Whether a perception consumer is attached. Without one, processed
    #: throughput is genuinely zero and is reported as such.
    consumer_attached: bool = False

    @property
    def healthy(self) -> bool:
        return self.degraded_reason is None and self.unique_fps >= PerformanceTier.MINIMUM.fps

    @property
    def live_eligible(self) -> bool:
        """Whether cadence alone permits Live. Never a claim about anything else."""
        return self.governor.live_eligible

    @property
    def observation_loss_ratio(self) -> float:
        seen = self.processed_fps + self.dropped_observations.per_second
        return self.dropped_observations.per_second / seen if seen > 0 else 0.0

    def summary_line(self) -> str:
        return (
            f"{self.backend} {self.tier.fps}Hz  unique {self.unique_fps:.0f}  "
            f"processed {self.processed_fps:.0f}  preview {self.preview_fps:.0f}  "
            f"lat p95 {self.end_to_end.p95_ms:.0f} ms  cpu {self.cpu_percent:.0f}%  "
            f"rss {self.rss_current_mb:.0f} MB (peak {self.rss_peak_mb:.0f})"
        )


class _EvidenceMintKey:
    """Construction capability for :class:`EvidenceToken`.

    Only the capture registry holds an instance, so feature code cannot mint
    its own authority (plan 5).
    """

    __slots__ = ()


EVIDENCE_MINT_KEY = _EvidenceMintKey()


@dataclass(frozen=True)
class EvidenceToken:
    """Opaque proof that a command was derived from one specific real frame.

    It attests to provenance and freshness only - never to detector
    correctness. ``InputAuthority`` additionally checks that the exact token
    object was registered by the capture registry, so a hand-built token with
    plausible fields is still rejected.
    """

    run_id: str
    generation: int
    frame_sequence: int
    captured_at_s: float
    duration_ms: float
    viewport_identity: tuple[object, ...]
    _mint_key: _EvidenceMintKey = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._mint_key is not EVIDENCE_MINT_KEY:
            raise PermissionError("EvidenceToken may only be minted by the capture registry")


@dataclass(frozen=True)
class FrameEnvelope:
    """A frame paired with the token that authorizes acting on it."""

    frame: CapturedFrame
    evidence_token: EvidenceToken


# ---------------------------------------------------------------------------
# Input authority
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeaseHandle:
    """A monotonic capability to hold exactly one key or button.

    ``expires_at_s`` is authoritative: the watchdog releases on it regardless
    of what the owning worker is doing (plan 4.5).
    """

    lease_id: int
    generation: int
    key: InputKey | None
    button: MouseButton | None
    acquired_at_s: float
    expires_at_s: float

    def __post_init__(self) -> None:
        if (self.key is None) == (self.button is None):
            raise ValueError("LeaseHandle covers exactly one key or one button")

    def describe(self) -> str:
        return self.key.value if self.key is not None else f"mouse:{self.button.value}"  # type: ignore[union-attr]


@dataclass(frozen=True)
class ReleaseReport:
    """Outcome of a ``release_all()``.

    ``release_known_safe`` is the only field the coordinator may gate Live on.
    An empty ledger alone is explicitly insufficient (plan 4.4).
    """

    attempted_edges: tuple[str, ...]
    failures: tuple[str, ...]
    deadman_acknowledged: bool
    ledger_empty: bool
    release_known_safe: bool
    reason: str

    @property
    def uncertain(self) -> bool:
        return not self.release_known_safe

    @property
    def evidence_clean(self) -> bool:
        """Whether *this* release itself went perfectly.

        Distinct from ``release_known_safe``, which also refuses while an
        earlier run's uncertainty is still latched. That distinction is what
        stops a recovery record perpetuating itself: a clean release performed
        under an inherited latch is still a clean release, and must not write a
        fresh record saying otherwise.

        Only ``release_known_safe`` may gate Live. This may not.
        """
        return not self.failures and self.deadman_acknowledged and self.ledger_empty


class NavigationApplyStatus(Enum):
    APPLIED = auto()
    REJECTED_GENERATION = auto()
    REJECTED_EVIDENCE = auto()
    REJECTED_FOCUS = auto()
    REJECTED_VIEWPORT = auto()
    REJECTED_CANCELLED = auto()
    REJECTED_HEALTH = auto()


@dataclass(frozen=True)
class NavigationApplyResult:
    status: NavigationApplyStatus
    detail: str
    leases_held: tuple[str, ...] = ()

    @property
    def applied(self) -> bool:
        return self.status is NavigationApplyStatus.APPLIED


class CommandStage(Enum):
    """How far a movement command actually got. Ordered, and not merged.

    The distinction this exists to enforce: ``CGEventPost`` returning without
    raising is **not** evidence that anything moved. It is evidence that the
    call returned. Several very different faults produce the sentence "it says
    APPLIED and the character does not move", and until the stages were named
    separately the only available diagnosis was a guess.
    """

    REQUESTED = "requested"
    OS_EDGE_POSTED = "os_edge_posted"
    AUTHORITY_APPLIED = "authority_applied"
    GAME_MOTION_CONFIRMED = "game_motion_confirmed"
    REJECTED = "rejected"
    RELEASED = "released"

    @property
    def is_success(self) -> bool:
        """Only observed motion counts. Applied-without-movement is a failure."""
        return self is CommandStage.GAME_MOTION_CONFIRMED


class CommandOutcome(Enum):
    """What actually happened to a movement command, for the action overlay.

    The distinction this enum exists to enforce is between *asking* and
    *acting*. A requested command and an applied one look identical at the
    point they are decided and are completely different facts, and an overlay
    that blurs them tells a person the character is walking when it is not.
    """

    #: No command this frame - abstaining, acquiring, or between routes.
    NONE = "none"
    #: Shadow. The policy proposed this and nothing could have emitted it: the
    #: session is a NoInputSession and holds no authority to press anything.
    WOULD = "would"
    #: Live, and the input authority confirmed every edge landed. The glyphs
    #: are read from the leases it reports holding, never from the request.
    APPLIED = "applied"
    #: Live, and the authority refused - generation, evidence, focus, viewport,
    #: cancellation or health. Nothing is being held.
    REJECTED = "rejected"
    #: Deliberately released: a policy release, Stop, a fault, or worker exit.
    RELEASED = "released"

    @property
    def holds_input(self) -> bool:
        """Whether this outcome means keys are physically down right now."""
        return self is CommandOutcome.APPLIED


#: Lease target -> the glyph the overlay draws for it. Turning is deliberately
#: distinct from strafing: they are different actuators and a person watching a
#: route needs to see which one is running.
LEASE_GLYPHS: dict[str, str] = {
    "w": "W",
    "s": "S",
    "a": "A",
    "d": "D",
    "left": "<",
    "right": ">",
    "space": "JUMP",
}


@dataclass(frozen=True)
class CommandVisualization:
    """What the action overlay draws, and where every part of it came from.

    Constructed *after* the command has been proposed (Shadow) or applied
    (Live), never before - which is the whole point. Built by
    ``for_shadow`` / ``for_live`` / ``none`` rather than by hand, so a caller
    cannot accidentally build an APPLIED packet out of a request.
    """

    outcome: CommandOutcome
    #: The atomic identity of the frame and world this belongs to. A consumer
    #: compares it exactly as it compares an observation's key.
    key: RuntimeKey | None = None
    #: What the policy asked for. Present for WOULD and REJECTED too, because
    #: "it wanted to turn right and was refused" is the useful diagnostic.
    requested: NavigationCommand | None = None
    #: The authority's verdict. ``None`` in Shadow, which never asks.
    status: NavigationApplyStatus | None = None
    #: The authority's own answer about what is held. Empty unless APPLIED.
    leases_held: tuple[str, ...] = ()
    detail: str = ""
    #: True only for a real live session; Shadow provenance is never live.
    live: bool = False
    #: Set when the packet is terminal, frozen or stopped. The overlay clears
    #: the action layer outright rather than drawing a stale command.
    frozen: bool = False
    #: Whether perception saw the world move while this was held. ``None``
    #: means the estimator abstained - not that nothing moved. Applied without
    #: motion is the exact failure "it says APPLIED and nothing happens"
    #: describes, so it is recorded rather than assumed away.
    motion_confirmed: bool | None = None
    #: The yaw delta that **went out**, as opposed to the one that was asked
    #: for. ``None`` when nothing reported one. A yaw post can fail on its own
    #: without blocking the whole command, so drawing the request would show a
    #: camera movement that never happened.
    yaw_applied_px: int | None = None
    #: How long the longest current hold has been down, in milliseconds.
    held_ms: float = 0.0
    #: The mechanism the edges went through, for the overlay caption.
    backend: str = ""

    @classmethod
    def none(cls, *, detail: str = "", live: bool = False) -> CommandVisualization:
        return cls(CommandOutcome.NONE, detail=detail, live=live)

    @classmethod
    def released(cls, *, detail: str, live: bool = False) -> CommandVisualization:
        return cls(CommandOutcome.RELEASED, detail=detail, live=live)

    @classmethod
    def for_shadow(
        cls, command: NavigationCommand, *, key: RuntimeKey | None = None
    ) -> CommandVisualization:
        """A proposal. Shadow holds a NoInputSession and cannot emit an edge."""
        return cls(
            CommandOutcome.WOULD,
            key=key,
            requested=command,
            detail=command.reason,
            live=False,
        )

    @classmethod
    def for_live(
        cls,
        command: NavigationCommand,
        result: NavigationApplyResult,
        *,
        key: RuntimeKey | None = None,
        motion_confirmed: bool | None = None,
    ) -> CommandVisualization:
        """An applied - or refused - command, described by the authority.

        On anything but APPLIED the leases are dropped on the floor: a refusal
        released whatever had landed, so there is nothing to draw.
        """
        applied = result.status is NavigationApplyStatus.APPLIED
        return cls(
            CommandOutcome.APPLIED if applied else CommandOutcome.REJECTED,
            key=key,
            requested=command,
            status=result.status,
            leases_held=result.leases_held if applied else (),
            detail=result.detail,
            live=True,
            motion_confirmed=motion_confirmed,
        )

    @classmethod
    def for_movement(
        cls,
        command: NavigationCommand,
        outcome: Any,
        *,
        key: RuntimeKey | None = None,
        motion_confirmed: bool | None = None,
    ) -> CommandVisualization:
        """An applied - or blocked - movement, described by the actuator.

        The same contract as :meth:`for_live`: what is drawn comes from what
        the actuator reports it is *physically holding*, never from the command
        that was requested. ``leases_held`` keeps its name and its meaning -
        the targets that are down - so every consumer downstream is unchanged.
        """
        held = tuple(sorted(key_.value for key_ in outcome.held))
        blocked = bool(outcome.block.blocking)
        return cls(
            CommandOutcome.REJECTED if blocked else CommandOutcome.APPLIED,
            key=key,
            requested=command,
            status=NavigationApplyStatus.REJECTED_HEALTH
            if blocked
            else NavigationApplyStatus.APPLIED,
            leases_held=() if blocked else held,
            detail=outcome.block.value if blocked else outcome.detail,
            live=True,
            motion_confirmed=motion_confirmed,
            yaw_applied_px=int(getattr(outcome, "yaw_posted_px", 0)),
            held_ms=float(getattr(outcome, "held_ms", 0.0)),
            backend=str(getattr(outcome, "backend", "")),
        )

    @classmethod
    def for_held(
        cls,
        outcome: Any,
        *,
        key: RuntimeKey | None = None,
        motion_confirmed: bool | None = None,
    ) -> CommandVisualization:
        """What is down when nobody asked for anything new this frame.

        A level-triggered actuator holds until it is told otherwise, so a frame
        that carried no new evidence - a repeat, or one just past its freshness
        budget - changes nothing and still has a keyboard state worth drawing.
        There is no ``requested`` command because there was no request: the
        overlay draws the ledger, which is the honest thing to draw.
        """
        held = tuple(sorted(key_.value for key_ in outcome.held))
        blocked = bool(outcome.block.blocking)
        return cls(
            CommandOutcome.REJECTED if blocked else CommandOutcome.APPLIED,
            key=key,
            requested=None,
            status=NavigationApplyStatus.REJECTED_HEALTH
            if blocked
            else NavigationApplyStatus.APPLIED,
            leases_held=() if blocked else held,
            detail=outcome.block.value if blocked else outcome.detail,
            live=True,
            motion_confirmed=motion_confirmed,
            yaw_applied_px=int(getattr(outcome, "yaw_posted_px", 0)),
            held_ms=float(getattr(outcome, "held_ms", 0.0)),
            backend=str(getattr(outcome, "backend", "")),
        )

    def freeze(self) -> CommandVisualization:
        """The terminal form: nothing is held, and it says so."""
        return replace(
            self,
            outcome=CommandOutcome.RELEASED
            if self.outcome is CommandOutcome.APPLIED
            else self.outcome,
            leases_held=(),
            frozen=True,
        )

    @property
    def glyphs(self) -> tuple[str, ...]:
        """The keys to draw, in a stable order.

        For APPLIED these come from ``leases_held`` - the authority's answer -
        and for WOULD from the request, because in Shadow there is no authority
        to ask and no input to misrepresent. Every other outcome draws nothing.
        """
        if self.frozen:
            return ()
        if self.outcome is CommandOutcome.APPLIED:
            targets = set(self.leases_held)
        elif self.outcome is CommandOutcome.WOULD and self.requested is not None:
            command = self.requested
            targets = set()
            if command.forward_axis == 1:
                targets.add("w")
            elif command.forward_axis == -1:
                targets.add("s")
            if command.lateral_axis == -1:
                targets.add("a")
            elif command.lateral_axis == 1:
                targets.add("d")
            if command.turn_axis == -1:
                targets.add("left")
            elif command.turn_axis == 1:
                targets.add("right")
            if command.jump:
                targets.add("space")
        else:
            return ()
        order = list(LEASE_GLYPHS)
        return tuple(LEASE_GLYPHS[t] for t in order if t in targets)

    @property
    def yaw_px(self) -> int:
        """Relative mouse yaw, drawn only when it actually went out.

        A refused yaw edge fails the whole apply, so a non-zero value here on
        an APPLIED packet means the edge landed.
        """
        if self.frozen:
            return 0
        if self.outcome not in (CommandOutcome.APPLIED, CommandOutcome.WOULD):
            return 0
        if self.yaw_applied_px is not None:
            # The actuator's answer. A yaw post can fail without blocking the
            # command, and drawing the request in that case would put a camera
            # movement on screen that never left the process.
            return self.yaw_applied_px
        return 0 if self.requested is None else self.requested.yaw_delta_px

    @property
    def stage(self) -> CommandStage:
        """How far this command actually got, in the honest vocabulary.

        ``APPLIED`` means the authority holds the leases. It does **not** mean
        the character moved: that is a separate observation, and conflating the
        two is what makes "it says APPLIED and nothing happens" unanswerable.
        """
        if self.outcome is CommandOutcome.REJECTED:
            return CommandStage.REJECTED
        if self.outcome is CommandOutcome.RELEASED:
            return CommandStage.RELEASED
        if self.outcome is CommandOutcome.WOULD:
            return CommandStage.REQUESTED
        if self.outcome is CommandOutcome.APPLIED:
            if self.motion_confirmed:
                return CommandStage.GAME_MOTION_CONFIRMED
            return CommandStage.AUTHORITY_APPLIED
        return CommandStage.REQUESTED

    @property
    def label(self) -> str:
        """The word drawn beside the glyphs, with the evidence behind it.

        On an applied command the word alone was never enough: ACTIVE said
        nothing about *how long* the key had been down or *which mechanism* it
        went through, and both are the first things asked when a character is
        not moving. They are appended only when they are known, so Shadow's
        badge stays the single word it has always been.
        """
        if self.outcome is CommandOutcome.APPLIED and self.motion_confirmed is False:
            # Held, and the world is not moving. Saying ACTIVE here would be
            # the single most misleading thing the overlay could do.
            head = "NO MOTION"
        else:
            head = {
                CommandOutcome.NONE: "",
                CommandOutcome.WOULD: "WOULD",
                CommandOutcome.APPLIED: "ACTIVE",
                CommandOutcome.REJECTED: "REJECTED",
                CommandOutcome.RELEASED: "RELEASED",
            }[self.outcome]
        if self.outcome is not CommandOutcome.APPLIED:
            return head
        parts = [head]
        if self.held_ms >= 1.0:
            parts.append(f"{self.held_ms / 1000.0:.1f}s")
        if self.backend:
            parts.append(self.backend)
        return "  ".join(parts)

    @property
    def active(self) -> bool:
        """Whether there is anything at all for the action layer to draw."""
        return bool(self.glyphs) or self.yaw_px != 0


# ---------------------------------------------------------------------------
# Perception observations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArrowObservation:
    profile_id: str | None
    track_id: int | None
    bbox_px: tuple[int, int, int, int] | None
    centroid_px: tuple[float, float] | None
    tip_px: tuple[float, float] | None
    axis_unit_xy: tuple[float, float] | None
    confidence: float
    valid: bool
    abstain_reason: str | None = None
    #: The far end of the principal axis. Drawing tail-to-tip shows the shaft
    #: the direction estimate was taken from, which a bounding box cannot.
    tail_px: tuple[float, float] | None = None
    #: Per-term confidence breakdown, ordered as the detector scored them.
    #: Present whether the candidate was accepted or rejected, because "why
    #: not" is the half of the diagnostic that says what to fix.
    score_terms: tuple[tuple[str, float], ...] = ()
    #: How far ahead of the runner-up this candidate scored. A thin margin is
    #: an abstention, not a win.
    score_margin: float = 0.0
    #: Midpoint of the two arrowhead notches - the base of the head, and the
    #: origin of the well-conditioned signed direction (see D-024).
    notch_mid_px: tuple[float, float] | None = None
    #: The two detected notch points, for the Full Diagnostics overlay.
    notch_px: tuple[tuple[float, float], tuple[float, float]] | None = None
    #: sqrt(area) / canonical frame height. Scale continuity is checked against
    #: this rather than raw pixels, so it survives a viewport change.
    scale_norm: float = 0.0
    #: How long this identity has been held, in accepted frames.
    track_age: int = 0


@dataclass(frozen=True)
class CueReading:
    """One direction cue's answer, kept next to every other cue's.

    ``weight`` is what the robust consensus actually gave it, so a cue that was
    rejected as an outlier is visibly present with weight zero rather than
    silently missing.
    """

    cue_id: str
    heading_deg: float | None
    confidence: float
    weight: float
    valid: bool
    note: str = ""


@dataclass(frozen=True)
class DirectionObservation:
    error_deg: float | None
    confidence: float
    cue_id: str
    cue_disagreement_deg: float | None
    valid: bool
    abstain_reason: str | None = None
    #: Confidence that the *sign* is right, separate from the angle's accuracy.
    #: An unsigned axis with a perfect magnitude is still a coin flip, and the
    #: controller must be able to see the difference (mission section 8).
    sign_confidence: float = 0.0
    #: Margin in degrees between the accepted polarity and its opposite.
    sign_margin_deg: float = 0.0
    #: Every cue that was evaluated, with the weight consensus gave it.
    cues: tuple[CueReading, ...] = ()
    #: Ratio of the shape's principal eigenvalues. Below the configured floor
    #: the axis is ill-conditioned and PCA is not usable as a direction cue.
    anisotropy: float = 0.0


@dataclass(frozen=True)
class MotionObservation:
    forward_speed_norm: float | None
    lateral_speed_norm: float | None
    confidence: float
    inlier_count: int
    inlier_ratio: float
    spatial_coverage: float
    residual: float
    yaw_contamination: float
    valid: bool
    abstain_reason: str | None = None


@dataclass(frozen=True)
class ArrivalObservation:
    confidence: float
    support_hits: int
    support_window: int
    latched_map_id: str | None
    valid: bool
    evidence: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Navigation command
# ---------------------------------------------------------------------------


class CommandKind(Enum):
    """What a navigation command is *for*.

    The distinction is a safety boundary, not documentation: an ``ALIGN`` pulse
    turns the camera with the character stationary, and it may never renew a
    forward lease. Only ``FOLLOW`` may hold ``W``, and it may only do so inside
    the validated alignment threshold (mission section 11).
    """

    RELEASE = "release"
    ALIGN = "align"
    FOLLOW = "follow"

    @property
    def may_hold_forward(self) -> bool:
        return self is CommandKind.FOLLOW


class ControlState(Enum):
    """The steering controller's state.

    Five of these are *moving* states, and that is the whole point of the set.
    The controller this replaces had exactly one - ``FOLLOW`` - so every other
    thing it could be doing (correcting a heading, riding out an occlusion,
    looking for a target it lost) implied standing still, and the character
    stopped dead several times a second on an ordinary route.

    Correcting a heading and standing still are different facts. So are "the
    arrow is behind a bush" and "there is nothing to walk towards".
    """

    ACQUIRE = "acquire"
    """Standing still, waiting for a first trustworthy heading. Bounded."""

    ALIGN = "align"
    """Standing still and turning, because the target is behind us. Only a
    genuinely severe heading error reaches this, and only after confirmation."""

    FOLLOW = "follow"
    """Walking, inside the deadband, no correction wanted."""

    CORRECT = "correct"
    """Walking *and* turning. The ordinary state of a route with any curve in
    it, and the one the previous controller could not express."""

    COAST = "coast"
    """Walking on a remembered heading because the arrow is momentarily
    unreadable. Turning is decayed out; the forward hold is kept."""

    SEARCH = "search"
    """Walking a bounded shallow sweep because the arrow has been unreadable
    past the coast grace. Terminates in its own budget, never loops."""

    REACQUIRE = "reacquire"
    """Standing still with nothing to steer by. Bounded, and never the answer
    to an occlusion while the character is already walking."""

    BLOCKED = "blocked"
    """Contact suspected or a recovery maneuver in progress."""

    SAFE_STOP = "safe_stop"

    @property
    def holds_forward(self) -> bool:
        """Whether this state walks. Five of nine, by design."""
        return self in (
            ControlState.FOLLOW,
            ControlState.CORRECT,
            ControlState.COAST,
            ControlState.SEARCH,
            ControlState.BLOCKED,
        )

    @property
    def pursuing(self) -> bool:
        """Whether this state is following a target, moving or not.

        Used to decide whether controller memory survives a transition: moving
        between pursuing states preserves the heading filter and the target
        identity, leaving them does not.
        """
        return self in (
            ControlState.FOLLOW,
            ControlState.CORRECT,
            ControlState.COAST,
            ControlState.SEARCH,
            ControlState.ALIGN,
        )


@dataclass(frozen=True)
class NavigationCommand:
    """One bounded, evidence-bound movement intent.

    ``valid_until_s`` may never exceed ``source_captured_at_s +
    max_evidence_age_s``; re-reading or republishing the same frame cannot
    extend it (plan 5).
    """

    generation: int
    source_frame_sequence: int
    source_captured_at_s: float
    forward_axis: Literal[-1, 0, 1]
    lateral_axis: Literal[-1, 0, 1]
    jump: bool
    yaw_delta_px: int
    issued_at_s: float
    valid_until_s: float
    reason: str
    kind: CommandKind = CommandKind.FOLLOW
    #: Camera rotation by held arrow key: -1 left, +1 right, 0 none. Distinct
    #: from ``lateral_axis`` (strafe) and from ``yaw_delta_px`` (relative
    #: mouse) because they are three different actuators with three different
    #: failure modes. At most one turn actuator may be commanded per tick.
    turn_axis: Literal[-1, 0, 1] = 0

    def __post_init__(self) -> None:
        if self.forward_axis not in (-1, 0, 1):
            raise ValueError(f"forward_axis out of range: {self.forward_axis}")
        if self.lateral_axis not in (-1, 0, 1):
            raise ValueError(f"lateral_axis out of range: {self.lateral_axis}")
        if self.turn_axis not in (-1, 0, 1):
            raise ValueError(f"turn_axis out of range: {self.turn_axis}")
        if self.turn_axis != 0 and self.yaw_delta_px != 0:
            # Two actuators asking for the same rotation would double it, and
            # the response model is fitted per backend. One at a time.
            raise ValueError("a command may not use both the turn keys and mouse yaw")
        if self.valid_until_s < self.issued_at_s:
            raise ValueError("NavigationCommand expires before it is issued")
        if not self.kind.may_hold_forward and self.forward_axis != 0:
            raise ValueError(f"{self.kind.value} command may not command forward motion")

    @property
    def is_neutral(self) -> bool:
        return (
            self.forward_axis == 0
            and self.lateral_axis == 0
            and self.turn_axis == 0
            and not self.jump
            and self.yaw_delta_px == 0
        )

    @property
    def turns(self) -> bool:
        """Whether this command asks for camera rotation by any actuator."""
        return self.turn_axis != 0 or self.yaw_delta_px != 0


# ---------------------------------------------------------------------------
# Runtime intents and coordinator messages
# ---------------------------------------------------------------------------


class RunMode(Enum):
    IDLE = auto()
    SHADOW = auto()
    LIVE = auto()
    SERVICE = auto()
    SAFE_STOP = auto()

    @property
    def emits_input(self) -> bool:
        """Only LIVE and the bounded SERVICE mode may hold an input capability."""
        return self in (RunMode.LIVE, RunMode.SERVICE)


class NavigationPhase(Enum):
    """The route's lifecycle state, as the navigator reports it.

    Deliberately the same vocabulary as :class:`ControlState` for the states
    the two share, so a reader never has to translate between "the controller
    says CORRECT" and "the phase says something else".
    """

    ACQUIRE = auto()
    ALIGN = auto()
    FOLLOW = auto()
    CORRECT = auto()
    COAST = auto()
    SEARCH = auto()
    CONTACT = auto()
    RECOVERY = auto()
    REACQUIRE = auto()
    ARRIVAL_CONFIRM = auto()
    ARRIVED = auto()
    ABANDONED = auto()
    FAILED = auto()

    @property
    def terminal(self) -> bool:
        return self in (
            NavigationPhase.ARRIVED,
            NavigationPhase.ABANDONED,
            NavigationPhase.FAILED,
        )

    @property
    def moving(self) -> bool:
        """Whether the character is expected to be walking in this phase."""
        return self in (
            NavigationPhase.FOLLOW,
            NavigationPhase.CORRECT,
            NavigationPhase.COAST,
            NavigationPhase.SEARCH,
            NavigationPhase.RECOVERY,
        )


# ---------------------------------------------------------------------------
# Automatic runtime setup
# ---------------------------------------------------------------------------


class SetupStage(Enum):
    """Where automatic setup has got to.

    One explicit, typed, bounded stage per thing that can independently fail.
    A stage is never "probably fine": it either produced the evidence its
    successor needs, or it named the reason it could not.
    """

    IDLE = "idle"
    FIND_ROBLOX = "find_roblox"
    FIT_VIEWPORT = "fit_viewport"
    RESTART_CAPTURE = "restart_capture"
    STABILIZE_CAPTURE = "stabilize_capture"
    SELECT_PROFILE = "select_profile"
    ESTABLISH_REFERENCE = "establish_reference"
    VERIFY_INPUT = "verify_input"
    VERIFY_CONTROL_MODE = "verify_control_mode"
    CHARACTERIZE_TURN = "characterize_turn"
    SHADOW_QUALIFY = "shadow_qualify"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (SetupStage.READY, SetupStage.FAILED, SetupStage.CANCELLED)

    @property
    def emits_input(self) -> bool:
        """Whether this stage can inject input into the game.

        Only the three live probes can, and each runs under an arm token with
        the character stationary and releases before the next observation.
        Everything before them is read-only, which is why Start Navigator is
        safe to press with no arming at all.
        """
        return self in (
            SetupStage.VERIFY_INPUT,
            SetupStage.VERIFY_CONTROL_MODE,
            SetupStage.CHARACTERIZE_TURN,
        )


class SetupFailureKind(Enum):
    """Why automatic setup stopped, in the vocabulary a user can act on."""

    NO_WINDOW = "no_window"
    AMBIGUOUS_WINDOW = "ambiguous_window"
    PERMISSION = "permission"
    FULLSCREEN = "fullscreen"
    RESIZE_DENIED = "resize_denied"
    VIEWPORT_UNUSABLE = "viewport_unusable"
    CAPTURE_STALE = "capture_stale"
    PROFILE_AMBIGUOUS = "profile_ambiguous"
    REFERENCE_UNSTABLE = "reference_unstable"
    INPUT_NOT_ACCEPTED = "input_not_accepted"
    CONTROL_MODE_UNVERIFIED = "control_mode_unverified"
    ACTUATOR_UNPROVEN = "actuator_unproven"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


@dataclass(frozen=True)
class SetupFailure:
    """One bounded failure: what went wrong, and the single thing to do next.

    ``remedy`` is deliberately one sentence. A user staring at a stopped
    navigator needs the next action, not a checklist of everything that could
    in principle be wrong.
    """

    kind: SetupFailureKind
    stage: SetupStage
    summary: str
    remedy: str
    detail: str = ""

    def describe(self) -> str:
        return f"{self.summary} - {self.remedy}"


@dataclass(frozen=True)
class SetupProgress:
    """One observable snapshot of the setup machine. Published, never polled.

    Carries the achieved geometry and the chosen backend because those are the
    two facts a user most often needs to see *while* something is going wrong.
    """

    stage: SetupStage
    attempt: int
    detail: str
    started_at_s: float
    updated_at_s: float
    failure: SetupFailure | None = None
    #: Requested and achieved client size, in logical points.
    requested_client_logical: tuple[float, float] | None = None
    achieved_client_logical: tuple[float, float] | None = None
    achieved_client_backing_px: tuple[int, int] | None = None
    profile_id: str | None = None
    turn_backend: str | None = None

    @property
    def ok(self) -> bool:
        return self.stage is SetupStage.READY

    @property
    def running(self) -> bool:
        return not self.stage.terminal and self.stage is not SetupStage.IDLE

    def elapsed_s(self, now_s: float) -> float:
        return max(0.0, now_s - self.started_at_s)

    @classmethod
    def idle(cls) -> SetupProgress:
        return cls(
            stage=SetupStage.IDLE,
            attempt=0,
            detail="press Start Navigator",
            started_at_s=0.0,
            updated_at_s=0.0,
        )


class IntentType(Enum):
    #: The one production entry point. Runs automatic setup: find Roblox, fit
    #: and verify the viewport, restart capture, lock a profile, establish the
    #: reference, then observe. No manual step precedes it (mission section A).
    START_NAVIGATOR = auto()
    #: Re-run automatic setup from IDLE after a bounded failure. Advanced only.
    RETRY_SETUP = auto()
    #: Bind capture to the Roblox client as it is. Moves nothing, sends
    #: nothing, and is the recommended path (mission section 4).
    CONNECT_WINDOW = auto()
    #: Optional: resize the client to the canonical size and verify what was
    #: actually achieved. Distinct from CONNECT_WINDOW on purpose - capture
    #: must never depend on a resize succeeding.
    FIT_VIEWPORT = auto()
    #: Retained name for FIT_VIEWPORT, so an existing caller keeps working.
    PIN_WINDOW = FIT_VIEWPORT
    #: Swap the active arrow profile by stable id at a frame boundary.
    SELECT_PROFILE = auto()
    #: Leave Live but keep observing: releases movement, keeps perception.
    RETURN_TO_SHADOW = auto()
    START_SHADOW = auto()
    #: The single deliberate physical authorization. It both authorizes and
    #: begins Live in one coordinator transaction; there is no separate
    #: ``ARM_LIVE_FROM_UI`` gesture any more (DECISIONS.md D-062).
    START_LIVE = auto()
    STOP = auto()
    RESET_CHARACTER = auto()
    PAN_SWAP_TEST = auto()
    SHUTDOWN = auto()
    #: Deviation from plan 3.2, recorded in DECISIONS.md (D-015). A bounded
    #: SERVICE mode that keeps the pre-navigator dig loop reachable while the
    #: Live lifecycle's gates are pending.
    DIG_LOOP = auto()
    #: Deviation from plan 3.2, recorded in DECISIONS.md (D-013). The explicit
    #: release-only recovery handshake plan 4.4 requires before an unsafe-release
    #: latch can be cleared. It emits only up-edges.
    RECOVER_RELEASE = auto()
    #: The bounded native movement check (mission section C, D-064). It is
    #: *not* wired by :func:`~prospector_engine.application.build_application`:
    #: only a process launched with ``treasure.py --forward-probe`` registers a
    #: worker for it, so in the dashboard - and in every other process - it
    #: resolves to "no worker" and does nothing. It runs as a bounded SERVICE,
    #: so it needs the same positive focus, fresh capture, live watchdog,
    #: healthy deadman and empty ledger that Live does.
    FORWARD_PROBE = auto()
    #: Deviation from plan 3.2, recorded in DECISIONS.md (D-002). The legacy F3
    #: calibration read-out is preserved as a first-class intent so it flows
    #: through the coordinator like everything else. It samples pixels and shows
    #: a popup; it can never reach an input session.
    PIXEL_INFO = auto()

    @property
    def priority(self) -> int:
        """Lower sorts first in the coordinator's priority queue (plan 3.2)."""
        if self in (IntentType.STOP, IntentType.SHUTDOWN):
            return 0
        return 2  # safety faults occupy priority 1 and are not intents


@dataclass(frozen=True)
class PhysicalChordProof:
    """Evidence that a real key edge from the registered listener minted this.

    ``source="hotkey"`` is a *label*: any code in the process can write it, and
    for as long as it was the only check, "did a human press the chord" and
    "did something say it did" were the same question. This is the answer to
    the first one.

    The nonce is created inside :class:`~prospector_engine.coordinator.RuntimeCoordinator`
    at construction, handed to exactly one hotkey listener as a capability
    object, and never published - not to the GUI, not to a worker, not to the
    CLI. An intent without it is refused for Live no matter what its ``source``
    field says.
    """

    nonce: str
    chord: str
    minted_at_s: float


@dataclass(frozen=True)
class RuntimeIntent:
    sequence: int
    intent_type: IntentType
    source: Literal["gui", "hotkey", "system"]
    created_at_s: float
    #: Present only on an intent minted by the physical chord capability.
    proof: PhysicalChordProof | None = None

    def coalescing_key(self) -> tuple[IntentType, str] | None:
        """Ordinary duplicates coalesce; safety intents never do."""
        if self.intent_type.priority == 0:
            return None
        return (self.intent_type, self.source)


class SafetyFaultKind(Enum):
    FOCUS_LOST = auto()
    FOCUS_UNKNOWN = auto()
    VIEWPORT_INVALID = auto()
    CAPTURE_STALE = auto()
    CAPTURE_FAILED = auto()
    DEADMAN_UNHEALTHY = auto()
    LEASE_EXPIRED = auto()
    WORKER_ERROR = auto()
    RELEASE_UNCERTAIN = auto()


@dataclass(frozen=True)
class SafetyFault:
    generation: int | None
    kind: SafetyFaultKind
    evidence: tuple[str, ...]
    observed_at_s: float


class ModeResultKind(Enum):
    COMPLETED = auto()
    CANCELLED = auto()
    FAILED = auto()
    ARRIVED = auto()
    ABANDONED = auto()
    SESSION_COMPLETE = auto()


@dataclass(frozen=True)
class ModeResult:
    kind: ModeResultKind
    detail: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkerCompletion:
    generation: int
    mode: RunMode
    worker_id: str
    result: ModeResult


@dataclass(frozen=True)
class FitCompletion:
    """A finished Fit & Verify attempt, handed to the serialized coordinator.

    The fit thread never touches coordinator, capture or arm state itself:
    it submits this and the coordinator loop applies it, ignoring a stale
    generation and invalidating geometry only if the revision moved.
    """

    generation: int
    fit: ViewportFit
    revision_before: int


class BlockerScope(Enum):
    """What a Live blocker is a statement about.

    Four different questions that one flat list used to blur together:
    whether Shadow can run at all, what the machine is doing right now,
    which native commissioning evidence is missing, and whether keyboard and
    camera output may be enabled this minute.
    """

    SHADOW = "shadow readiness"
    RUNTIME = "current condition"
    EVIDENCE = "native commissioning evidence"
    LIVE = "live control eligibility"


@dataclass(frozen=True)
class LiveBlocker:
    """One keyed reason Live cannot be enabled, recomputed on every read.

    ``status`` is ``blocking`` (must change), ``expected`` (a normal state of
    affairs while using the dashboard, such as Roblox not being frontmost),
    or ``pending`` (an evidence gate that has not been run). Exactly one
    blocker exists per ``code``; details that belong to the same gate are
    grouped under it rather than listed as separate permanent blockers.
    """

    code: str
    scope: BlockerScope
    status: str
    summary: str
    detail: str
    remedy: str
    evidence: str = ""

    #: Statuses that describe the world rather than refuse anything. A caller
    #: asking "is Live blocked" must filter on :attr:`blocking` rather than on
    #: a status string, because adding a fourth status to the string test in
    #: two separate modules is how the dashboard and the coordinator came to
    #: disagree about the same row in the first place.
    ADVISORY_STATUSES: ClassVar[frozenset[str]] = frozenset({"expected", "advisory"})

    @property
    def blocking(self) -> bool:
        """Whether this row actually stops Live."""
        return self.status not in self.ADVISORY_STATUSES

    def describe(self) -> str:
        return f"{self.code}: {self.summary}"


# ---------------------------------------------------------------------------
# Bounded service outcomes (plan 10)
# ---------------------------------------------------------------------------


class ServiceKind(Enum):
    RESET_CHARACTER = auto()
    PAN_SWAP = auto()
    DIG = auto()
    NEXT_MAP = auto()


class DigOutcome(Enum):
    DIG_PROGRESS = auto()
    TREASURE_COMPLETE = auto()
    PAN_FULL = auto()
    CUE_LOST = auto()
    TIMEOUT = auto()
    CANCELLED = auto()
    FAILED = auto()


@dataclass(frozen=True)
class DigEvidence:
    """Exactly what was on screen when a dig outcome was decided."""

    frame_sequence: int
    dig_spot_a_rgb: tuple[float, float, float] | None
    dig_spot_b_rgb: tuple[float, float, float] | None
    capacity_rgb: tuple[float, float, float] | None
    on_dig_spot: bool
    capacity_full: bool
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class DigHandoffResult:
    outcome: DigOutcome
    evidence: DigEvidence
    elapsed_s: float
    attempts: int
    detail: str


class PanSwapOutcome(Enum):
    SUCCESS = auto()
    TIMEOUT = auto()
    CANCELLED = auto()
    FAILED = auto()


@dataclass(frozen=True)
class PanSwapResult:
    outcome: PanSwapOutcome
    attempts: int
    elapsed_s: float
    detail: str
    evidence: tuple[str, ...] = ()


class ResetOutcome(Enum):
    SUCCESS = auto()
    TIMEOUT = auto()
    CANCELLED = auto()
    FAILED = auto()


@dataclass(frozen=True)
class ResetResult:
    outcome: ResetOutcome
    elapsed_s: float
    detail: str
    evidence: tuple[str, ...] = ()


class NextMapOutcome(Enum):
    EQUIPPED = auto()
    NO_MAPS = auto()
    AMBIGUOUS = auto()
    TIMEOUT = auto()
    CANCELLED = auto()
    FAILED = auto()


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArrowCandidateRecord:
    """One considered blob, kept whether it was accepted or not.

    Rejections are the useful half of the diagnostic: "no arrow" and "four
    arrows, all plausible" look identical in a boolean but need opposite fixes.
    """

    label: int
    area_px: int
    bbox_px: tuple[int, int, int, int]
    centroid_px: tuple[float, float]
    score: float
    accepted: bool
    rejected_reason: str | None = None
    #: Every scoring term by name, in the order the detector applied them, so a
    #: rejection can be read as "shape 0.91, contrast 0.12" rather than a
    #: single opaque number.
    score_terms: tuple[tuple[str, float], ...] = ()
    contour_px: tuple[tuple[int, int], ...] = ()
    #: Where this candidate ended up: ``proposed`` (scored, below threshold),
    #: ``viable`` (above threshold, not chosen), ``selected`` (the single
    #: candidate the observation was built from), ``challenger`` (contesting
    #: the held identity), ``rejected`` (failed a hard constraint). Exactly one
    #: record per observation is ``selected``; ``accepted`` mirrors that.
    state: str = "proposed"

    def term(self, name: str) -> float | None:
        for key, value in self.score_terms:
            if key == name:
                return value
        return None


class PacketKind(Enum):
    """Why a dashboard packet exists.

    A ``TRANSITION`` or ``TERMINAL`` packet carries no frame: it is how Stop,
    a profile swap, or a source replacement reaches the UI as a fact rather
    than as the absence of an update (mission section 6).
    """

    FRAME = "frame"
    TRANSITION = "transition"
    TERMINAL = "terminal"


@dataclass(frozen=True, order=True)
class RuntimeKey:
    """The identity every dashboard packet is stamped with.

    Ordering is lexicographic over the whole tuple, which is what lets the UI
    reject an older or mismatched packet with a single comparison instead of
    seven ad-hoc checks. A change in any of the first six fields invalidates
    every in-flight observation, tracker state, and actionable command.
    """

    run_id: str
    coordinator_generation: int
    mode_session_id: int
    source_epoch: int
    geometry_revision: int
    profile_revision: int
    frame_sequence: int
    content_id: int | None = None

    @property
    def session_key(self) -> tuple[str, int, int, int, int, int]:
        """Everything except the per-frame fields.

        Two packets with different session keys describe different worlds; one
        may not be drawn over the other, and observations may not cross the
        boundary (mission section 6).
        """
        return (self.run_id, *self.ordinal)

    @property
    def ordinal(self) -> tuple[int, int, int, int, int]:
        """The world's position in time.

        Every component is a **monotonic non-decreasing** counter within one
        process run: the coordinator generation, the mode session, the capture
        source epoch, the viewport revision, and the profile revision. Because
        none of them ever goes backwards, a newer world has every component
        greater than or equal to an older one's and at least one strictly
        greater - which makes tuple order the correct comparison, and is what
        lets a straggling frame from a cancelled worker be recognised as old
        rather than merely different.
        """
        return (
            self.coordinator_generation,
            self.mode_session_id,
            self.source_epoch,
            self.geometry_revision,
            self.profile_revision,
        )

    def supersedes(self, other: RuntimeKey | None) -> bool:
        """Whether this packet may replace ``other`` on screen."""
        if other is None:
            return True
        if self.run_id != other.run_id:
            # Not from this process run at all. Nothing to compare.
            return False
        if self.ordinal == other.ordinal:
            return self.frame_sequence > other.frame_sequence
        return self.ordinal > other.ordinal

    def describe(self) -> str:
        return (
            f"run {self.run_id} gen {self.coordinator_generation} "
            f"session {self.mode_session_id} source {self.source_epoch} "
            f"geometry {self.geometry_revision} profile {self.profile_revision} "
            f"frame {self.frame_sequence}"
        )


@dataclass(frozen=True)
class PursuitTelemetry:
    """One state-change's worth of navigation state, in numbers a person reads.

    Deliberately flat and deliberately small: it is emitted on every frame but
    only *written out* when something in it changed, so it has to be cheap to
    build and cheap to compare. Every field is an observation - what is held,
    what was measured, how much budget is left - and none of it is a request.

    The distinction that runs through it: ``held_keys`` comes from the
    actuator's own ledger and ``wanted_keys`` from the controller. When the two
    differ, something refused a press, and that is precisely the case no
    earlier version of this could show.
    """

    state: ControlState
    phase: NavigationPhase
    #: What the actuator reports it is physically holding, right now.
    held_keys: tuple[str, ...] = ()
    #: What the controller asked for on this frame.
    wanted_keys: tuple[str, ...] = ()
    reason: str = ""

    #: Target memory.
    track_id: int | None = None
    arrow_age_ms: float | None = None
    error_deg: float | None = None
    raw_error_deg: float | None = None
    heading_rate_deg_s: float = 0.0
    heading_confidence: float = 0.0
    heading_spread_deg: float = 0.0

    #: Motion evidence.
    speed_norm: float | None = None
    baseline_norm: float | None = None
    progress_ratio: float | None = None
    stall_ms: float = 0.0
    forward_held_ms: float = 0.0

    #: The bounded episodes.
    search_elapsed_ms: float = 0.0
    recovery_rung: str = ""
    recovery_side: int = 0
    recovery_jumps: int = 0
    recovery_elapsed_ms: float = 0.0
    recovery_input_ms: float = 0.0
    #: Why a maneuver escalated, resolved, or was abandoned. Written once per
    #: transition, not per frame.
    escalation: str = ""
    #: Sector costs as ``(centre_deg, cost)``, decayed to this instant.
    sectors: tuple[tuple[float, float], ...] = ()

    def movement_line(self) -> str:
        """The composite movement, as the overlay draws it: ``W + D + > + JUMP``."""
        return " + ".join(self.held_keys) if self.held_keys else "nothing held"

    def changed_from(self, other: PursuitTelemetry | None) -> bool:
        """Whether this is worth a new log line rather than a repeat.

        State, what is actually held, and the recovery rung. Numbers churn
        every frame and are deliberately not part of the comparison - they are
        carried on the line, they do not cause one.
        """
        if other is None:
            return True
        return (
            self.state is not other.state
            or self.phase is not other.phase
            or self.held_keys != other.held_keys
            or self.recovery_rung != other.recovery_rung
            or self.escalation != other.escalation
        )

    def describe(self) -> str:
        """One line for the log a person reads."""
        parts = [f"{self.state.value.upper()} {self.movement_line()}"]
        if self.error_deg is not None:
            parts.append(f"err {self.error_deg:+.0f}deg")
        if self.arrow_age_ms is not None and self.arrow_age_ms > 50.0:
            parts.append(f"arrow {self.arrow_age_ms:.0f}ms old")
        if self.progress_ratio is not None:
            parts.append(f"speed {self.progress_ratio:.0%} of baseline")
        if self.recovery_rung:
            side = {-1: "left", 1: "right"}.get(self.recovery_side, "")
            parts.append(f"recovery {self.recovery_rung} {side}".strip())
        if self.escalation:
            parts.append(self.escalation)
        return "; ".join(parts)


@dataclass(frozen=True)
class DiagnosticObservation:
    """Everything derived from ONE frame, produced once and shared.

    The preview and the controller consume the same instance, so the overlay
    can never show observation N drawn over frame N+1. ``frame_sequence`` and
    ``content_id`` tie it to the exact source frame, and every consumer is
    expected to check them rather than assume freshness.
    """

    #: The exact frame this was derived from. Holding it - rather than a copy
    #: of its identifiers - is what makes it impossible for the preview to draw
    #: observation N over frame N+1.
    frame: CapturedFrame
    processed_at_s: float
    published_at_s: float

    #: The identity every consumer compares before drawing or acting. A packet
    #: whose key does not supersede what is on screen is discarded, which is
    #: what makes "preview frame 53545, decision frame 53542" impossible.
    key: RuntimeKey

    profile_id: str | None
    profile_status: str
    strategy_id: str

    arrow: ArrowObservation
    candidates: tuple[ArrowCandidateRecord, ...]
    contour_px: tuple[tuple[int, int], ...]

    anchor_px: tuple[float, float] | None
    forward_deg: float | None
    forward_source: str
    desired_deg: float | None
    direction: DirectionObservation
    #: Every candidate cue evaluated on this frame, not only the selected one,
    #: each carrying the weight robust consensus gave it. Seeing which cues
    #: agreed - and which were rejected as outliers, at weight zero - is the
    #: whole diagnostic value of E-DIR-IDEAL, and a consensus abstention is far
    #: more informative next to its components than on its own.
    cues: tuple[CueReading, ...]
    motion: MotionObservation | None
    arrival: ArrivalObservation | None

    phase: NavigationPhase | None
    command: NavigationCommand | None
    abstain_reason: str | None
    #: What actually happened to ``command`` - proposed, applied, refused or
    #: released - filled in *after* the worker has acted on it. The overlay
    #: draws this and never ``command``, which is only ever a request.
    command_view: CommandVisualization = field(default_factory=CommandVisualization.none)

    capture_ms: float = 0.0
    normalize_ms: float = 0.0
    perception_ms: float = 0.0
    decision_ms: float = 0.0

    packet_kind: PacketKind = PacketKind.FRAME
    #: The steering controller's state, or ``None`` outside a navigation mode.
    control_state: ControlState | None = None
    #: One sentence a person can act on: "Turn right 60 degrees". Composed
    #: where the reasoning lives, so the UI cannot paraphrase it into a
    #: different claim (mission section 10).
    plain_summary: str = ""
    #: Why Live output is blocked right now, in the same plain language.
    blockers: tuple[str, ...] = ()
    #: Stage timings and the tracker's verdict for this frame.
    timing: PerceptionTiming | None = None
    #: What the pursuit controller and the recovery ladder were doing when this
    #: frame was decided. ``None`` outside a navigation mode.
    pursuit: PursuitTelemetry | None = None

    @property
    def frame_sequence(self) -> int:
        return self.frame.sequence

    @property
    def content_id(self) -> int | None:
        return self.frame.content_id

    @property
    def geometry(self) -> ViewportGeometry:
        return self.frame.geometry

    @property
    def captured_at_s(self) -> float:
        return self.frame.captured_at_s

    @property
    def age_s(self) -> float:
        return monotonic_s() - self.frame.captured_at_s

    @property
    def signed_error_deg(self) -> float | None:
        return self.direction.error_deg if self.direction.valid else None

    @property
    def has_direction_arms(self) -> bool:
        """Whether the overlay has enough to draw both arms and the arc."""
        return self.anchor_px is not None and (
            self.forward_deg is not None or self.desired_deg is not None
        )

    def canonical_to_preview(self, canvas_px: tuple[int, int]) -> Affine2D:
        return self.geometry.preview_from_canonical(canvas_px)


@dataclass(frozen=True)
class ActuatorState:
    """What the machine is *physically doing*, as distinct from what was asked.

    Every field here is read from the input authority's own ledger or from the
    answer it gave, never from the command a planner wanted. The dashboard bug
    this exists to end: the overlay drew a bright forward arrow for a command
    the authority had refused, so "the planner asked for W" and "W is down"
    looked identical on screen.
    """

    #: Targets the ledger says are held right now, e.g. ``("w", "mouse:left")``.
    held: tuple[str, ...] = ()
    #: How long forward has been *continuously* down. Zero when it is not.
    forward_held_ms: float = 0.0
    #: Down/up edges posted this run, for the rattle check.
    down_edges: int = 0
    up_edges: int = 0
    #: Holds that came up and were re-pressed inside the lapse window.
    hold_lapses: int = 0
    #: The last relative yaw actually posted, and when.
    last_yaw_delta_px: int = 0
    last_yaw_at_s: float = 0.0
    #: The turn actuator this run measured and chose, or "" before it has.
    turn_backend: str = ""
    #: The most recent confirmed player displacement, normalized. ``None``
    #: means the estimator abstained, which is not the same as "did not move".
    last_displacement_norm: float | None = None
    #: Why nothing is being sent, in the words the authority or the navigator
    #: used. Empty while input is flowing.
    blocked_reason: str = ""

    @property
    def forward_held(self) -> bool:
        return InputKey.W.value in self.held

    def describe(self) -> str:
        if not self.held:
            return self.blocked_reason or "nothing held"
        held = " ".join(sorted(target.split(":")[-1].upper() for target in self.held))
        if self.forward_held:
            return f"{held} held {self.forward_held_ms / 1000.0:.1f}s"
        return f"{held} held"


@dataclass(frozen=True)
class TelemetrySnapshot:
    """One emit-on-change view of the whole runtime (bug B13)."""

    sequence: int
    mode: RunMode
    phase: NavigationPhase | None
    viewport: ViewportGeometry | None
    arrow: ArrowObservation | None
    direction: DirectionObservation | None
    motion: MotionObservation | None
    arrival: ArrivalObservation | None
    command: NavigationCommand | None
    ledger_empty: bool
    focus: FocusState
    frame_age_ms: float | None
    warnings: tuple[str, ...] = ()
    readiness: Mapping[str, str] = field(default_factory=dict)
    metrics: CaptureMetrics | None = None
    #: The last fit attempt, so the UI can say "clamped to 1024x768" rather
    #: than only "non-canonical".
    fit: ViewportFit | None = None
    #: Live blockers in plain language, ready to render as a checklist.
    live_blockers: tuple[str, ...] = ()
    #: The same reasons as keyed, scoped objects; the plain strings above are
    #: derived from these and kept for the header line.
    blockers: tuple[LiveBlocker, ...] = ()
    #: A Fit & Verify attempt is in flight.
    fit_active: bool = False
    #: How the previous session ended. Neutral by construction: a clean stop is
    #: not a fault, and red is reserved for a fault that is happening now.
    last_session_note: str = ""
    control_state: ControlState | None = None
    #: The outcome of the last Live authorization attempt, in one phrase:
    #: ``"none"``, ``"granted <id>"`` or ``"refused: <reason>"``. It replaced
    #: ``arm_state``, which counted down a token from a button that no longer
    #: exists (D-062).
    live_authorization: str = "none"
    #: Whether the global chord listener is currently hearing the keyboard,
    #: and its own one-line account of itself.
    hotkey_ready: bool = False
    hotkey_detail: str = ""
    #: The physical actuator, read from the ledger rather than the plan.
    actuator: ActuatorState = field(default_factory=lambda: ActuatorState())
    recording: str = "off"
    #: Where automatic setup has got to. The production UI renders this rather
    #: than a list of gates, because it is the thing that is actually happening.
    setup: SetupProgress | None = None


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


@runtime_checkable
class CancellationToken(Protocol):
    """Cooperative cancellation for every bounded worker and service."""

    def is_cancelled(self) -> bool: ...

    def wait(self, timeout_s: float) -> bool:
        """Sleep up to ``timeout_s``; return True if cancelled meanwhile."""
        ...


class Cancellation:
    """The concrete cooperative :class:`CancellationToken` used everywhere.

    ``wait`` returns ``True`` as soon as cancellation is requested, which is
    what lets a multi-second bounded service (the 8 s post-reset settle, say)
    be cut off within one poll slice instead of running to completion.
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout_s: float) -> bool:
        return self._event.wait(max(0.0, timeout_s))
