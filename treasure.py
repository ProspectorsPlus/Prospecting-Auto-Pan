#!/usr/bin/env python3
"""Treasure Navigator entry point.

    python treasure.py                launch the dashboard
    python treasure.py --deadman      run the release-only helper (internal)
    python treasure.py --self-test    import and contract check, emits no input
    python treasure.py --smoke-test   packaging smoke test, emits no input
    python treasure.py --calibrate    read client-relative pixels under the cursor
    python treasure.py --capture-probe  measure capture cost, read-only
    python treasure.py --replay DIR   replay a recorded session, emits no input
    python treasure.py --detector-report  stratified detector metrics, no input
    python treasure.py --soak MINUTES bounded pipeline soak, no input

``--deadman`` is dispatched **before** Tk, OpenCV, capture, or engine code is
imported (plan 4.5), so the helper stays small and starts even if the heavy
graphics stack is broken. Everything else imports normally.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _run_deadman() -> int:
    import deadman

    return deadman.main(sys.argv[1:])


def _run_self_test() -> int:
    """Import every module and assert the invariants that need no OS access."""
    from prospector_engine import __version__, contracts, engine, motion, navigation, vision
    from prospector_engine.capture import EvidenceRegistry
    from prospector_engine.contracts import (
        EvidenceStatus,
        EvidenceToken,
        InputKey,
        InputVocabulary,
    )
    from prospector_engine.telemetry import resolve_app_paths
    from prospector_engine.vision import load_profiles

    checks: list[tuple[str, bool, str]] = []

    vocabulary = InputVocabulary()
    checks.append(
        (
            "release floor covers every key that can be pressed",
            set(vocabulary.keys) == set(InputKey),
            f"{len(vocabulary.keys)} keys, {len(vocabulary.buttons)} buttons",
        )
    )

    forged = False
    try:
        EvidenceToken("run", 1, 1, 0.0, 1.0, (0, 0, 0, 0, "d"), object())  # type: ignore[arg-type]
        forged = True
    except PermissionError:
        pass
    checks.append(("evidence tokens cannot be minted by feature code", not forged, ""))

    registry = EvidenceRegistry("self-test")
    checks.append(("capture registry can mint a token", registry is not None, ""))

    library = load_profiles()
    checks.append(
        (
            "no arrow profile is auto-selectable before E-PROF",
            not library.validated(),
            f"{len(library)} profiles, all {EvidenceStatus.PENDING.value}",
        )
    )
    checks.append(
        (
            "migrated dig/pan pixels are marked pending reverification",
            engine.DEFAULT_PIXELS.status is EvidenceStatus.PENDING,
            str(engine.DEFAULT_PIXELS.provenance),
        )
    )
    gates = navigation.NavigationGates(os_name=sys.platform, profile_id="yellow_map_v0")
    checks.append(
        (
            "live steering is disabled until its gates pass",
            not gates.steering_enabled and not gates.recovery_enabled,
            "pending: " + ",".join(gates.blocking_reasons()),
        )
    )
    checks.append(
        (
            "motion estimator candidates are all present",
            set(motion.MOTION_ESTIMATORS) >= {"lk_affine", "phase_correlation"},
            ",".join(sorted(motion.MOTION_ESTIMATORS)),
        )
    )
    checks.append(
        (
            "angle wrapping is correct across the +-180 seam",
            vision.wrap_deg(190.0) == -170.0 and vision.wrap_deg(-190.0) == 170.0,
            "",
        )
    )
    try:
        paths = resolve_app_paths()
        checks.append(("writable data root resolves", True, str(paths.root)))
    except Exception as exc:
        checks.append(("writable data root resolves", False, repr(exc)))

    python_version = sys.version.split()[0]
    print(
        f"Treasure Navigator {__version__} self-test ({sys.platform}, python {python_version})"
    )
    failures = 0
    for name, ok, detail in checks:
        marker = "PASS" if ok else "FAIL"
        failures += 0 if ok else 1
        print(f"  [{marker}] {name}" + (f"  -- {detail}" if detail else ""))
    print(f"  contracts module exports {len(contracts.__all__)} names")
    print(
        "\nNo OS input was emitted. Native macOS/Windows gates remain PENDING; see STATUS.md."
    )
    return 1 if failures else 0


def _run_smoke_test() -> int:
    """Packaging smoke test: resources load and the helper dispatches."""
    import json
    import subprocess
    import tempfile

    from prospector_engine.vision import load_profiles

    problems: list[str] = []
    try:
        library = load_profiles()
        print(f"  profiles loaded from package data: {', '.join(library.ids())}")
    except Exception as exc:
        problems.append(f"profile package data: {exc!r}")

    with tempfile.TemporaryDirectory() as directory:
        sink = os.path.join(directory, "deadman.log")
        environment = dict(os.environ)
        environment["TREASURE_DEADMAN_TOKEN"] = "smoke"
        environment["TREASURE_DEADMAN_SINK"] = sink
        argv = (
            [sys.executable, "--deadman"]
            if getattr(sys, "frozen", False)
            else [sys.executable, os.path.join(_HERE, "treasure.py"), "--deadman"]
        )
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=environment,
            text=True,
            bufsize=1,
        )
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps({"op": "hello", "token": "smoke"}) + "\n")
        process.stdin.flush()
        reply = json.loads(process.stdout.readline())
        if not reply.get("ok"):
            problems.append(f"deadman handshake: {reply}")
        else:
            print(f"  deadman dispatched and answered (pid {reply.get('pid')})")
        process.stdin.write(json.dumps({"op": "shutdown", "token": "smoke"}) + "\n")
        process.stdin.flush()
        process.stdout.readline()
        process.wait(timeout=5)
        released: list[str] = []
        if os.path.exists(sink):
            with open(sink, encoding="utf-8") as handle:
                released = handle.read().splitlines()
        if not released:
            problems.append("deadman released nothing on shutdown")
        else:
            print(f"  deadman release floor covered {len(released)} targets (file sink)")

    for problem in problems:
        print(f"  [FAIL] {problem}")
    return 1 if problems else 0


def _run_replay(session_dir: str, profile_id: str = "yellow_map_v0") -> int:
    """Replay a recorded session through the real decision path.

    Emits **no** OS input: there is no authority and no port in this path at
    all. It is how a recorded route is re-examined after the fact, and how the
    same frames are shown to produce the same decisions (plan 15, phase 3).
    """
    from pathlib import Path

    from prospector_engine.contracts import CapturedFrame, monotonic_s
    from prospector_engine.geometry import (
        DisplayInfo,
        LogicalRect,
        ViewportGeometry,
        ViewportState,
        WindowIdentity,
    )
    from prospector_engine.navigation import NavigationGates, Navigator, PerceptionPipeline
    from prospector_engine.telemetry import read_session
    from prospector_engine.vision import ArrowSegmenter, load_profiles

    profile = load_profiles().get(profile_id)
    if profile is None:
        print(f"Unknown profile {profile_id!r}.")
        return 2
    gates = NavigationGates(os_name=sys.platform, profile_id=profile.profile_id)
    navigator = Navigator(gates=gates)
    pipeline = PerceptionPipeline(segmenter=ArrowSegmenter(profile))

    print(f"Replaying {session_dir} with profile {profile.profile_id} [{profile.status.value}]")
    print("  no input authority exists in this path; nothing can be emitted.\n")
    counts: dict[str, int] = {}
    frames = 0
    for record in read_session(Path(session_dir)):
        frames += 1
        height, width = record.bgr.shape[:2]
        client = LogicalRect(0.0, 0.0, float(width), float(height))
        geometry = ViewportGeometry(
            state=ViewportState.CANONICAL_VERIFIED,
            window=WindowIdentity(0, 0, "replay"),
            display=DisplayInfo("replay", client, 1.0),
            frame_logical=client,
            client_logical=client,
            canonical_px=(width, height),
            verified_at_s=record.captured_at_s,
            detail="replayed recording",
        )
        frame = CapturedFrame(
            sequence=record.sequence,
            captured_at_s=record.captured_at_s,
            completed_at_s=record.captured_at_s + record.duration_ms / 1000.0,
            duration_ms=record.duration_ms,
            geometry=geometry,
            bgr=record.bgr,
            duplicate=record.duplicate,
            backend="replay",
        )
        decision = navigator.decide(
            pipeline.observe(frame, map_id="replay", approach_valid=False),
            generation=1,
            # Replay uses recorded time, not wall time, so a slow replay cannot
            # make every frame look stale.
            now_s=record.captured_at_s,
        )
        key = f"{decision.phase.name}: {decision.reason}"
        counts[key] = counts.get(key, 0) + 1

    if not frames:
        print("  no frames found (empty or unreadable session).")
        return 1
    print(f"  {frames} frames replayed at {monotonic_s():.0f}s monotonic\n")
    for key, count in sorted(counts.items(), key=lambda item: -item[1]):
        print(f"  {count:6d}  {key}")
    print("\nDecision counts only. Metrics against a frozen evaluation spec are")
    print(
        "PENDING - no gate has been frozen (prospector_engine/profiles/evaluation_spec.json)."
    )
    return 0


def _run_soak(minutes: float = 10.0) -> int:
    """Run the full pipeline against a synthetic source and watch what grows.

    Emits no OS input and needs no Roblox: the point is to hold the capture,
    perception and telemetry machinery under load long enough that a leak
    becomes visible, which a unit test cannot do.
    """
    import gc
    import threading
    import time

    from prospector_engine.capture import (
        CaptureConfig,
        CaptureService,
        EvidenceRegistry,
        ViewportGuard,
        _ProcessUsage,
    )
    from prospector_engine.contracts import PerformanceTier
    from prospector_engine.navigation import NavigationGates, Navigator, PerceptionPipeline
    from prospector_engine.vision import ArrowSegmenter, load_profiles

    sys.path.insert(0, _HERE)
    try:
        from tests.arrow_fixtures import render_scene
        from tests.fakes import FakeCaptureSource, FakePlatformPort, VirtualClock, make_geometry
    except ImportError:
        print("The soak needs the test fixtures; run from a source checkout.")
        return 2

    profile = load_profiles().get("green_arrow_v1")
    if profile is None:
        return 2
    port = FakePlatformPort(VirtualClock(), geometry=make_geometry(size=(1280.0, 720.0)))
    guard = ViewportGuard(port)
    guard.connect()

    frames = [
        render_scene(heading_deg=float(angle), terrain="grass", scale_px=100.0, seed=angle).bgr
        for angle in range(0, 360, 30)
    ]
    source = FakeCaptureSource(frames=frames)
    service = CaptureService(
        guard,
        EvidenceRegistry("soak"),
        config=CaptureConfig(start_tier=PerformanceTier.STANDARD),
        source_factory=lambda: source,
    )
    pipeline = PerceptionPipeline(segmenter=ArrowSegmenter(profile))
    navigator = Navigator(gates=NavigationGates(os_name=sys.platform, profile_id="soak"))
    usage = _ProcessUsage()

    threads_before = threading.active_count()
    if not service.start():
        print(f"capture failed to start: {service.last_error()}")
        return 1

    print(f"Soaking for {minutes:g} minutes. No OS input is emitted; no window is touched.")
    header = f"{'elapsed':>8} {'unique':>7} {'processed':>10} {'rss MB':>8}"
    print(f"{header} {'peak':>7} {'cpu%':>6}")
    started = time.monotonic()
    deadline = started + minutes * 60.0
    interval = min(30.0, max(2.0, minutes * 60.0 / 8.0))
    next_report = started + interval
    baseline_rss = 0.0
    samples: list[tuple[float, float]] = []
    last_sequence = 0
    processed = 0
    try:
        while time.monotonic() < deadline:
            envelope = service.wait_for_new(last_sequence, 0.25)
            if envelope is None:
                continue
            last_sequence = envelope.frame.sequence
            result = pipeline.analyze(envelope.frame, map_id="soak", approach_valid=False)
            navigator.decide(result.inputs, generation=1, now_s=time.monotonic())
            service.note_perception_ms(result.perception_ms)
            service.note_decision_ms(0.05)
            processed += 1
            now = time.monotonic()
            if now >= next_report:
                next_report = now + interval
                gc.collect()
                sample = usage.sample()
                metrics = service.metrics()
                elapsed = now - started
                if baseline_rss == 0.0 and elapsed >= interval:
                    baseline_rss = sample.rss_current_mb
                if baseline_rss:
                    samples.append((elapsed, sample.rss_current_mb))
                print(
                    f"{elapsed:7.0f}s {metrics.unique_fps:7.1f} {metrics.processed_fps:10.1f} "
                    f"{sample.rss_current_mb:8.1f} {sample.rss_peak_mb:7.1f} "
                    f"{sample.cpu_percent:6.0f}"
                )
    finally:
        stopped = service.stop(3.0)

    gc.collect()
    threads_after = threading.active_count()
    slope = 0.0
    if len(samples) >= 2:
        span_minutes = (samples[-1][0] - samples[0][0]) / 60.0
        slope = (samples[-1][1] - samples[0][1]) / max(span_minutes, 1e-6)

    print()
    print(f"  frames processed   {processed}")
    print(f"  threads            {threads_before} before, {threads_after} after")
    print(f"  capture shutdown   {'clean' if stopped else 'SURVIVOR'}")
    print(f"  buffer pool live   {service._pool.live} of {service._pool.capacity}")
    print(f"  RSS slope          {slope:+.2f} MB/min (provisional target: under 1.0)")
    ok = stopped and threads_after <= threads_before + 1 and slope < 1.0
    print(f"\n  {'PASS' if ok else 'FAIL'} - this is a local soak, not E-PERF.")
    return 0 if ok else 1


def _run_detector_report(profile_id: str = "green_arrow_v1") -> int:
    """Measure the detector on rendered stress frames. Emits no input.

    Rendered frames are **training stress, never a held-out split** (plan 7.2),
    so this can never pass E-PROF or E-DIR-E2E. It exists so a change to the
    detector can be judged against the same conditions every time, and so the
    numbers in STATUS.md can be regenerated by anybody.
    """
    from dataclasses import asdict

    import numpy as np

    from prospector_engine.arrow import ArrowDetector, DetectorConfig, DirectionEstimator
    from prospector_engine.contracts import CapturedFrame, freeze_array
    from prospector_engine.evaluation import DatasetSplit, LabelledFrame, evaluate
    from prospector_engine.geometry import (
        DisplayInfo,
        LogicalRect,
        ViewportGeometry,
        ViewportState,
        WindowIdentity,
    )
    from prospector_engine.vision import load_profiles

    sys.path.insert(0, _HERE)
    try:
        from tests.arrow_fixtures import render_scene
    except ImportError:
        print("The detector report needs the test fixtures; run from a source checkout.")
        return 2

    profile = load_profiles().get(profile_id)
    if profile is None:
        print(f"Unknown profile {profile_id!r}.")
        return 2

    client = LogicalRect(0.0, 0.0, 1280.0, 720.0)
    geometry = ViewportGeometry(
        state=ViewportState.CANONICAL_VERIFIED,
        window=WindowIdentity(0, 0, "synthetic"),
        display=DisplayInfo("synthetic", client, 1.0),
        frame_logical=client,
        client_logical=client,
        canonical_px=(1280, 720),
        detail="rendered stress frame",
    )
    strata: dict[str, dict[str, object]] = {
        "day-grass": {"terrain": "grass", "scale_px": 100.0},
        "day-dirt": {"terrain": "dirt", "scale_px": 100.0},
        "water": {"terrain": "water", "scale_px": 100.0},
        "pale-terrain": {"terrain": "pale", "scale_px": 100.0},
        "night-grass": {"terrain": "night_grass", "scale_px": 100.0},
        "small-arrow": {"terrain": "grass", "scale_px": 45.0},
        "large-arrow": {"terrain": "grass", "scale_px": 210.0},
        "foreshortened": {"terrain": "dirt", "scale_px": 130.0, "foreshorten": 0.5},
        "blurred": {"terrain": "grass", "scale_px": 100.0, "blur_px": 7},
        "translucent": {"terrain": "pale", "scale_px": 110.0, "alpha": 0.55},
        "dim": {"terrain": "grass", "scale_px": 100.0, "brightness": 0.5},
        "same-colour-clutter": {"terrain": "dirt", "scale_px": 100.0, "distractors": 5},
        "same-colour-occlusion": {"terrain": "dirt", "scale_px": 100.0, "occluders": 4},
    }

    def frame_of(scene: object, sequence: int) -> CapturedFrame:
        return CapturedFrame(
            sequence=sequence,
            captured_at_s=sequence * 0.01,
            completed_at_s=sequence * 0.01 + 0.002,
            duration_ms=2.0,
            geometry=geometry,
            bgr=freeze_array(np.ascontiguousarray(scene.bgr)),  # type: ignore[attr-defined]
            backend="synthetic",
        )

    labelled: list[LabelledFrame] = []
    episode = 0
    for name, options in strata.items():
        for _run in range(6):
            episode += 1
            for step, heading in enumerate(range(0, 360, 24)):
                scene = render_scene(
                    heading_deg=float(heading), seed=episode * 100 + step, **options
                )
                labelled.append(
                    LabelledFrame(
                        frame_of(scene, step + 1),
                        float(heading),
                        name,
                        session_id=f"synthetic-{name}",
                        episode_id=f"ep{episode}",
                    )
                )
            for step in range(6):
                scene = render_scene(
                    heading_deg=0.0, seed=episode * 100 + 60 + step, arrow=False, **options
                )
                labelled.append(
                    LabelledFrame(
                        frame_of(scene, 100 + step),
                        None,
                        name,
                        session_id=f"synthetic-{name}",
                        episode_id=f"ep{episode}",
                        arrow_present=False,
                    )
                )

    config = DetectorConfig()
    detector = ArrowDetector(profile, config)
    estimator = DirectionEstimator(config)

    def predict(frame: CapturedFrame) -> tuple[float | None, bool]:
        arrow, hypotheses = detector.analyze(frame)
        if not arrow.valid:
            return (None, False)
        accepted = next((h for h in hypotheses if h.accepted), None)
        observation = estimator.estimate(
            accepted.features if accepted is not None else None,
            anchor_px=(640.0, 430.0),
            forward_deg=0.0,
            arrow_confidence=arrow.confidence,
        ).observation
        if not observation.valid or observation.error_deg is None:
            return (None, False)
        return (observation.error_deg, True)

    report = evaluate(
        DatasetSplit("synthetic-stress", tuple(labelled)),
        predict,
        detector_config={key: str(value) for key, value in asdict(config).items()},
        notes=(
            "Rendered frames fitted to the owner's measured crops. Training "
            "stress only: plan 7.2 forbids synthetic data in a held-out split, "
            "so nothing here can pass E-PROF or E-DIR-E2E.",
        ),
    )
    print(f"Detector report for {profile.profile_id} [{profile.status.value}]\n")
    print(report.describe())
    return 0


def _run_capture_probe(seconds: float = 4.0) -> int:
    """Measure what the real pipeline sustains. Read-only.

    It captures the Roblox window through the production source and consumes
    every frame the way the navigator does. No window is moved and no input is
    sent. The numbers feed E-PERF planning; a probe is not the gate (plan 7.4).
    """
    from prospector_engine.capture import (
        CaptureConfig,
        CaptureService,
        EvidenceRegistry,
        ViewportGuard,
    )
    from prospector_engine.contracts import PerformanceTier, monotonic_s
    from prospector_engine.ports import create_platform_port

    port = create_platform_port()
    guard = ViewportGuard(port)
    geometry = guard.adopt_current()
    print("Capture pipeline probe (read-only; no window is moved, no input is sent)")
    print(f"  viewport: {geometry.describe()}")
    if not geometry.valid:
        print("  Roblox client not available; nothing to measure.")
        return 1

    for tier in (PerformanceTier.STANDARD, PerformanceTier.HIGH, PerformanceTier.MAXIMUM):
        service = CaptureService(
            guard,
            EvidenceRegistry("probe"),
            config=CaptureConfig(start_tier=tier, max_tier=tier),
            source_factory=port.create_capture_source,
        )
        if not service.start():
            print(f"  {tier.fps:>3d} Hz request: source failed: {service.last_error()}")
            continue
        consumed, last = 0, 0
        deadline = monotonic_s() + seconds
        while monotonic_s() < deadline:
            envelope = service.wait_for_new(last, 0.25)
            if envelope is None:
                continue
            last = envelope.frame.sequence
            consumed += 1
            service.note_perception_ms(0.0)
        metrics = service.metrics()
        service.stop()
        budget = "within" if metrics.end_to_end.p95_ms <= 40.0 else "OVER"
        print(
            f"  {tier.fps:>3d} Hz request: {consumed / seconds:6.1f} unique fps consumed  "
            f"capture p50 {metrics.capture.p50_ms:4.1f} p95 {metrics.capture.p95_ms:4.1f} ms  "
            f"age {0.0 if metrics.frame_age_ms is None else metrics.frame_age_ms:4.1f} ms  "
            f"dup {metrics.duplicate_frames.session_total}  "
            f"superseded {metrics.superseded_frames.session_total}  "
            f"cpu {metrics.cpu_percent:3.0f}%  rss {metrics.rss_current_mb:3.0f} MB "
            f"(peak {metrics.rss_peak_mb:3.0f})  [{budget} 40 ms]"
        )
    print("\nBackend:", port.create_capture_source().name)
    print("These are capture-and-consume costs on this machine. E-PERF, which also")
    print("covers perception, control, and Stop latency, remains PENDING (STATUS.md).")
    return 0


def _run_calibrate() -> int:
    """Read the client-relative pixel under the cursor.

    Reports in the **canonical client basis** so the value pastes straight into
    a ``TreasurePixels`` field. It refuses when the Roblox client rect cannot be
    verified, because there is nothing to measure against.
    """
    import time

    from prospector_engine.capture import CaptureService, EvidenceRegistry, ViewportGuard
    from prospector_engine.engine import DEFAULT_PIXELS
    from prospector_engine.ports import create_platform_port

    port = create_platform_port()
    guard = ViewportGuard(port)
    guard.adopt_current()
    capture = CaptureService(
        guard, EvidenceRegistry("calibrate"), source_factory=port.create_capture_source
    )
    capture.start()
    print("CALIBRATE - hover a target inside the Roblox client, Ctrl+C to quit.")
    print("  PIXEL is reported in CANONICAL CLIENT coordinates (physical px from the")
    print("  client area's top-left), which is the basis TreasurePixels uses.")
    try:
        while True:
            envelope = capture.latest()
            cursor = port.cursor_client_px()
            if envelope is None or cursor is None:
                print(
                    "\rRoblox client not found or cursor outside it...            ",
                    end="",
                    flush=True,
                )
                time.sleep(0.1)
                continue
            r, g, b = envelope.frame.sample_mean_rgb(cursor, DEFAULT_PIXELS.sample_box_px)
            geometry = envelope.frame.geometry
            note = "" if geometry.is_canonical else "  [NON-CANONICAL: do not bake this]"
            print(
                f"\rPIXEL=({cursor[0]:>5},{cursor[1]:>5})  RGB=({int(r):>3},{int(g):>3},"
                f"{int(b):>3})  {geometry.state.value}{note}   ",
                end="",
                flush=True,
            )
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\nDone.")
    finally:
        capture.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    if "--deadman" in arguments:
        return _run_deadman()
    if "--self-test" in arguments:
        return _run_self_test()
    if "--smoke-test" in arguments:
        return _run_smoke_test()
    if "--replay" in arguments:
        index = arguments.index("--replay")
        if index + 1 >= len(arguments):
            print("--replay needs a recorded session directory")
            return 2
        return _run_replay(arguments[index + 1])
    if "--capture-probe" in arguments:
        return _run_capture_probe()
    if "--detector-report" in arguments:
        index = arguments.index("--detector-report")
        profile = (
            arguments[index + 1]
            if index + 1 < len(arguments) and not arguments[index + 1].startswith("-")
            else "green_arrow_v1"
        )
        return _run_detector_report(profile)
    if "--soak" in arguments:
        index = arguments.index("--soak")
        minutes = (
            float(arguments[index + 1])
            if index + 1 < len(arguments) and not arguments[index + 1].startswith("-")
            else 10.0
        )
        return _run_soak(minutes)
    if "--calibrate" in arguments:
        return _run_calibrate()
    from treasure_gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
