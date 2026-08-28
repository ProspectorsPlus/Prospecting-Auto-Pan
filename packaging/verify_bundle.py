#!/usr/bin/env python3
"""Fail-loud verification of a built bundle.

Run after PyInstaller, against the produced application directory. It checks
the things that silently break in a frozen build and would otherwise only be
discovered by a user: missing package data, a helper that cannot be dispatched,
a resource path resolved relative to the current working directory, and a
missing artifact hash.

It verifies a bundle; it does not verify the *game*. Every native gate in
STATUS.md stays pending regardless of what this reports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _executable(bundle: Path) -> Path:
    candidates = [
        bundle / "Contents" / "MacOS" / "TreasureNavigator",
        bundle / "TreasureNavigator.exe",
        bundle / "TreasureNavigator",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise SystemExit(f"No Treasure Navigator executable found under {bundle}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="built application directory or .app")
    parser.add_argument("--report", type=Path, default=None)
    arguments = parser.parse_args()

    executable = _executable(arguments.bundle)
    problems: list[str] = []
    results: dict[str, object] = {"executable": str(executable), "checks": {}}

    def check(name: str, ok: bool, detail: str = "") -> None:
        results["checks"][name] = {"ok": ok, "detail": detail}  # type: ignore[index]
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
        if not ok:
            problems.append(name)

    # 1. Self-test from a *random* working directory: nothing may be resolved
    #    relative to cwd (plan 11.4).
    with tempfile.TemporaryDirectory() as elsewhere:
        completed = subprocess.run(
            [str(executable), "--self-test"],
            capture_output=True,
            text=True,
            cwd=elsewhere,
            timeout=120,
            check=False,
        )
        check(
            "self-test passes from an unrelated working directory",
            completed.returncode == 0,
            completed.stdout.strip().splitlines()[-1] if completed.stdout else completed.stderr,
        )

    # 2. Smoke test: package data plus a real deadman dispatch through a file sink.
    with tempfile.TemporaryDirectory() as elsewhere:
        completed = subprocess.run(
            [str(executable), "--smoke-test"],
            capture_output=True,
            text=True,
            cwd=elsewhere,
            timeout=120,
            check=False,
        )
        check(
            "smoke test passes (resources + deadman dispatch)",
            completed.returncode == 0,
            completed.stdout.strip().replace("\n", " | "),
        )

    # 3. Package data is physically inside the bundle.
    profiles = list(arguments.bundle.rglob("arrow_profiles.json"))
    check(
        "arrow profiles are bundled as package data",
        bool(profiles),
        str(profiles[0]) if profiles else "not found",
    )
    specs = list(arguments.bundle.rglob("evaluation_spec.json"))
    check("evaluation spec is bundled", bool(specs), str(specs[0]) if specs else "not found")

    # 4. Nothing writable is expected next to the executable.
    check(
        "bundle does not ship a writable data directory",
        not (arguments.bundle / "recordings").exists(),
        "recordings must live under the OS data root",
    )

    # 5. Artifact hash, for the release record.
    digest = _sha256(executable)
    results["sha256"] = digest
    print(f"  sha256({executable.name}) = {digest}")

    # 6. Signing status is reported, never assumed.
    if sys.platform == "darwin":
        signed = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(arguments.bundle)],
            capture_output=True,
            text=True,
            check=False,
        )
        results["macos_signature"] = (
            "valid" if signed.returncode == 0 else "unsigned-or-invalid"
        )
        print(f"  macOS signature: {results['macos_signature']} (notarization: PENDING)")
    else:
        results["windows_signature"] = "PENDING (Authenticode not applied by this script)"
        print("  Windows signature: PENDING")

    if arguments.report is not None:
        arguments.report.write_text(json.dumps(results, indent=2) + "\n")

    if problems:
        print(f"\nFAILED: {', '.join(problems)}")
        return 1
    print("\nBundle verification passed. Native game gates remain PENDING (STATUS.md).")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
