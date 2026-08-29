"""The composition root: every object the process owns, wired once.

This module builds the application and nothing else. It deliberately imports
no user interface, because two very different front ends need the same
assembly:

* ``treasure_gui.Dashboard``, the Tk window a person actually uses;
* ``treasure.py --setup-probe``, which runs the *real* automatic setup against
  the live client with no window and no input at all.

Keeping the wiring here is what makes the second one honest. When the
composition root lived inside the Tk module, the only way to exercise
automatic setup was to open a dashboard, so the native check that matters most
- "does Start Navigator reach READY on this machine?" - could only ever be
answered by a throwaway script. It is now a committed, bounded command.

Nothing in this module sends input. ``build_application`` constructs the input
authority but presses nothing; the live worker behind ``START_LIVE`` still
requires a physical arm before it can move anything.
"""

from __future__ import annotations

import contextlib
import os
import queue
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Any

from prospector_engine.acceptance import AcceptanceConfig
from prospector_engine.autosetup import (
    AutomaticSetup,
    CaptureSample,
    ControlModeSample,
    PerceptionSample,
    ProfileVote,
    SetupConfig,
    WindowProbe,
)
from prospector_engine.capture import CaptureService, EvidenceRegistry, ViewportGuard
from prospector_engine.contracts import (
    CapturedFrame,
    IntentType,
    ModeResult,
    ModeResultKind,
    SafetyFault,
    SetupProgress,
    ViewportFit,
    monotonic_s,
)
from prospector_engine.coordinator import (
    CoordinatorConfig,
    RuntimeCoordinator,
    WorkerContext,
    WorkerFactory,
)
from prospector_engine.engine import (
    DEFAULT_PIXELS,
    ServiceContext,
    run_dig_loop,
    run_pan_swap,
    run_reset,
    run_wiggle_to_chest,
)
from prospector_engine.geometry import ViewportGeometry
from prospector_engine.input_authority import (
    AuthorityConfig,
    DeadmanClient,
    HealthSources,
    InputAuthority,
)
from prospector_engine.navigation import (
    DegreeMonitor,
    NavigationCapabilities,
    PerceptionPipeline,
    make_forward_probe_worker,
    make_live_prologue,
    make_live_worker,
    make_shadow_worker,
)
from prospector_engine.plainlog import PlainLog, Topic, Verdict
from prospector_engine.ports import PlatformPort, create_platform_port, current_platform_name
from prospector_engine.preflight import InputPreflight, gather
from prospector_engine.steering import SteeringLimits
from prospector_engine.telemetry import AppPaths, EventLog, LatestSlot, resolve_app_paths
from prospector_engine.turning import ControlFingerprint, TurnResponse, TurnResponseCache
from prospector_engine.vision import ArrowSegmenter, ProfileAuthority, load_profiles

# ---------------------------------------------------------------------------
# Worker factories
# ---------------------------------------------------------------------------


def _service_worker(kind: str) -> WorkerFactory:
    """Wrap a bounded engine service as a coordinator mode worker."""

    def worker(context: WorkerContext) -> ModeResult:
        session = context.service
        if session is None:
            return ModeResult(ModeResultKind.FAILED, f"{kind} started without an input session")
        service_context = ServiceContext(
            frames=context.frames,
            session=session,
            cancel=context.cancellation,
            deadline_s=monotonic_s() + 90.0,
            on_status=context.on_status,
        )
        kind_map = {
            "SUCCESS": ModeResultKind.COMPLETED,
            "CANCELLED": ModeResultKind.CANCELLED,
        }
        if kind == "reset":
            result = run_reset(service_context)
            return ModeResult(
                kind_map.get(result.outcome.name, ModeResultKind.FAILED),
                f"reset: {result.detail}",
                result.evidence,
            )
        if kind == "dig_loop":
            # The dig loop runs for as long as it is productive, so it gets the
            # service's own deadline rather than the 90 s the one-shot services use.
            service_context.deadline_s = monotonic_s() + 31 * 60.0
            dig = run_dig_loop(service_context)
            digs_ok = dig.outcome.name in ("CANCELLED", "TIMEOUT", "PAN_FULL", "CUE_LOST")
            return ModeResult(
                ModeResultKind.CANCELLED if digs_ok else ModeResultKind.FAILED,
                f"dig loop: {dig.taps} taps, {dig.pan_swaps} pan swaps - {dig.detail}",
                dig.evidence,
            )
        swap = run_pan_swap(service_context)
        return ModeResult(
            kind_map.get(swap.outcome.name, ModeResultKind.FAILED),
            f"pan swap: {swap.detail}",
            swap.evidence,
        )

    return worker


