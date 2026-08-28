#!/usr/bin/env python3
"""Release-only out-of-process input deadman.

The helper exists for one reason: if the parent process is wedged, crashed, or
descheduled while an input is held, *something* must still lift that input.
It therefore has exactly one capability - releasing - and no code path anywhere
in this file, or in the :class:`~prospector_engine.ports.ReleaseOnlyPort` it
holds, that can press a key or a button (plan 4.5).

It releases on any of:

* a registered lease reaching its independently tracked expiry;
* an explicit ``release_all`` from the parent;
* a generation change (leases from an invalidated generation are dropped);
* stdin EOF (the parent closed the pipe or died);
* the parent process disappearing (``getppid`` changes).

Protocol: one JSON object per line in each direction, over stdin/stdout. Every
request carries the shared secret from ``TREASURE_DEADMAN_TOKEN``; an
unauthenticated request is refused without side effects.

Environment (all in the ``TREASURE_DEADMAN_*`` namespace, plan 4.5):

``TREASURE_DEADMAN_TOKEN``
    Required shared secret.
``TREASURE_DEADMAN_PLATFORM``
    ``macos`` / ``windows``; defaults to the running OS.
``TREASURE_DEADMAN_SINK``
    Test-only. When set, release edges are appended to this file instead of
    being injected, so the real helper subprocess can be exercised without
    emitting OS input.
``TREASURE_DEADMAN_POLL_MS``
    Expiry/liveness poll interval. Default 20 ms.

Run directly (``python deadman.py``) or, in the packaged build, through
``treasure.py --deadman`` which dispatches here before importing Tk, OpenCV,
capture, or engine code.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, TextIO

PROTOCOL_VERSION = 1
DEFAULT_POLL_MS = 20


@dataclass
class _Lease:
    lease_id: int
    generation: int
    target: str
    expires_at_s: float


class _Sink:
    """Where release edges go.

    The real sink is a platform release-only port. The file sink exists so the
    protocol, expiry, EOF, and parent-death paths can be tested end to end with
    a real subprocess and zero OS input.
    """

    def release(self, target: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def targets(self) -> tuple[str, ...]:  # pragma: no cover - interface
        raise NotImplementedError


class _FileSink(_Sink):
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        keys = [key.value for key in _vocabulary_keys()]
        buttons = [f"mouse:{name}" for name in ("left", "right", "middle")]
        self._targets = tuple(keys + buttons)

    def release(self, target: str) -> None:
        with self._lock, open(self._path, "a", encoding="utf-8") as handle:
            handle.write(f"{time.monotonic():.6f} release {target}\n")
            handle.flush()
            os.fsync(handle.fileno())

    def targets(self) -> tuple[str, ...]:
        return self._targets


def _vocabulary_keys() -> tuple[Any, ...]:
    from prospector_engine.contracts import InputKey

    return tuple(InputKey)


class _PlatformSink(_Sink):
    def __init__(self, platform_name: str) -> None:
        from prospector_engine.ports import create_release_only_port

        self._port = create_release_only_port(platform_name)
        vocab = self._port.vocabulary
        self._keys = {key.value: key for key in vocab.keys}
        self._buttons = {f"mouse:{button.value}": button for button in vocab.buttons}

    def release(self, target: str) -> None:
        key = self._keys.get(target)
        if key is not None:
            self._port.raw_key_up(self._port.key_code(key))
            return
        button = self._buttons.get(target)
        if button is not None:
            self._port.raw_button_up(button)

    def targets(self) -> tuple[str, ...]:
        return tuple(self._keys) + tuple(self._buttons)


class DeadmanHelper:
    """The helper's state machine, separated from stdio for testability."""

    def __init__(self, sink: _Sink, token: str, poll_ms: int = DEFAULT_POLL_MS) -> None:
        self._sink = sink
        self._token = token
        self._poll_s = max(0.001, poll_ms / 1000.0)
        self._lock = threading.Lock()
        self._leases: dict[int, _Lease] = {}
        self._generation = 0
        self._stopped = threading.Event()
        self._released_all_at_s: float | None = None

    # -- lifecycle --------------------------------------------------------
    def start_expiry_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self._expiry_loop, name="deadman-expiry", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stopped.set()

    def _expiry_loop(self) -> None:
        parent_pid = os.getppid()
        while not self._stopped.wait(self._poll_s):
            now = time.monotonic()
            with self._lock:
                expired = [
                    lease for lease in self._leases.values() if lease.expires_at_s <= now
                ]
                for lease in expired:
                    self._leases.pop(lease.lease_id, None)
            for lease in expired:
                self._sink.release(lease.target)
            # A parent that dies without closing stdin (SIGKILL) is detected
            # here: on POSIX the ppid becomes 1 (or the reaper's pid).
            if os.getppid() != parent_pid:
                self.release_all(reason="parent-death")
                self._stopped.set()
                return

    # -- operations -------------------------------------------------------
    def release_all(self, reason: str) -> tuple[str, ...]:
        """Lift every vocabulary target, unconditionally and idempotently."""
        with self._lock:
            self._leases.clear()
            self._generation += 1
            self._released_all_at_s = time.monotonic()
        released: list[str] = []
        for target in self._sink.targets():
            try:
                self._sink.release(target)
                released.append(target)
            except Exception:  # one failure may not abort the remaining release floor
                continue
        return tuple(released)

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        op = str(request.get("op", ""))
        if op == "hello":
            if request.get("token") != self._token:
                return {"ok": False, "op": op, "error": "bad-token"}
            return {"ok": True, "op": op, "protocol": PROTOCOL_VERSION, "pid": os.getpid()}
        if request.get("token") != self._token:
            return {"ok": False, "op": op, "error": "bad-token"}
        if op == "ping":
            with self._lock:
                return {
                    "ok": True,
                    "op": op,
                    "leases": len(self._leases),
                    "gen": self._generation,
                }
        if op == "register":
            return self._register(request)
        if op == "renew":
            return self._renew(request)
        if op == "forget":
            lease_id = int(request["lease_id"])
            with self._lock:
                self._leases.pop(lease_id, None)
            return {"ok": True, "op": op, "lease_id": lease_id}
        if op == "release_all":
            released = self.release_all(reason=str(request.get("reason", "parent-request")))
            with self._lock:
                gen = self._generation
            return {"ok": True, "op": op, "released": list(released), "gen": gen}
        if op == "shutdown":
            self.release_all(reason="shutdown")
            self.stop()
            return {"ok": True, "op": op}
        return {"ok": False, "op": op, "error": "unknown-op"}

    def _register(self, request: dict[str, Any]) -> dict[str, Any]:
        generation = int(request["gen"])
        with self._lock:
            if generation < self._generation:
                return {"ok": False, "op": "register", "error": "stale-generation"}
            self._generation = max(self._generation, generation)
            lease = _Lease(
                lease_id=int(request["lease_id"]),
                generation=generation,
                target=str(request["target"]),
                expires_at_s=time.monotonic() + float(request["expires_in_ms"]) / 1000.0,
            )
            self._leases[lease.lease_id] = lease
        return {"ok": True, "op": "register", "lease_id": lease.lease_id}

    def _renew(self, request: dict[str, Any]) -> dict[str, Any]:
        lease_id = int(request["lease_id"])
        generation = int(request["gen"])
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                return {"ok": False, "op": "renew", "error": "unknown-lease"}
            if generation < self._generation or lease.generation != generation:
                return {"ok": False, "op": "renew", "error": "stale-generation"}
            lease.expires_at_s = time.monotonic() + float(request["expires_in_ms"]) / 1000.0
        return {"ok": True, "op": "renew", "lease_id": lease_id}


def _serve(helper: DeadmanHelper, stdin: TextIO, stdout: TextIO) -> int:
    helper.start_expiry_thread()
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                response: dict[str, Any] = {"ok": False, "error": "bad-json"}
            else:
                response = helper.handle(request)
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
            if response.get("op") == "shutdown" and response.get("ok"):
                return 0
    finally:
        # EOF, an exception, or a normal exit all end the same way: release.
        helper.release_all(reason="stdin-eof")
        helper.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    del argv
    token = os.environ.get("TREASURE_DEADMAN_TOKEN")
    if not token:
        sys.stderr.write("deadman: TREASURE_DEADMAN_TOKEN is required\n")
        return 2
    sink_path = os.environ.get("TREASURE_DEADMAN_SINK")
    poll_ms = int(os.environ.get("TREASURE_DEADMAN_POLL_MS", DEFAULT_POLL_MS))
    sink: _Sink = (
        _FileSink(sink_path)
        if sink_path
        else _PlatformSink(
            os.environ.get("TREASURE_DEADMAN_PLATFORM", "")
            or ("windows" if sys.platform == "win32" else "macos")
        )
    )
    helper = DeadmanHelper(sink, token, poll_ms=poll_ms)
    return _serve(helper, sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
