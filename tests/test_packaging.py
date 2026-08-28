"""Packaging, resources, provenance discipline, and reuse boundaries.

Two kinds of check live here. The first is mechanical: package data loads from
the package, nothing resolves against the current working directory, and the
build inputs exist. The second is a discipline check the plan asks for
explicitly - shipping code must not reference the `Claude` or
`ProspectorStudio` worktrees, and unvalidated features must be labelled
(plan 12, 17).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHIPPING = [
    ROOT / "treasure.py",
    ROOT / "treasure_gui.py",
    ROOT / "treasure_overlay.py",
    ROOT / "deadman.py",
    *sorted((ROOT / "prospector_engine").glob("*.py")),
]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


def test_package_data_loads_through_importlib_resources() -> None:
    profiles = resources.files("prospector_engine") / "profiles" / "arrow_profiles.json"
    document = json.loads(profiles.read_text(encoding="utf-8"))
    assert document["schema"] == 1
    assert document["profiles"]

    spec = resources.files("prospector_engine") / "profiles" / "evaluation_spec.json"
    assert json.loads(spec.read_text(encoding="utf-8"))["frozen"] is False


def test_the_app_launches_its_self_test_from_an_unrelated_working_directory() -> None:
    """Plan 11.4: nothing may be resolved relative to cwd."""
    with tempfile.TemporaryDirectory() as elsewhere:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "treasure.py"), "--self-test"],
            capture_output=True,
            text=True,
            cwd=elsewhere,
            timeout=120,
            check=False,
        )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "PASS" in completed.stdout
    assert "FAIL" not in completed.stdout


def test_the_smoke_test_passes_from_an_unrelated_working_directory() -> None:
    with tempfile.TemporaryDirectory() as elsewhere:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "treasure.py"), "--smoke-test"],
            capture_output=True,
            text=True,
            cwd=elsewhere,
            timeout=120,
            check=False,
        )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "deadman dispatched" in completed.stdout


def test_no_shipping_module_writes_relative_to_the_working_directory() -> None:
    offenders: list[str] = []
    pattern = re.compile(r"""open\(\s*["'][^/~$][^"']*["']\s*,\s*["'][wa]""")
    for path in SHIPPING:
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)


# ---------------------------------------------------------------------------
# Build inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        "pyproject.toml",
        "requirements-macos.lock",
        "requirements-windows.lock",
        "packaging/treasure.spec",
        "packaging/treasure.manifest",
        "packaging/build_macos.sh",
        "packaging/build_windows.ps1",
        "packaging/verify_bundle.py",
        "CLAUDE.md",
        "README.md",
        "STATUS.md",
        "DECISIONS.md",
    ],
)
def test_the_build_and_documentation_inputs_exist(relative: str) -> None:
    assert (ROOT / relative).is_file(), relative


def test_the_macos_lock_is_hashed_and_pinned() -> None:
    text = (ROOT / "requirements-macos.lock").read_text()
    pins = re.findall(r"^([A-Za-z0-9_.-]+)==", text, flags=re.MULTILINE)
    assert {"numpy", "mss", "opencv-python-headless", "pillow"} <= {p.lower() for p in pins}
    assert text.count("--hash=sha256:") > 50


def test_the_windows_lock_is_honestly_unpopulated() -> None:
    """It must not look authoritative while having been generated elsewhere."""
    text = (ROOT / "requirements-windows.lock").read_text()
    assert re.search(r"^[A-Za-z0-9_.-]+==", text, flags=re.MULTILINE) is None
    assert "UNPOPULATED ON PURPOSE" in text
    assert "generate" in text.lower()


def test_the_windows_build_script_refuses_an_unpopulated_lock() -> None:
    script = (ROOT / "packaging" / "build_windows.ps1").read_text()
    assert "no pinned requirements" in script
    assert "--require-hashes" in script


def test_the_spec_bundles_package_data_and_the_deadman_module() -> None:
    spec = (ROOT / "packaging" / "treasure.spec").read_text()
    assert "prospector_engine/profiles" in spec
    assert "prospector_engine/assets" in spec
    assert '"deadman"' in spec