def _wiggle_worker(monitor: DegreeMonitor) -> WorkerFactory:
    """Ctrl+4: wiggle toward the degree monitor's heading error until
    X_MARKS_THE_SPOT is found, then release every key it holds.

    Reads ``monitor.current()`` fresh on every wiggle pass rather than once:
    whether Ctrl+5 is armed never changes how the loop behaves, only what
    angle it sees - it either keeps tracking a live reading, or holds
    whatever it last reported (0.0 if Ctrl+5 has never been pressed). See
    D-089 for why a single cold perception call was replaced with the warm
    monitor in the first place, and D-092 for the repeat-until-found chest
    behaviour itself.
    """

    def worker(context: WorkerContext) -> ModeResult:
        session = context.service
        if session is None:
            return ModeResult(ModeResultKind.FAILED, "wiggle started without an input session")
        # Unlike the other one-shot services this loops until it finds the
        # chest, so it gets the same "runs for as long as it needs to" treatment
        # as the dig loop's own deadline override, not the 90 s one-shot bound.
        service_context = ServiceContext(
            frames=context.frames,
            session=session,
            cancel=context.cancellation,
            deadline_s=monotonic_s() + 10 * 60.0,
            on_status=context.on_status,
        )
        result = run_wiggle_to_chest(
            service_context, monitor.current, forward_s=3.0, backward_s=0.5
        )
        kind_map = {
            "SUCCESS": ModeResultKind.COMPLETED,
            "CANCELLED": ModeResultKind.CANCELLED,
        }
        return ModeResult(
            kind_map.get(result.outcome.name, ModeResultKind.FAILED),
            f"wiggle: {result.detail} (degree={result.degree_deg:.1f})",
            result.evidence,
        )

    return worker


def _degree_monitor_worker(monitor: DegreeMonitor) -> WorkerFactory:
    """Ctrl+5: (re)arm the degree monitor and reset its stored angle to 0.

    Never reaches an input session and never touches ``context.frames``
    itself - the monitor already owns its own frame reads on its own thread,
    started here and left running. This factory just starts (or resets) it
    and returns immediately.
    """

    def worker(context: WorkerContext) -> ModeResult:
        del context
        monitor.start_or_reset()
        return ModeResult(ModeResultKind.COMPLETED, "degree monitor armed, angle reset to 0")

    return worker


def _pixel_info_worker(port: PlatformPort, on_report: Any) -> WorkerFactory:
    """F3: read the pixel under the cursor. Emits no input, reads one frame."""

    def worker(context: WorkerContext) -> ModeResult:
        envelope = context.frames.latest()
        cursor = port.cursor_client_px()
        if envelope is None or cursor is None:
            on_report("Pixel probe: Roblox client not verified, or cursor outside it.")
            return ModeResult(ModeResultKind.FAILED, "no client rect or cursor outside it")
        r, g, b = envelope.frame.sample_mean_rgb(cursor, DEFAULT_PIXELS.sample_box_px)
        message = (
            f"PIXEL=({cursor[0]},{cursor[1]}) RGB=({int(r)},{int(g)},{int(b)}) "
            f"[canonical client basis]"
        )
        on_report(message)
        return ModeResult(ModeResultKind.COMPLETED, message)

    return worker


# ---------------------------------------------------------------------------
# Automatic setup, wired to the real engine
# ---------------------------------------------------------------------------


