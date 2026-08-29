#!/usr/bin/env python3
"""Treasure Navigator entry point.

    python treasure.py                launch the dashboard
    python treasure.py --deadman      run the release-only helper (internal)
    python treasure.py --self-test    import and contract check, emits no input
    python treasure.py --smoke-test   packaging smoke test, emits no input
    python treasure.py --calibrate    read client-relative pixels under the cursor
    python treasure.py --capture-probe  measure capture cost, read-only
    python treasure.py --setup-probe    run automatic setup, no input sent
    python treasure.py --replay DIR   replay a recorded session, emits no input
    python treasure.py --detector-report [PROFILE] [--corpus DIR] [--json PATH]
                                      detector metrics on rendered stress frames,
                                      or on a real-frame corpus; no input
    python treasure.py --soak MINUTES bounded pipeline soak, no input
    python treasure.py --shadow-bench SECONDS [--json PATH]
                                      native capture and headless perception
                                      against the real Roblox window; no input

Every offline mode is **mutually exclusive and bounded**: it never builds the
dashboard, never starts the input authority or the deadman helper, writes
its report, and exits with a meaningful status. ``--deadman`` is dispatched
**before** Tk, OpenCV, capture, or engine code is imported (plan 4.5), so the
helper stays small and starts even if the heavy graphics stack is broken.
Everything else imports normally.

Set ``TREASURE_LIFECYCLE_PROBE=1`` to print, on exit, which GUI modules were
loaded, how many threads survive, and how many child processes exist - the
subprocess tests assert on that line.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # imported for types only; the offline modes stay import-light
    from prospector_engine.geometry import ViewportGeometry
    from prospector_engine.ports import PlatformPort

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
            "no arrow profile claims a passed offline E-PROF gate",
            not library.validated(),
            f"{len(library)} profiles, all {EvidenceStatus.PENDING.value}",
        )
    )
    checks.append(
        (
            "the runtime classifier has candidates to choose between",
            len(library.selectable()) >= 2,
            ", ".join(p.profile_id for p in library.selectable()),
        )
    )
    checks.append(
        (
            "migrated dig/pan pixels are marked pending reverification",
            engine.DEFAULT_PIXELS.status is EvidenceStatus.PENDING,
            str(engine.DEFAULT_PIXELS.provenance),
        )
    )
    fresh = navigation.NavigationCapabilities.observing(
        os_name=sys.platform, profile_id="yellow_map_v1"
    )
    checks.append(
        (
            "a fresh run cannot steer until it has measured something",
            not fresh.steering_enabled and not fresh.recovery_enabled,
            "missing: " + ", ".join(fresh.blocking_reasons()),
        )
    )
    checks.append(
        (
            "the release floor covers the turn keys",
            {contracts.InputKey.LEFT, contracts.InputKey.RIGHT}
            <= set(contracts.InputVocabulary().keys),
            f"{len(contracts.InputVocabulary().keys)} keys in the vocabulary",
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
        "\nNo OS input was emitted. Native macOS/Windows verification is tracked in STATUS.md."
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
    from prospector_engine.navigation import (
        NavigationCapabilities,
        Navigator,
        PerceptionPipeline,
    )
    from prospector_engine.telemetry import read_session
    from prospector_engine.vision import ArrowSegmenter, load_profiles

    profile = load_profiles().get(profile_id)
    if profile is None:
        print(f"Unknown profile {profile_id!r}.")
        return 2
    navigator = Navigator(
        capabilities=NavigationCapabilities.observing(
            os_name=sys.platform, profile_id=profile.profile_id
        )
    )
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
    from prospector_engine.navigation import (
        NavigationCapabilities,
        Navigator,
        PerceptionPipeline,
    )
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
    navigator = Navigator(
        capabilities=NavigationCapabilities.observing(os_name=sys.platform, profile_id="soak")
    )
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


def _run_corpus_report(corpus_dir: str, json_path: str | None) -> int:
    """Measure the detector on a labelled real-frame corpus. Emits no input.

    Both splits are reported; ``tune`` is what the detector was chosen on and
    ``eval`` was only ever read. The JSON carries every count next to every
    rate, the corpus provenance and the detector configuration, so a number
    can always be traced back to the frames that produced it.
    """
    import json
    import time
    from dataclasses import asdict

    from prospector_engine.arrow import ArrowDetector, DetectorConfig
    from prospector_engine.contracts import CapturedFrame
    from prospector_engine.corpus import (
        FramePrediction,
        by_stratum,
        describe,
        evaluate_corpus,
        heading_from_axis,
        load_corpus,
    )
    from prospector_engine.navigation import PerceptionPipeline
    from prospector_engine.vision import ArrowSegmenter, load_profiles

    try:
        corpus = load_corpus(corpus_dir)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Cannot load corpus {corpus_dir!r}: {exc}")
        return 2
    profile = load_profiles().get(corpus.profile_id)
    if profile is None:
        print(f"The corpus names profile {corpus.profile_id!r}, which is not bundled.")
        return 2
    config = DetectorConfig()
    holder: dict[str, PerceptionPipeline] = {}
    costs: list[float] = []

    def reset() -> None:
        holder["pipeline"] = PerceptionPipeline(
            segmenter=ArrowSegmenter(profile), detector=ArrowDetector(profile, config)
        )

    def predict(frame: CapturedFrame) -> FramePrediction:
        started = time.perf_counter()
        result = holder["pipeline"].analyze(frame, map_id="corpus", approach_valid=False)
        costs.append((time.perf_counter() - started) * 1000.0)
        arrow = result.inputs.arrow
        decision = result.timing.tracking_decision if result.timing is not None else ""
        return FramePrediction(
            accepted=arrow.valid,
            bbox_px=arrow.bbox_px,
            heading_deg=heading_from_axis(arrow.tip_px, arrow.tail_px) if arrow.valid else None,
            track_id=arrow.track_id,
            decision=decision,
        )

    document: dict[str, object] = {
        "corpus": str(corpus.root),
        "profile_id": corpus.profile_id,
        "provenance": corpus.provenance,
        "detector_config": {key: str(value) for key, value in asdict(config).items()},
        "splits": {},
    }
    print(f"Real-frame corpus report: {corpus.root} (profile {corpus.profile_id})\n")
    for split in ("tune", "eval"):
        costs.clear()
        results = evaluate_corpus(corpus, predict, split=split, reset=reset)  # type: ignore[arg-type]
        ordered = sorted(costs)
        timing = {
            "frames": len(ordered),
            "p50_ms": ordered[len(ordered) // 2] if ordered else 0.0,
            "p95_ms": ordered[int(0.95 * (len(ordered) - 1))] if ordered else 0.0,
            "max_ms": ordered[-1] if ordered else 0.0,
        }
        document["splits"][split] = {  # type: ignore[index]
            "sequences": {k: v.as_dict() for k, v in results.items() if k != "__all__"},
            "strata": {k: v.as_dict() for k, v in by_stratum(results).items()},
            "total": results["__all__"].as_dict(),
            "perception_ms": timing,
        }
        print(f"== split: {split} ==")
        print(describe({k: v for k, v in results.items() if k != "__all__"}))
        print(describe(by_stratum(results)))
        print(describe({"__all__": results["__all__"]}))
        print(
            f"perception ms p50 {timing['p50_ms']:.1f} p95 {timing['p95_ms']:.1f} "
            f"max {timing['max_ms']:.1f}\n"
        )
    print(
        "Regression evidence on real frames from one session. It is not E-PROF: "
        "one map, one machine, one lighting pass per stratum, no held-out session."
    )
    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(json.dumps(document, indent=1), encoding="utf-8")
        print(f"JSON written to {json_path}")
    return 0


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
    last_episode: list[str | None] = [None]
    original_predict_frames = {id(item.frame): item.episode_id for item in labelled}

    def predict(frame: CapturedFrame) -> tuple[float | None, bool]:
        episode = original_predict_frames.get(id(frame))
        if episode != last_episode[0]:
            # Identities never cross an episode cut.
            detector.reset()
            last_episode[0] = episode
        arrow, hypotheses = detector.analyze(frame)
        if not arrow.valid:
            return (None, False)
        selected = next((h for h in hypotheses if h.state == "selected"), None)
        observation = estimator.estimate(
            selected.features if selected is not None else None,
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


def _percentile_of(ordered: list[float]) -> Callable[[float], float]:
    """A percentile reader over an already-sorted list; empty reads as zero."""

    def at(fraction: float) -> float:
        if not ordered:
            return 0.0
        return ordered[min(len(ordered) - 1, int(fraction * (len(ordered) - 1)))]

    return at


def _run_shadow_bench(seconds: float = 20.0, json_path: str | None = None) -> int:
    """Native capture and headless perception against the real Roblox window.

    Two configurations, each for ``seconds``: **capture** consumes every frame
    and does nothing with it; **perception** runs the production pipeline on
    every frame. No dashboard, no input authority, no deadman, no window is
    moved and no input is sent. The numbers are the same ones the dashboard
    shows, taken without the dashboard, which is what makes a regression in
    the perception path separable from a regression in the preview.
    """
    import json

    from prospector_engine.capture import (
        CaptureConfig,
        CaptureService,
        EvidenceRegistry,
        ViewportGuard,
        _ProcessUsage,
    )
    from prospector_engine.contracts import CadenceMode, monotonic_s
    from prospector_engine.navigation import PerceptionPipeline
    from prospector_engine.ports import create_platform_port
    from prospector_engine.trace import FrameTrace
    from prospector_engine.vision import ArrowSegmenter, load_profiles

    seconds = max(2.0, min(float(seconds), 120.0))
    port = create_platform_port()
    guard = ViewportGuard(port)
    geometry = guard.connect()
    print("Shadow bench (read-only; no window is moved, no input is sent, no dashboard)")
    print(f"  viewport: {geometry.describe()}")
    if not geometry.valid:
        print("  Roblox client not available; nothing to measure.")
        return 1
    profile = load_profiles().get("yellow_map_v1") or load_profiles().all()[0]
    report: dict[str, object] = {
        "seconds_per_configuration": seconds,
        "viewport": geometry.describe(),
        "profile_id": profile.profile_id,
        "configurations": {},
    }
    for name in ("capture", "perception"):
        service = CaptureService(
            guard,
            EvidenceRegistry(f"bench-{name}"),
            config=CaptureConfig(),
            source_factory=port.create_capture_source,
        )
        service.set_cadence_mode(CadenceMode.BALANCED)
        pipeline = PerceptionPipeline(segmenter=ArrowSegmenter(profile))
        usage = _ProcessUsage()
        if not service.start():
            print(f"  {name}: source failed: {service.last_error()}")
            continue
        usage.sample()
        latencies: list[float] = []
        started = monotonic_s()
        deadline = started + seconds
        last, consumed, unique_seen = 0, 0, 0
        try:
            while monotonic_s() < deadline:
                envelope = service.wait_for_new(last, 0.25)
                if envelope is None:
                    continue
                frame = envelope.frame
                if last and frame.sequence > last + 1:
                    service.note_dropped_observation(frame.sequence - last - 1)
                unique_seen += frame.sequence - last if last else 1
                last = frame.sequence
                picked_at_s = monotonic_s()
                timing = None
                if name == "perception":
                    result = pipeline.analyze(frame, map_id="bench", approach_valid=False)
                    service.note_perception_ms(result.perception_ms)
                    timing = result.timing
                else:
                    service.note_perception_ms(0.0)
                latency = (monotonic_s() - frame.captured_at_s) * 1000.0
                service.note_end_to_end_ms(latency)
                latencies.append(latency)
                consumed += 1
                if timing is not None:
                    service.trace.record(
                        FrameTrace(
                            frame_sequence=frame.sequence,
                            captured_at_s=frame.captured_at_s,
                            completed_at_s=frame.completed_at_s,
                            source_epoch=service.source_epoch,
                            cadence_hz=service.tier.fps,
                            capture_ms=frame.duration_ms,
                            scheduling_delay_ms=max(
                                0.0, (picked_at_s - frame.completed_at_s) * 1000.0
                            ),
                            perception=timing,
                            decision_ms=0.0,
                            capture_to_observation_ms=latency,
                            settling=service.settling,
                        )
                    )
        finally:
            elapsed = monotonic_s() - started
            sample = usage.sample()
            metrics = service.metrics()
            service.stop(3.0)
        ordered = sorted(latencies)
        at = _percentile_of(ordered)

        summary = {
            "consumed_fps": consumed / elapsed,
            "unique_fps_seen": unique_seen / elapsed,
            "processed_over_unique": consumed / max(1, unique_seen),
            "capture_to_observation_ms": {
                "p50": at(0.5),
                "p95": at(0.95),
                "p99": at(0.99),
                "max": at(1.0),
            },
            "tier_hz": metrics.tier.fps,
            "governor": metrics.governor.reason,
            "cpu_percent": sample.cpu_percent,
            "rss_mb": sample.rss_current_mb,
            "superseded": metrics.superseded_frames.session_total,
            "pool_exhausted": metrics.pool_exhausted.session_total,
            "backend": metrics.backend,
            "trace": service.trace.summary().describe(),
            "governor_transitions": [t.as_row() for t in service.trace.transitions()],
        }
        report["configurations"][name] = summary  # type: ignore[index]
        print(
            f"  {name:<10} {summary['consumed_fps']:6.1f} fps consumed of "
            f"{summary['unique_fps_seen']:6.1f} unique  latency p50 {at(0.5):5.1f} "
            f"p95 {at(0.95):5.1f} p99 {at(0.99):5.1f} max {at(1.0):6.1f} ms  "
            f"tier {metrics.tier.fps} Hz  "
            f"cpu {sample.cpu_percent:3.0f}%  rss {sample.rss_current_mb:4.0f} MB  "
            f"superseded {summary['superseded']}  pool-exhausted {summary['pool_exhausted']}"
        )
    for name, summary in report["configurations"].items():  # type: ignore[union-attr]
        for row in summary["governor_transitions"]:  # type: ignore[index]
            print(f"    [{name}] governor {row['from_hz']}->{row['to_hz']} Hz: {row['reason']}")
    print("\nBackend:", port.create_capture_source().name)
    print(
        "This is a native measurement of capture and headless perception. E-PERF stays PENDING."
    )
    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(json.dumps(report, indent=1), encoding="utf-8")
        print(f"JSON written to {json_path}")
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


def _settled_geometry(
    port: PlatformPort, attempts: int = 20, timeout_s: float = 3.0
) -> ViewportGeometry:
    """Read the window back until it stops moving, bounded both ways.

    A resize is asynchronous: macOS animates it, and the first read-back after
    ``pin_client_rect`` lands mid-flight. Reading once made a *successful*
    restore report a window size that had never been asked for and did not
    survive the next second. The fit machine settles for the same reason; this
    is the small version of it, for a probe that only needs to say what it left
    behind.
    """
    import time

    from prospector_engine.contracts import monotonic_s

    deadline = monotonic_s() + timeout_s
    previous = port.window_geometry()
    for _attempt in range(attempts):
        if monotonic_s() >= deadline:
            break
        time.sleep(0.1)
        current = port.window_geometry()
        if (
            current.valid
            and previous.valid
            and current.client_logical is not None
            and previous.client_logical is not None
            and current.client_logical == previous.client_logical
        ):
            return current
        previous = current
    return previous


def _run_setup_probe(json_path: str | None = None, restore: bool = True) -> int:
    """Run the real automatic setup against the live client. Sends no input.

    This is what pressing **Start Navigator** does, minus the window: the same
    ``build_application``, the same coordinator, the same bounded stages. Only
    the observation half runs. The armed half - control mode and turn
    characterization - needs a physical arm and is never reached from here, so
    the run is incapable of emitting an input edge; the held-lease ledger is
    printed at the end to show that it did not.

    Fitting genuinely resizes the Roblox window, because that is what the stage
    is. The client size is read before anything starts and restored on the way
    out, so the probe leaves the machine as it found it. ``--keep`` skips the
    restore for a session that is about to be used.
    """
    import json as _json
    import time

    from prospector_engine.application import build_application
    from prospector_engine.contracts import IntentType, SetupStage, monotonic_s

    print("Automatic setup probe (no input is sent; the window is restored afterwards)")
    # Everything after this line is inside the try, because build_application
    # starts the deadman helper as a child process: anything that raises before
    # the finally is reached would leave it running.
    application = build_application()
    port = application.port
    stages: list[tuple[float, str, str]] = []
    started = monotonic_s()
    original: tuple[float, float] | None = None

    try:
        # Started here for the same reason main() starts it: without a deadman
        # acknowledgement every release reports "uncertain", and a probe that
        # printed that would be describing itself rather than the machine.
        try:
            application.deadman.start()
        except Exception as exc:
            print(f"  deadman unavailable: {exc!r}")

        before = port.window_geometry()
        print(f"  before: {before.describe()}")
        original = (
            before.client_logical.size
            if before.valid and before.client_logical is not None
            else None
        )
        application.capture.start()
        application.coordinator.start()
        coordinator = application.coordinator
        coordinator.submit(coordinator.next_intent(IntentType.START_NAVIGATOR, "setup-probe"))

        # Bounded by the machine's own deadlines, with a hard ceiling here so a
        # hung stage cannot hold the terminal.
        deadline = started + 120.0
        seen = ""
        progress = coordinator.setup_progress
        while monotonic_s() < deadline:
            progress = coordinator.setup_progress
            marker = f"{progress.stage.value}/{progress.attempt}/{progress.detail}"
            if marker != seen:
                seen = marker
                stages.append((monotonic_s() - started, progress.stage.value, progress.detail))
                print(
                    f"  {monotonic_s() - started:6.2f}s  {progress.stage.value:<20}"
                    f"  {progress.detail}"
                )
            if progress.stage.terminal and not coordinator.setup_active:
                break
            time.sleep(0.05)

        held = application.authority.held_targets()
        print()
        print(f"  stage:   {progress.stage.value}")
        if progress.achieved_client_logical is not None:
            width, height = progress.achieved_client_logical
            backing = progress.achieved_client_backing_px
            suffix = f", backing {backing[0]}x{backing[1]} px" if backing else ""
            print(f"  viewport: achieved {width:.0f}x{height:.0f} pt{suffix}")
        if progress.profile_id:
            print(f"  profile: {progress.profile_id}")
        failure = progress.failure
        if failure is not None:
            print(f"  kind:    {failure.kind.value}")
            print(f"  summary: {failure.summary}")
            print(f"  remedy:  {failure.remedy}")
            if failure.detail:
                print(f"  detail:  {failure.detail}")
        print(f"  input edges held: {held}")

        if json_path:
            payload = {
                "stage": progress.stage.value,
                "ok": progress.ok,
                "elapsed_s": round(monotonic_s() - started, 3),
                "profile_id": progress.profile_id,
                "achieved_client_logical": progress.achieved_client_logical,
                "achieved_client_backing_px": progress.achieved_client_backing_px,
                "failure": None
                if failure is None
                else {
                    "kind": failure.kind.value,
                    "stage": failure.stage.value,
                    "summary": failure.summary,
                    "remedy": failure.remedy,
                    "detail": failure.detail,
                },
                "held_input_edges": list(held),
                "stages": [
                    {"at_s": round(at, 3), "stage": name, "detail": detail}
                    for at, name, detail in stages
                ],
            }
            Path(json_path).write_text(_json.dumps(payload, indent=2), encoding="utf-8")
            print(f"  wrote {json_path}")

        return 0 if progress.stage is SetupStage.READY else 1
    finally:
        application.capture.stop(2.0)
        application.shutdown()
        if restore and original is not None:
            result = port.pin_client_rect(original)
            if not result.ok:
                print(f"  restore failed: {result.message}")
            else:
                print(f"  restored: {_settled_geometry(port).describe()}")


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


def _run_hotkey_test(seconds: float = 30.0) -> int:
    """Watch the hotkey listener hear the keyboard. Emits nothing, arms nothing.

    The submit callback is a list, not the coordinator, so a chord recognized
    here cannot start a mode, consume an arm token, or reach an input session.
    Nothing in this mode can press a key.

    It answers the three questions separately, because they have three
    different remedies:

    * **Is the listener alive?**  ``state`` is READY only when the event source
      exists, is enabled, and its loop has ticked.
    * **Did the edge arrive?**  every key you press prints a raw edge, whatever
      it normalizes to. Silence here means the OS is not delivering events -
      Input Monitoring, almost always.
    * **Did the chord complete?**  a recognized chord prints its label. A chord
      that never prints while raw edges do is a normalization or exactness
      problem, not a permission one.
    """
    import time

    from prospector_engine.bindings import HotkeyState, chord_label
    from prospector_engine.contracts import IntentType, RuntimeIntent, monotonic_s
    from prospector_engine.ports import create_platform_port

    start_chord = chord_label(IntentType.START_LIVE, sys.platform)
    port = create_platform_port()
    submitted: list[RuntimeIntent] = []
    source = port.create_hotkey_source(submitted.append)

    print(f"Hotkey self-test - {seconds:.0f}s. Nothing here arms or presses anything.")
    print(f"Press {start_chord}. Ordinary keys and a lone Ctrl are shown too;")
    print("neither may stop the listener. Ctrl+C to finish early.\n")

    source.start()
    health = source.health()
    print(f"listener: {health.state.value} - {health.describe()}")
    if health.state is not HotkeyState.READY:
        print(f"\nBLOCKED: {health.detail}")
        source.stop()
        return 1

    deadline = monotonic_s() + max(1.0, seconds)
    last_edge = ""
    last_chord = ""
    edges = 0
    try:
        while monotonic_s() < deadline:
            time.sleep(0.05)
            health = source.health()
            if health.last_edge and health.last_edge != last_edge:
                last_edge = health.last_edge
                edges += 1
                print(f"  RAW EDGE      {last_edge}")
            if health.last_chord and health.last_chord != last_chord:
                last_chord = health.last_chord
                print(f"  RECOGNIZED    {last_chord} -> {health.last_chord_disposition}")
            if health.state is not HotkeyState.READY:
                print(f"\nLISTENER LOST: {health.state.value} - {health.detail}")
                break
    except KeyboardInterrupt:
        print()
    finally:
        source.stop()

    health = source.health()
    print("\n--- summary ---")
    print(f"  backend            {health.backend}")
    print(f"  edges seen         {health.events_seen}")
    print(f"  chords recognized  {health.chords_recognized}")
    print(f"  last edge          {health.last_edge or '(none)'}")
    print(f"  last chord         {health.last_chord or '(none)'}")
    print(f"  disposition        {health.last_chord_disposition or '(none)'}")
    print(f"  tap re-enables     {health.restarts}")
    print(f"  last exception     {health.last_error or '(none)'}")
    print(f"  intents submitted  {len(submitted)} (to a list, never to the coordinator)")
    if health.events_seen == 0:
        print(
            "\nNo key edge arrived at all. Grant Input Monitoring to whichever "
            "application launched this process, then restart it."
        )
        return 1
    if health.chords_recognized == 0:
        print(f"\nEdges arrived but no chord completed. {start_chord} was never seen.")
        return 1
    print(f"\n{start_chord} was heard. The listener half is working.")
    return 0


_MODES = (
    "--deadman",
    "--self-test",
    "--smoke-test",
    "--replay",
    "--capture-probe",
    "--setup-probe",
    "--detector-report",
    "--soak",
    "--shadow-bench",
    "--calibrate",
    "--hotkey-test",
)


def _option(arguments: list[str], flag: str) -> str | None:
    """The value after ``flag``, or ``None``."""
    if flag in arguments:
        index = arguments.index(flag)
        if index + 1 < len(arguments) and not arguments[index + 1].startswith("--"):
            return arguments[index + 1]
    return None


def _positional_after(arguments: list[str], flag: str) -> str | None:
    value = _option(arguments, flag)
    return None if value is None or value.startswith("-") else value


def _lifecycle_probe() -> None:
    """Print what an offline mode left behind, for the subprocess tests."""
    import subprocess
    import threading

    if os.environ.get("TREASURE_LIFECYCLE_PROBE") != "1":
        return
    try:
        children = subprocess.run(
            ["pgrep", "-P", str(os.getpid())], capture_output=True, text=True, check=False
        ).stdout.split()
    except OSError:
        children = []
    print(
        "lifecycle:"
        f" tkinter={'tkinter' in sys.modules}"
        f" treasure_gui={'treasure_gui' in sys.modules}"
        f" threads={threading.active_count()}"
        f" children={len(children)}"
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    if "--deadman" in arguments:
        return _run_deadman()
    chosen = [flag for flag in _MODES if flag in arguments]
    if len(chosen) > 1:
        print(f"Choose one mode, not {', '.join(chosen)}.")
        return 2
    if not chosen:
        from treasure_gui import main as gui_main

        return gui_main()

    mode = chosen[0]
    try:
        if mode == "--self-test":
            return _run_self_test()
        if mode == "--smoke-test":
            return _run_smoke_test()
        if mode == "--replay":
            session = _positional_after(arguments, "--replay")
            if session is None:
                print("--replay needs a recorded session directory")
                return 2
            return _run_replay(session)
        if mode == "--capture-probe":
            return _run_capture_probe()
        if mode == "--setup-probe":
            return _run_setup_probe(
                _option(arguments, "--json"), restore="--keep" not in arguments
            )
        if mode == "--detector-report":
            corpus = _option(arguments, "--corpus")
            json_path = _option(arguments, "--json")
            if corpus is not None:
                return _run_corpus_report(corpus, json_path)
            return _run_detector_report(
                _positional_after(arguments, "--detector-report") or "green_arrow_v1"
            )
        if mode == "--soak":
            minutes = _positional_after(arguments, "--soak")
            return _run_soak(float(minutes) if minutes is not None else 10.0)
        if mode == "--shadow-bench":
            seconds = _positional_after(arguments, "--shadow-bench")
            return _run_shadow_bench(
                float(seconds) if seconds is not None else 20.0, _option(arguments, "--json")
            )
        if mode == "--calibrate":
            return _run_calibrate()
        if mode == "--hotkey-test":
            seconds = _positional_after(arguments, "--hotkey-test")
            return _run_hotkey_test(float(seconds) if seconds is not None else 30.0)
        return 2
    finally:
        _lifecycle_probe()


if __name__ == "__main__":
    raise SystemExit(main())
