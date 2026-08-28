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
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Literal, Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "ArrivalObservation",
    "ArrowObservation",
    "Cancellation",
    "CancellationToken",
    "CapturedFrame",
    "ClientRectPhysicalPx",
    "DigEvidence",
    "DigHandoffResult",
    "DigOutcome",
    "EvidenceStatus",
    "EvidenceToken",
    "FocusState",
    "FrameEnvelope",
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
    "PanSwapOutcome",
    "PanSwapResult",
    "PinResult",
    "Provenance",
    "ResetOutcome",
    "ResetResult",
    "RunMode",
    "RuntimeIntent",
    "SafetyFault",
    "SafetyFaultKind",
    "ServiceKind",
    "TelemetrySnapshot",
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
class ClientRectPhysicalPx:
    """The canonical Roblox **client area** in physical pixels.

    Every capture, click, detector, recorder, and diagnostic coordinate in the
    application is relative to this rect. Outer-window geometry never leaves
    the platform port (plan 4.1, bug B11).
    """

    origin_px: tuple[int, int]
    size_px: tuple[int, int]
    scale: float
    verified_at_s: float
    display_id: str
    valid: bool
    invalid_reason: str | None = None

    @property
    def width_px(self) -> int:
        return self.size_px[0]

    @property
    def height_px(self) -> int:
        return self.size_px[1]

    def identity(self) -> tuple[int, int, int, int, str]:
        """Fields whose change invalidates every in-flight coordinate."""
        return (*self.origin_px, *self.size_px, self.display_id)

    def contains_client_point(self, point_px: tuple[int, int]) -> bool:
        x, y = point_px
        return 0 <= x < self.width_px and 0 <= y < self.height_px

    def to_screen_px(self, point_px: tuple[int, int]) -> tuple[int, int]:
        """Client-relative -> screen-absolute physical pixels.

        Only the platform port may call this; feature code never handles a
        desktop coordinate (plan 4.3).
        """
        return (point_px[0] + self.origin_px[0], point_px[1] + self.origin_px[1])


@dataclass(frozen=True)
class PinResult:
    ok: bool
    message: str
    rect: ClientRectPhysicalPx | None = None


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
    client_rect: ClientRectPhysicalPx
    bgr: NDArray[np.uint8]
    duplicate: bool = False
    capture_error: str | None = None

    def __post_init__(self) -> None:
        if self.bgr.flags.writeable:
            raise ValueError("CapturedFrame.bgr must be non-writeable; use freeze_array()")

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
    viewport_identity: tuple[int, int, int, int, str]
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


@dataclass(frozen=True)
class DirectionObservation:
    error_deg: float | None
    confidence: float
    cue_id: str
    cue_disagreement_deg: float | None
    valid: bool
    abstain_reason: str | None = None


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

    def __post_init__(self) -> None:
        if self.forward_axis not in (-1, 0, 1):
            raise ValueError(f"forward_axis out of range: {self.forward_axis}")
        if self.lateral_axis not in (-1, 0, 1):
            raise ValueError(f"lateral_axis out of range: {self.lateral_axis}")
        if self.valid_until_s < self.issued_at_s:
            raise ValueError("NavigationCommand expires before it is issued")

    @property
    def is_neutral(self) -> bool:
        return (
            self.forward_axis == 0
            and self.lateral_axis == 0
            and not self.jump
            and self.yaw_delta_px == 0
        )


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
    ACQUIRE = auto()
    ALIGN = auto()
    FOLLOW = auto()
    CONTACT = auto()
    RECOVERY = auto()
    REACQUIRE = auto()
    ARRIVAL_CONFIRM = auto()
    ARRIVED = auto()
    ABANDONED = auto()
    FAILED = auto()


class IntentType(Enum):
    PIN_WINDOW = auto()
    START_SHADOW = auto()
    ARM_LIVE_FROM_UI = auto()
    START_LIVE = auto()
    STOP = auto()
    RESET_CHARACTER = auto()
    PAN_SWAP_TEST = auto()
    SHUTDOWN = auto()
    #: Deviation from plan 3.2, recorded in DECISIONS.md (D-013). The explicit
    #: release-only recovery handshake plan 4.4 requires before an unsafe-release
    #: latch can be cleared. It emits only up-edges.
    RECOVER_RELEASE = auto()
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
class RuntimeIntent:
    sequence: int
    intent_type: IntentType
    source: Literal["gui", "hotkey", "system"]
    created_at_s: float

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
class TelemetrySnapshot:
    """One emit-on-change view of the whole runtime (bug B13)."""

    sequence: int
    mode: RunMode
    phase: NavigationPhase | None
    viewport: ClientRectPhysicalPx | None
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
