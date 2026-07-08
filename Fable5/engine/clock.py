"""Clock abstraction.

RealClock wraps the wall clock with a millisecond-accurate hybrid sleep
(coarse OS sleep, then a short spin for the tail — OS sleep alone can
overshoot by 1-10 ms, which matters for 18 ms shake clicks).

The simulator provides its own SimClock with the same interface, which is
how the whole engine runs headless under a virtual clock in tests.
"""
from __future__ import annotations

import time


class Clock:
    """Interface: now() -> seconds (monotonic float), sleep_ms(ms)."""

    def now(self) -> float:
        raise NotImplementedError

    def sleep_ms(self, ms: float) -> None:
        raise NotImplementedError

    def sleep_until(self, deadline: float) -> None:
        raise NotImplementedError


class RealClock(Clock):
    SPIN_S = 0.002          # busy-wait tail length

    def now(self) -> float:
        return time.perf_counter()

    def sleep_until(self, deadline: float) -> None:
        while True:
            rem = deadline - time.perf_counter()
            if rem <= 0:
                return
            if rem > self.SPIN_S:
                time.sleep(rem - self.SPIN_S)
            else:
                # spin the tail for ms accuracy
                while time.perf_counter() < deadline:
                    pass
                return

    def sleep_ms(self, ms: float) -> None:
        if ms <= 0:
            return
        self.sleep_until(time.perf_counter() + ms / 1000.0)