class EngineSetupPort:
    """The real capture, viewport and perception objects, behind the setup port.

    Every method is a thin, honest adapter. The interesting choices - what
    counts as ambiguous, how a permission failure is distinguished from a
    missing window - live here rather than in the machine, because they are
    facts about this OS and these objects.
    """

    def __init__(
        self,
        *,
        port: PlatformPort,
        guard: ViewportGuard,
        capture: CaptureService,
        pipeline: PerceptionPipeline,
        profiles: ProfileAuthority,
        authority: InputAuthority,
    ) -> None:
        self._port = port
        self._guard = guard
        self._capture = capture
        self._pipeline = pipeline
        self._profiles = profiles
        self._authority = authority
        self._sequence = 0

    # -- window -----------------------------------------------------------
    def locate_window(self) -> WindowProbe:
        geometry = self._guard.connect()
        if geometry.valid:
            return WindowProbe(True, geometry.describe(), identity=geometry.identity())
        detail = (geometry.detail or "").lower()
        trusted = getattr(self._port, "accessibility_trusted", None)
        permission = bool(trusted is not None and not trusted())
        return WindowProbe(
            False,
            geometry.detail or "no Roblox window",
            ambiguous="ambiguous" in detail or "more than one" in detail,
            permission_denied=permission,
            fullscreen="fullscreen" in detail or "space" in detail,
        )

    def release_all_input(self, reason: str) -> None:
        self._authority.release_all(reason)

    def fit_viewport(self) -> ViewportFit:
        return self._guard.fit_and_lock()

    def viewport(self) -> ViewportGeometry:
        return self._guard.geometry

    # -- capture ----------------------------------------------------------
    def restart_capture(self, reason: str) -> None:
        if not self._capture.running:
            self._capture.start()
            return
        self._capture.restart_source(reason)

    def heal_viewport(self) -> bool:
        """Re-adopt the client when the guard has lost its pin.

        Observed natively, intermittently, right after a successful fit: the
        frames are the fitted 1280x720 and the guard reports UNPINNED with no
        adopted window, so every delivery is rejected as a mismatch and
        automatic setup fails with ``capture_stale`` on a condition that heals
        itself a moment later.

        ``connect()`` binds to the client and touches nothing, so this is safe
        to call from a polling loop; it returns whether the guard is usable
        afterwards.
        """
        geometry = self._guard.geometry
        if geometry.valid:
            return True
        return self._guard.connect().valid

    def capture_sample(self) -> CaptureSample:
        envelope = self._capture.latest()
        geometry = self._guard.geometry
        expected = geometry.canonical_px if geometry.valid else None
        if envelope is None:
            return CaptureSample(
                sequence=0,
                age_s=None,
                delivered_px=None,
                expected_px=expected,
                processed_fps=self._capture.processed_fps,
                error=self._capture.health() or "no frame has arrived yet",
            )
        frame = envelope.frame
        return CaptureSample(
            sequence=frame.sequence,
            age_s=frame.age_s(monotonic_s()),
            delivered_px=frame.canonical_size_px,
            expected_px=expected,
            processed_fps=self._capture.processed_fps,
            error=frame.capture_error or self._capture.health(),
        )

    # -- perception -------------------------------------------------------
    @contextlib.contextmanager
    def consuming(self) -> Iterator[None]:
        """Declare setup as a frame consumer for as long as it runs.

        Automatic setup really does consume frames and really does run the
        detector on them, so it registers like any other phase and reports its
        perception cost. What it must not do is *latch* consumption: reading a
        few frames used to mark the slot as consumed forever, so after setup
        finished the governor saw a processed rate of zero against a consumer
        that no longer existed and downshifted to 15 Hz - under the 30 the
        steering controller requires. Live then refused to start.
        """
        # measured=False: setup polls on its own bounded schedule, and that
        # cadence is not the pipeline's throughput.
        with self._capture.consuming("setup", measured=False):
            yield

    def _newest(self) -> CapturedFrame | None:
        envelope = self._capture.wait_for_new(self._sequence, 0.2)
        if envelope is None:
            return None
        self._sequence = envelope.frame.sequence
        return envelope.frame

    def profile_vote(self) -> ProfileVote | None:
        frame = self._newest()
        if frame is None:
            return None
        candidates = self._profiles.library.selectable()
        return ProfileVote(frame.sequence, self._pipeline.score_profiles(frame, candidates))

    def lock_profile(self, profile_id: str) -> None:
        """Stage the swap; the pipeline adopts it at the next frame boundary.

        Deliberately not routed through the coordinator: a profile *intent*
        also spends an arm token and safe-stops Live, which is right when a
        person changes the dropdown mid-run and wrong when automatic setup is
        choosing the profile before anything has started.
        """
        self._profiles.request(profile_id)
        self._pipeline.forget_classifiers()

    def perception_sample(self) -> PerceptionSample | None:
        frame = self._newest()
        if frame is None:
            return None
        result = self._pipeline.analyze(frame, map_id="setup", approach_valid=False)
        inputs = result.inputs
        return PerceptionSample(
            frame_sequence=frame.sequence,
            arrow_valid=inputs.arrow.valid,
            direction_valid=inputs.direction.valid,
            error_deg=inputs.direction.error_deg,
            confidence=inputs.direction.confidence,
            track_id=inputs.arrow.track_id,
            processed_fps=self._capture.processed_fps,
            frame_age_ms=frame.age_s(monotonic_s()) * 1000.0,
        )


