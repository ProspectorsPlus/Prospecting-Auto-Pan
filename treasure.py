#!/usr/bin/env python3
"""Treasure Navigator entry point.

    python treasure.py                launch the dashboard
    python treasure.py --deadman      run the release-only helper (internal)
    python treasure.py --self-test    import and contract check, emits no input
    python treasure.py --smoke-test   packaging smoke test, emits no input
    python treasure.py --calibrate    read client-relative pixels under the cursor

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
    capture = CaptureService(guard, EvidenceRegistry("calibrate"))
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
            rect = envelope.frame.client_rect
            print(
                f"\rPIXEL=({cursor[0]:>5},{cursor[1]:>5})  RGB=({int(r):>3},{int(g):>3},"
                f"{int(b):>3})  client={rect.width_px}x{rect.height_px} "
                f"scale {rect.scale:g}   ",
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
    if "--calibrate" in arguments:
        return _run_calibrate()
    from treasure_gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
