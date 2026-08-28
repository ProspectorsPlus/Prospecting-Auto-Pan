#!/usr/bin/env python3
"""Treasure Navigator entry point.

    python treasure.py                launch the dashboard
    python treasure.py --deadman      run the release-only helper (internal)
    python treasure.py --self-test    import and contract check, emits no input
    python treasure.py --smoke-test   packaging smoke test, emits no input
    python treasure.py --calibrate    read client-relative pixels under the cursor
    python treasure.py --capture-probe  measure capture cost, read-only
    python treasure.py --replay DIR   replay a recorded session, emits no input

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
            f"dup {metrics.duplicate_frames}  drop {metrics.dropped_frames}  "
            f"cpu {metrics.cpu_percent:3.0f}%  rss {metrics.rss_mb:3.0f} MB  [{budget} 40 ms]"
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
    if "--calibrate" in arguments:
        return _run_calibrate()
    from treasure_gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