def shift_lock_probe(
    cursor: Callable[[], tuple[int, int] | None],
) -> Callable[[CapturedFrame], ControlModeSample]:
    """Confirm the locked-camera control mode without ever toggling it.

    The cue is the cursor: in Shift Lock, Roblox replaces the pointer with a
    centred crosshair and holds it there. We do not have a classifier for that
    glyph and will not pretend to - what we *can* observe is that the system
    pointer stays pinned to the middle of the client while the camera is free
    to move, which is exactly what the locked mode does and what the free mode
    does not.

    When the pointer cannot be read at all the honest answer is "cannot
    confirm", and setup stops with a sentence telling the user to switch Shift
    Lock on. It never presses Shift to find out (D-037).

    ``cursor`` reports the pointer in **canonical** client coordinates, or
    ``None`` when it is outside the client - which is itself an answer, and the
    negative one.
    """

    def probe(frame: CapturedFrame) -> ControlModeSample:
        try:
            point = cursor()
        except Exception:
            return ControlModeSample(False, 0.0, "none", "the pointer could not be read")
        if point is None:
            return ControlModeSample(
                False, 0.0, "pointer", "the pointer is outside the Roblox client"
            )
        width, height = frame.canonical_size_px
        dx = abs(point[0] - width / 2.0) / max(1.0, width)
        dy = abs(point[1] - height / 2.0) / max(1.0, height)
        centred = dx < 0.06 and dy < 0.06
        return ControlModeSample(
            centred,
            0.9 if centred else 0.0,
            "pointer",
            "the pointer is held at the centre of the client, as Shift Lock does"
            if centred
            else "the pointer is not centred - Shift Lock does not look switched on",
        )

    return probe


# ---------------------------------------------------------------------------
# Application wiring
# ---------------------------------------------------------------------------