def test_pyproject_declares_only_a_verified_interpreter() -> None:
    text = (ROOT / "pyproject.toml").read_text()
    assert 'requires-python = ">=3.13"' in text
    assert "pyautogui" not in text.lower(), "pyautogui was removed (plan 14.1)"


def test_pyautogui_is_gone_from_shipping_code() -> None:
    for path in SHIPPING:
        assert "pyautogui" not in path.read_text().lower(), path.name


# ---------------------------------------------------------------------------
# Reuse and licensing discipline
# ---------------------------------------------------------------------------


def test_no_shipping_module_references_another_worktree() -> None:
    """Plan 12: shipping code may not import or depend on those paths."""
    forbidden = re.compile(r"(Roblox Macro/Claude|ProspectorStudio|Prospector Studio/)")
    offenders = [path.name for path in SHIPPING if forbidden.search(path.read_text())]
    assert offenders == []


def test_no_shipping_module_carries_a_prospector_lite_namespace() -> None:
    """Plan 4.5: no ``PP_*`` product namespace remains in the implementation."""
    pattern = re.compile(r"\bPP_[A-Z_]+\b")
    offenders = [path.name for path in SHIPPING if pattern.search(path.read_text())]
    assert offenders == []


def test_the_license_blocker_is_recorded_rather_than_assumed_resolved() -> None:
    assert 'license = { text = "UNLICENSED" }' in (ROOT / "pyproject.toml").read_text()
    status = (ROOT / "STATUS.md").read_text()
    assert "G-LICENSE" in status
    assert "unresolved" in status.lower()


# ---------------------------------------------------------------------------
# Evidence discipline
# ---------------------------------------------------------------------------


def test_no_experiment_gate_is_marked_passed() -> None:
    spec = json.loads(
        (ROOT / "prospector_engine" / "profiles" / "evaluation_spec.json").read_text()
    )
    assert spec["frozen"] is False
    assert spec["spec_id"] is None
    for name, gate in spec["gates"].items():
        assert gate["status"] == "pending", f"{name} claims {gate['status']}"


def test_the_status_document_separates_local_macos_and_windows() -> None:
    status = (ROOT / "STATUS.md").read_text()
    assert "macOS commissioning" in status
    assert "Windows commissioning" in status
    assert "No Roblox session was operated" in status


def test_every_tuned_config_carries_provenance() -> None:
    """Plan 17: no unexplained magic numbers in configuration dataclasses."""
    from dataclasses import fields

    from prospector_engine.capture import CaptureConfig
    from prospector_engine.coordinator import CoordinatorConfig
    from prospector_engine.engine import ServiceTimings, TreasurePixels
    from prospector_engine.input_authority import AuthorityConfig
    from prospector_engine.motion import ContactConfig
    from prospector_engine.navigation import MotionConfig, RecoveryBudget
    from prospector_engine.steering import SteeringLimits
    from prospector_engine.telemetry import RecorderConfig
    from prospector_engine.turning import TurnLimits
    from prospector_engine.vision import ArrivalConfig, SegmenterConfig

    for cls in (
        AuthorityConfig,
        CaptureConfig,
        CoordinatorConfig,
        ServiceTimings,
        TreasurePixels,
        ContactConfig,
        MotionConfig,
        SteeringLimits,
        TurnLimits,
        RecoveryBudget,
        RecorderConfig,
        ArrivalConfig,
        SegmenterConfig,
    ):
        names = {field.name for field in fields(cls)}
        assert "provenance" in names, cls.__name__
        instance = cls()
        assert instance.provenance.source, cls.__name__
        assert instance.provenance.status.value in {
            "provisional",
            "pending",
            "validated",
            "observed_fact",
        }, cls.__name__


def test_the_readme_labels_what_is_unsupported() -> None:
    readme = (ROOT / "README.md").read_text()
    assert "pending" in readme.lower()
    assert "complexion.md" in readme, "the stale analysis must be flagged (plan 13.3)"
