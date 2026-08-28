"""Shared fixtures: virtual clock, wired authority, and a bounded-service context."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospector_engine import engine
from prospector_engine.contracts import RunMode
from prospector_engine.input_authority import (
    AuthorityConfig,
    HealthSources,
    InputAuthority,
    ServiceInputSession,
)
from tests.fakes import (
    FakeCancellation,
    FakeDeadmanClient,
    FakeFrameSource,
    FakePlatformPort,
    VirtualClock,
    install_virtual_clock,
    make_frame,
    make_geometry,
)

CLOCK_MODULES = (
    "prospector_engine.engine",
    "prospector_engine.input_authority",
    "prospector_engine.capture",
)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> VirtualClock:
    virtual = VirtualClock()
    install_virtual_clock(monkeypatch, virtual, *CLOCK_MODULES)
    return virtual


@pytest.fixture
def journal() -> list[str]:
    """One ordered log shared by the port and the deadman.

    Ordering between a deadman ACK and a native down-edge is a safety
    invariant, so both have to land in the same list to be assertable.
    """
    return []


@pytest.fixture
def port(clock: VirtualClock, journal: list[str]) -> FakePlatformPort:
    return FakePlatformPort(clock, geometry=make_geometry(), journal=journal)


@pytest.fixture
def deadman(journal: list[str]) -> FakeDeadmanClient:
    return FakeDeadmanClient(journal=journal)


@dataclass
class Rig:
    """Everything a safety or service test needs, already wired together."""

    clock: VirtualClock
    port: FakePlatformPort
    deadman: FakeDeadmanClient
    authority: InputAuthority
    capture_age_s: Callable[[], float | None]
    journal: list[str]
    set_capture_age: Callable[[float | None], None]

    def activate(self, generation: int = 1, *, mode: RunMode = RunMode.SERVICE) -> None:
        self.authority.activate_generation(
            generation,
            emits_input=mode.emits_input,
            cancellation=None,
            requires_capture=True,
            pinned_rect=self.port.window_geometry(),
        )

    def session(self, generation: int = 1) -> ServiceInputSession:
        return self.authority.service_session(generation)


@pytest.fixture
def rig(
    clock: VirtualClock,
    port: FakePlatformPort,
    deadman: FakeDeadmanClient,
    journal: list[str],
) -> Iterator[Rig]:
    age: dict[str, float | None] = {"s": 0.005}

    def capture_age_s() -> float | None:
        return age["s"]

    def set_capture_age(value: float | None) -> None:
        age["s"] = value

    authority = InputAuthority(
        port,
        deadman=deadman,
        health=HealthSources(
            focus=port.focus_state,
            client_rect=port.window_geometry,
            capture_age_s=capture_age_s,
        ),
        config=AuthorityConfig(),
        run_id="test-run",
    )
    rig = Rig(clock, port, deadman, authority, capture_age_s, journal, set_capture_age)
    yield rig
    authority.stop_watchdog(timeout_s=0.5)


@pytest.fixture
def service_context(rig: Rig) -> engine.ServiceContext:
    """A bounded-service context whose waits advance virtual time."""
    rig.activate()
    cancellation = FakeCancellation(rig.clock)
    rig.authority.activate_generation(
        1,
        emits_input=True,
        cancellation=cancellation,
        requires_capture=True,
        pinned_rect=rig.port.window_geometry(),
    )
    frames = FakeFrameSource()
    return engine.ServiceContext(
        frames=frames,
        session=rig.session(),
        cancel=cancellation,
        deadline_s=rig.clock.now() + 120.0,
        on_status=None,
    )


@pytest.fixture
def blank_frame_source(clock: VirtualClock) -> FakeFrameSource:
    from prospector_engine.capture import EvidenceRegistry

    registry = EvidenceRegistry("test-run")
    source = FakeFrameSource()
    source.push(registry.envelope_for(make_frame(1, captured_at_s=clock.now())))
    return source