@dataclass
class Application:
    """Everything the process owns, constructed once and shut down once."""

    port: PlatformPort
    guard: ViewportGuard
    registry: EvidenceRegistry
    capture: CaptureService
    authority: InputAuthority
    coordinator: RuntimeCoordinator
    deadman: DeadmanClient
    preview: LatestSlot[Any]
    reports: queue.Queue[str]
    paths: AppPaths
    #: One pipeline shared with the running worker, so a profile change in the
    #: UI takes effect on the very next frame instead of the next session.
    pipeline: PerceptionPipeline
    #: Ctrl+5's heading-error reading, kept warm independent of RunMode
    #: (D-089). The dashboard polls ``current()``/``armed_count()`` to drive
    #: its own small degree-readout window; nothing else needs to reach in.
    degree_monitor: DegreeMonitor
    profiles: ProfileAuthority
    turn_cache: TurnResponseCache
    #: What this run has proven, read live. Automatic setup and the live
    #: prologue write into the same cell, so the dashboard and the workers
    #: cannot disagree about what has been measured.
    capabilities_provider: Callable[[], NavigationCapabilities]
    #: The readable running commentary, shared with the coordinator and the
    #: movement actuator. The engineering rings are unchanged; this is the
    #: half a person can read.
    plain: PlainLog
    #: The two factories the live prologue needs, exposed so a CLI mode can
    #: build the bounded native movement check without a second copy of this
    #: wiring. There is one composition root and this is it.
    control_fingerprint: Callable[[], ControlFingerprint]
    control_mode_probe: Callable[[CapturedFrame], ControlModeSample]
    #: The global chord listener, installed by ``main()``. Held here rather
    #: than as a local in ``main`` so the dashboard can *show* whether it is
    #: running: a listener that silently is not is indistinguishable from a
    #: hotkey that does nothing, and that was exactly the confusion.
    hotkeys: Any = None

    def enable_forward_probe(self, *, pulse_ms: int) -> AcceptanceConfig:
        """Register the bounded native movement check on *this* process only.

        Called by ``treasure.py --forward-probe`` and by nothing else. The
        dashboard never calls it, so in the dashboard ``FORWARD_PROBE``
        resolves to "no worker" and cannot emit an edge. That is the gate: the
        code path exists only in a process launched to run it (D-064).
        """
        config = replace(AcceptanceConfig(), pulse_ms=pulse_ms)
        self.coordinator.register_worker(
            IntentType.FORWARD_PROBE,
            make_forward_probe_worker(
                lambda: self.pipeline,
                fingerprint_factory=self.control_fingerprint,
                control_mode_probe=self.control_mode_probe,
                config=config,
            ),
        )
        return config

    @property
    def capabilities(self) -> NavigationCapabilities:
        return self.capabilities_provider()

    @property
    def turn_response(self) -> TurnResponse | None:
        return self.capabilities.turn_response

    @property
    def library(self) -> Any:
        return self.profiles.library

    def preflight(self) -> InputPreflight:
        """Everything Live depends on, read once, in one coherent pass.

        Assembled here because this is the only object that can see all of it
        at once: the platform port's permissions, the listener's health, the
        coordinator's readiness, the capture cadence and the authority's
        release state.
        """
        port = self.port
        listener = self.hotkeys
        return gather(
            os_name=sys.platform,
            launcher=_launcher_identity(),
            accessibility_probe=getattr(port, "event_post_trusted", None),
            listen_probe=getattr(port, "input_listening_trusted", None),
            capture_probe=self._capture_permitted,
            hotkey_running=bool(listener is not None and listener.is_running()),
            roblox_focused=port.focus_state(),
            processed_fps=self.capture.processed_fps,
            min_processed_fps=float(SteeringLimits().min_processed_fps),
            release_uncertain=self.authority.release_uncertain,
            ledger_empty=self.authority.ledger_empty(),
        )

    def _capture_permitted(self) -> bool:
        """Screen Recording, answered by whether frames are actually arriving."""
        if self.capture.latest() is not None:
            return True
        error = self.capture.last_error() or ""
        return "permission" not in error.lower() and "recording" not in error.lower()

    def shutdown(self) -> dict[str, str]:
        report = self.coordinator.shutdown()
        self.deadman.close()
        return report


def _launcher_identity() -> str:
    """Which application owns this process's permissions.

    On macOS a permission is granted to the *launching* application - Terminal,
    iTerm, or a packaged app - not to Python. Naming it is the difference
    between "grant permissions" and an instruction someone can follow.
    """
    bundle = os.environ.get("__CFBundleIdentifier")  # noqa: SIM112 - Apple spells it so
    if bundle:
        return bundle.rsplit(".", 1)[-1].replace("-", " ").title()
    return os.path.basename(sys.executable)


def build_application(profile_id: str = "green_arrow_v1") -> Application:
    paths = resolve_app_paths().ensure()
    port = create_platform_port()
    deadman = DeadmanClient(config=AuthorityConfig())
    reports: queue.Queue[str] = queue.Queue(maxsize=32)
    preview: LatestSlot[Any] = LatestSlot()

    registry = EvidenceRegistry("pending")
    guard = ViewportGuard(port)
    capture = CaptureService(guard, registry)
    events = EventLog()

    def capture_age_s() -> float | None:
        return capture.latest_age_s()

    #: Set once the coordinator exists. The authority is built first because
    #: the coordinator needs it, so the fault path is closed here rather than
    #: at construction - a late binding, but a total one: every fault the
    #: authority raises reaches the mode owner.
    fault_sink: dict[str, Any] = {}

    def on_safety_fault(fault: SafetyFault) -> None:
        """A terminal authority fault. Recorded *and* submitted.

        It used to only be recorded. The authority's watchdog would release
        every edge and disarm the movement actuator, and the coordinator - the
        one object that owns ``RunMode`` - was never told, so the dashboard
        went on saying LIVE over a runtime whose actuator was stopped and whose
        next ``apply`` could only answer "the navigator is stopped". A zombie
        Live is worse than a stop, because the person watching it has no reason
        to press anything.

        Submitted, not called: the coordinator owns its own thread and its
        queue puts safety ahead of every other intent, so this returns
        immediately and cannot deadlock the watchdog behind a mode transition.
        """
        events.add(
            "safety.fault",
            f"{fault.kind.name} gen={fault.generation} {' '.join(fault.evidence)}",
        )
        plain.say(
            Topic.STATE,
            Verdict.FAIL,
            f"Safety fault {fault.kind.name.replace('_', ' ').lower()}: "
            f"{' '.join(fault.evidence) or 'no detail'}. Everything is released.",
        )
        sink = fault_sink.get("submit")
        if sink is not None:
            with contextlib.suppress(Exception):
                sink(fault)

    #: The readable running commentary. Created here, before anything that

    #: might want to write to it, and handed to the two places that know

    #: things a person needs: the actuator (what is being pressed) and the

    #: coordinator (setup, the chord, and every refusal).

    plain = PlainLog()

    def narrate(verdict: str, text: str) -> None:
        """One line from the actuator. An OS edge gets its own topic.

        ``Topic.INPUT`` is not a per-frame topic, so a key-down line can never
        be evicted by the frame telemetry around it - which is what a person
        reading "why did nothing move" is looking for first.
        """
        topic = Topic.INPUT if verdict == "input" else Topic.FORWARD
        plain.say(topic, Verdict(verdict), text)

    authority = InputAuthority(
        port,
        deadman=deadman,
        health=HealthSources(
            focus=port.focus_state,
            client_rect=lambda: guard.geometry if guard.geometry.valid else None,
            capture_age_s=capture_age_s,
        ),
        config=AuthorityConfig(),
        on_safety_fault=on_safety_fault,
        narrate=narrate,
    )
    registry = EvidenceRegistry(authority.run_id, on_token=authority.register_evidence)
    capture = CaptureService(
        guard, registry, source_factory=port.create_capture_source, on_frame=preview.publish
    )

    library = load_profiles()
    profiles = ProfileAuthority(library, profile_id)
    pipeline = PerceptionPipeline(segmenter=ArrowSegmenter(profiles.active), profiles=profiles)
    # A separate instance, not a second reference to `pipeline`: the tracker
    # and direction estimator inside PerceptionPipeline carry state across
    # calls and are not locked, so a second thread calling into the one
    # Shadow/Live update would race it (D-089).
    degree_pipeline = PerceptionPipeline(
        segmenter=ArrowSegmenter(profiles.active), profiles=profiles
    )

    # No on_update callback: this module imports no UI (see the module
    # docstring), so it cannot own a widget. The dashboard instead polls
    # `degree_monitor.current()`/`armed_count()` from its own Tk-scheduled
    # timer to drive the small degree-readout window (D-091).
    degree_monitor = DegreeMonitor(frames=capture, primary=pipeline, mirror=degree_pipeline)
    turn_cache = TurnResponseCache(paths.config / "turn-response.json")

    state: dict[str, Any] = {
        "capabilities": NavigationCapabilities.observing(
            os_name=current_platform_name(), profile_id=profiles.active_id
        )
    }

    def capabilities_factory() -> NavigationCapabilities:
        current: NavigationCapabilities = state["capabilities"]
        return replace(current, profile_id=profiles.active_id)

    def pipeline_factory() -> PerceptionPipeline:
        return pipeline

    def report(message: str) -> None:
        # Drop-oldest is fine here: this is a diagnostic read-out, not evidence.
        with contextlib.suppress(queue.Full):
            reports.put_nowait(message)

    def control_fingerprint() -> ControlFingerprint:
        geometry = guard.geometry
        return ControlFingerprint(
            os_name=current_platform_name(),
            backend="unset",
            client_fingerprint=f"roblox@{geometry.canonical_px[0]}x{geometry.canonical_px[1]}",
            camera_sensitivity="unknown",
            control_mode="shift_lock",
            viewport_identity=geometry.identity(),
            profile_id=profiles.active_id,
            profile_revision=profiles.revision,
            supported_min_fps=SteeringLimits().min_processed_fps,
        )

    setup_port = EngineSetupPort(
        port=port,
        guard=guard,
        capture=capture,
        pipeline=pipeline,
        profiles=profiles,
        authority=authority,
    )
    probe = shift_lock_probe(port.cursor_client_px)

    def make_setup(
        cancelled: Callable[[], bool], publish: Callable[[SetupProgress], None]
    ) -> AutomaticSetup:
        return AutomaticSetup(
            setup_port,
            config=SetupConfig(),
            publish=publish,
            cancelled=cancelled,
            candidates=tuple(p.profile_id for p in library.selectable()),
        )

    def run_setup(
        cancelled: Callable[[], bool], publish: Callable[[SetupProgress], None]
    ) -> SetupProgress:
        machine = make_setup(cancelled, publish)
        with setup_port.consuming():
            progress = machine.run_observation()
        if progress.ok:
            reference = machine.reference
            state["capabilities"] = replace(
                capabilities_factory(),
                reference_ok=True,
                profile_id=profiles.active_id,
            )
            if reference is not None:
                pipeline.reference = replace(
                    pipeline.reference, measured_jitter_deg=reference.jitter_deg
                )
        else:
            state["capabilities"] = replace(capabilities_factory(), reference_ok=False)
        return progress

    def remember(response: TurnResponse) -> None:
        state["capabilities"] = replace(
            capabilities_factory(), control_mode_ok=True, turn_response=response
        )
        turn_cache.save(response)

    # Bound late: the coordinator does not exist yet, and the prologue needs
    # somewhere to publish. One cell rather than a forward reference, so the
    # wiring stays readable in the order it is built.
    setup_sink: list[Callable[[SetupProgress], None]] = []

    def coordinator_publish(progress: SetupProgress) -> None:
        for sink in setup_sink:
            sink(progress)

    prologue = make_live_prologue(
        fingerprint_factory=control_fingerprint,
        control_mode_probe=probe,
        capabilities_factory=capabilities_factory,
        # Published, not discarded. The prologue used to run its stages into a
        # sink that dropped them, so the thirty seconds Live spent failing to
        # characterize a turn looked from the dashboard exactly like thirty
        # seconds of navigating.
        setup_factory=lambda cancelled: make_setup(cancelled, coordinator_publish),
        prior_factory=turn_cache.load,
        on_measured=remember,
    )

    workers: dict[IntentType, WorkerFactory] = {
        IntentType.START_SHADOW: make_shadow_worker(pipeline_factory, capabilities_factory),
        IntentType.START_LIVE: make_live_worker(
            pipeline_factory, capabilities_factory, prologue=prologue
        ),
        IntentType.RESET_CHARACTER: _service_worker("reset"),
        IntentType.PAN_SWAP_TEST: _service_worker("pan_swap"),
        IntentType.DIG_LOOP: _service_worker("dig_loop"),
        IntentType.WIGGLE_TEST: _wiggle_worker(degree_monitor),
        IntentType.DEGREE_MONITOR: _degree_monitor_worker(degree_monitor),
        IntentType.PIXEL_INFO: _pixel_info_worker(port, report),
    }
    coordinator = RuntimeCoordinator(
        authority=authority,
        guard=guard,
        capture=capture,
        registry=registry,
        workers=workers,
        config=CoordinatorConfig(),
        events=events,
        plain=plain,
        paths=paths,
        pipeline_provider=lambda: pipeline,
        profiles=profiles,
        setup_runner=run_setup,
        cursor_probe=port.cursor_client_px,
        key_state_probe=port.key_state,
    )
    setup_sink.append(coordinator._publish_setup)
    # Close the safety loop. Until this line a terminal fault released every
    # edge and left the coordinator believing it was still in Live.
    fault_sink["submit"] = coordinator.submit_fault

    application = Application(
        port=port,
        guard=guard,
        registry=registry,
        capture=capture,
        authority=authority,
        coordinator=coordinator,
        deadman=deadman,
        preview=preview,
        reports=reports,
        paths=paths,
        pipeline=pipeline,
        degree_monitor=degree_monitor,
        profiles=profiles,
        turn_cache=turn_cache,
        plain=plain,
        capabilities_provider=capabilities_factory,
        control_fingerprint=control_fingerprint,
        control_mode_probe=probe,
    )
    return application
