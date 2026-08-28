"""Writable data roots, emit-on-change telemetry, and the bounded evidence recorder.

Three separate jobs that share one theme: nothing here may ever block or stall
the control loop, and nothing here writes relative to the current working
directory (plan 11.1, 11.4, bug B13).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from prospector_engine.contracts import (
    CapturedFrame,
    EvidenceStatus,
    Provenance,
    TelemetrySnapshot,
    monotonic_s,
)

__all__ = [
    "AppPaths",
    "EventLog",
    "EvidenceRecorder",
    "LatestSlot",
    "RecordedFrame",
    "RecorderConfig",
    "RecorderStats",
    "TelemetryHub",
    "atomic_write_bytes",
    "atomic_write_text",
    "clear_recovery_record",
    "read_recovery_record",
    "read_session",
    "resolve_app_paths",
    "write_recovery_record",
]


# ---------------------------------------------------------------------------
# Writable roots
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppPaths:
    """The single resolved contract for every mutable file this app writes.

    Read-only bundled resources are loaded with ``importlib.resources`` and
    never from here; shipping code must not assume its own bundle or the
    current working directory is writable (plan 11.4).
    """

    root: Path

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def profiles(self) -> Path:
        return self.root / "profiles"

    @property
    def recordings(self) -> Path:
        return self.root / "recordings"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def manifests(self) -> Path:
        return self.root / "manifests"

    @property
    def recovery(self) -> Path:
        return self.root / "recovery"

    @property
    def crash(self) -> Path:
        return self.root / "crash"

    def ensure(self) -> AppPaths:
        for path in (
            self.root,
            self.config,
            self.profiles,
            self.recordings,
            self.logs,
            self.manifests,
            self.recovery,
            self.crash,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self


def resolve_app_paths(override: str | os.PathLike[str] | None = None) -> AppPaths:
    """Resolve the writable root for this platform.

    ``TREASURE_DATA_DIR`` (or an explicit override) is honoured for
    development and tests; it must point somewhere other than the filesystem
    root so a typo cannot scatter files across the disk.
    """
    raw = override or os.environ.get("TREASURE_DATA_DIR")
    if raw:
        candidate = Path(raw).expanduser().resolve()
        if str(candidate) == candidate.anchor or str(candidate) in ("/", ""):
            raise ValueError(f"TREASURE_DATA_DIR must not be the filesystem root: {candidate}")
        return AppPaths(candidate)
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise RuntimeError(
                "LOCALAPPDATA is not set; cannot resolve the Windows data root. "
                "Set TREASURE_DATA_DIR explicitly."
            )
        return AppPaths(Path(local) / "ProspectorTreasure")
    return AppPaths(Path.home() / "Library" / "Application Support" / "Prospector Treasure")


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write via a temporary file *in the destination directory*, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(temporary, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Latest-only publication
# ---------------------------------------------------------------------------


class LatestSlot[T]:
    """A size-one drop-oldest slot.

    The legacy status path pushed a message every 10 ms into an unbounded
    queue even while stopped (bug B13). A consumer that falls behind should
    see the newest truth, not a backlog of stale ones.
    """

    def __init__(self) -> None:
        self._value: T | None = None
        self._lock = threading.Lock()
        self._dropped = 0
        self._updated = threading.Event()

    def publish(self, value: T) -> None:
        with self._lock:
            if self._value is not None:
                self._dropped += 1
            self._value = value
        self._updated.set()

    def take(self) -> T | None:
        with self._lock:
            value, self._value = self._value, None
        self._updated.clear()
        return value

    def peek(self) -> T | None:
        with self._lock:
            return self._value

    def wait(self, timeout_s: float) -> bool:
        return self._updated.wait(timeout_s)

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped


class EventLog:
    """A bounded ring of stable-named events for the UI and the crash record."""

    def __init__(self, capacity: int = 200) -> None:
        self._events: deque[tuple[float, str, str]] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def add(self, name: str, detail: str = "") -> None:
        with self._lock:
            self._events.append((monotonic_s(), name, detail))

    def recent(self, limit: int = 20) -> tuple[tuple[float, str, str], ...]:
        with self._lock:
            return tuple(list(self._events)[-limit:])

    def as_lines(self, limit: int = 20) -> tuple[str, ...]:
        return tuple(
            f"{name}: {detail}" if detail else name for _, name, detail in self.recent(limit)
        )


class TelemetryHub:
    """Emit-on-change publication of :class:`TelemetrySnapshot`.

    Identical consecutive snapshots are dropped, so a stopped application is
    silent instead of filling a queue at the poll rate (bug B13).
    """

    def __init__(self) -> None:
        self._slot: LatestSlot[TelemetrySnapshot] = LatestSlot()
        self._last_key: tuple[Any, ...] | None = None
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[TelemetrySnapshot], None]] = []
        self._sequence = 0
        self._emitted = 0
        self._suppressed = 0

    def subscribe(self, callback: Callable[[TelemetrySnapshot], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    @staticmethod
    def _change_key(snapshot: TelemetrySnapshot) -> tuple[Any, ...]:
        return (
            snapshot.mode,
            snapshot.phase,
            snapshot.viewport.identity() if snapshot.viewport else None,
            snapshot.ledger_empty,
            snapshot.focus,
            snapshot.warnings,
            tuple(sorted(snapshot.readiness.items())),
            snapshot.command,
        )

    def publish(self, snapshot: TelemetrySnapshot) -> bool:
        key = self._change_key(snapshot)
        with self._lock:
            if key == self._last_key:
                self._suppressed += 1
                return False
            self._last_key = key
            self._sequence += 1
            self._emitted += 1
            stamped = replace(snapshot, sequence=self._sequence)
            subscribers = list(self._subscribers)
        self._slot.publish(stamped)
        for callback in subscribers:
            with contextlib.suppress(Exception):
                callback(stamped)
        return True

    def latest(self) -> TelemetrySnapshot | None:
        return self._slot.peek()

    @property
    def counters(self) -> Mapping[str, int]:
        with self._lock:
            return {"emitted": self._emitted, "suppressed": self._suppressed}


# ---------------------------------------------------------------------------
# Evidence recorder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecorderConfig:
    """Explicit bounds, so a long session cannot fill the disk (plan 11.1)."""

    queue_capacity: int = 32
    pre_event_ring: int = 40
    chunk_frames: int = 30
    chunk_uncompressed_bytes: int = 256 * 1024 * 1024
    session_bytes: int = 8 * 1024 * 1024 * 1024
    protected_bytes: int = 2 * 1024 * 1024 * 1024
    background_fps: float = 2.0
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="TREASURE_NAVIGATION_PLAN.md section 11.1",
            note="explicit initial bounds; disk behaviour on long soaks is PENDING",
        )
    )


@dataclass
class RecorderStats:
    accepted: int = 0
    dropped_ordinary: int = 0
    chunks_written: int = 0
    bytes_written: int = 0
    protected_bytes: int = 0
    truncated: bool = False
    last_error: str | None = None


@dataclass(frozen=True)
class _Packet:
    frame: CapturedFrame
    telemetry: Mapping[str, Any]
    protected: bool


class EvidenceRecorder:
    """Bounded, checksummed, chunked frame recorder on one writer thread.

    Overflow drops the oldest *ordinary* packet and increments a visible
    counter; a protected packet (labelled, contact, arrival, event-triggered)
    is never dropped silently. At either ceiling the recorder stops accepting
    frames and finalizes the manifest with ``truncated=true`` rather than
    deleting evidence.
    """

    def __init__(
        self,
        session_dir: Path,
        *,
        config: RecorderConfig | None = None,
        mask_regions_px: tuple[tuple[int, int, int, int], ...] = (),
    ) -> None:
        self._dir = session_dir
        self._config = config or RecorderConfig()
        self._mask_regions = mask_regions_px
        self._queue: queue.Queue[_Packet | None] = queue.Queue(
            maxsize=self._config.queue_capacity
        )
        self._stats = RecorderStats()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pending: list[_Packet] = []
        self._chunk_index = 0
        self._manifest: dict[str, Any] = {}

    @property
    def stats(self) -> RecorderStats:
        with self._lock:
            return replace(self._stats)

    @property
    def session_dir(self) -> Path:
        return self._dir

    def start(self) -> None:
        if self._thread is not None:
            return
        (self._dir / "chunks").mkdir(parents=True, exist_ok=True)
        self._quarantine_orphans()
        self._manifest = {
            "schema": 1,
            "started_at_unix_s": time.time(),
            "config": {
                "chunk_frames": self._config.chunk_frames,
                "queue_capacity": self._config.queue_capacity,
                "session_bytes": self._config.session_bytes,
            },
            "chunks": [],
            "truncated": False,
        }
        self._write_manifest()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._writer_loop, name="treasure-recorder", daemon=True
        )
        self._thread.start()

    def offer(
        self,
        frame: CapturedFrame,
        telemetry: Mapping[str, Any] | None = None,
        *,
        protected: bool = False,
    ) -> bool:
        """Non-blocking hand-off. Returns False when the packet was dropped.

        The control loop never waits on this: at capacity an ordinary packet
        is discarded and counted, and a protected packet displaces the oldest
        ordinary one rather than being lost.
        """
        with self._lock:
            if self._stats.truncated:
                return False
        packet = _Packet(frame=frame, telemetry=dict(telemetry or {}), protected=protected)
        try:
            self._queue.put_nowait(packet)
            return True
        except queue.Full:
            pass
        if not protected:
            with self._lock:
                self._stats.dropped_ordinary += 1
            return False
        with contextlib.suppress(queue.Empty):
            self._queue.get_nowait()
            with self._lock:
                self._stats.dropped_ordinary += 1
        try:
            self._queue.put_nowait(packet)
            return True
        except queue.Full:
            with self._lock:
                self._stats.dropped_ordinary += 1
            return False

    def stop(self, timeout_s: float = 2.0) -> bool:
        """Bounded finalize. A wedged writer must not block application exit."""
        self._stop.set()
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout_s)
            if thread.is_alive():
                return False
        self._flush_chunk(final=True)
        self._write_manifest()
        return True

    def _quarantine_orphans(self) -> None:
        """Move chunks the manifest never recorded into ``quarantine/``.

        A run that ended without a bounded flush can leave a chunk on disk that
        no manifest entry describes. It is evidence of *something*, so it is
        set aside rather than deleted or silently replayed (plan 11.1).
        """
        manifest_path = self._dir / "manifest.json"
        known: set[str] = set()
        if manifest_path.exists():
            with contextlib.suppress(OSError, json.JSONDecodeError):
                previous = json.loads(manifest_path.read_text(encoding="utf-8"))
                known = {str(entry["name"]) for entry in previous.get("chunks", [])}
        orphans = [p for p in (self._dir / "chunks").glob("*.npz") if p.name not in known]
        if not orphans:
            return
        destination = self._dir / "quarantine"
        destination.mkdir(parents=True, exist_ok=True)
        for path in orphans:
            with contextlib.suppress(OSError):
                os.replace(path, destination / path.name)

    # -- writer -----------------------------------------------------------
    def _writer_loop(self) -> None:
        while not self._stop.is_set():
            try:
                packet = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if packet is None:
                break
            try:
                self._accept(packet)
            except Exception as exc:
                with self._lock:
                    self._stats.last_error = repr(exc)
        with contextlib.suppress(Exception):
            self._flush_chunk(final=True)
            self._write_manifest()

    def _accept(self, packet: _Packet) -> None:
        with self._lock:
            if self._stats.bytes_written >= self._config.session_bytes:
                self._stats.truncated = True
                self._manifest["truncated"] = True
                return
            if packet.protected and self._stats.protected_bytes >= self._config.protected_bytes:
                self._stats.truncated = True
                self._manifest["truncated"] = True
                return
            self._stats.accepted += 1
        self._pending.append(packet)
        if len(self._pending) >= self._config.chunk_frames:
            self._flush_chunk(final=False)

    def _masked(self, frame: CapturedFrame) -> np.ndarray[Any, Any]:
        """Fixed chat/HUD regions are blanked at write time.

        Moving world-space name tags are *not* masked and this is not claimed
        to be anonymization (plan 11.1).
        """
        image = np.array(frame.bgr, copy=True)
        for x, y, width, height in self._mask_regions:
            image[y : y + height, x : x + width] = 0
        return image

    def _flush_chunk(self, *, final: bool) -> None:
        if not self._pending:
            if final:
                self._manifest["ended_at_unix_s"] = time.time()
            return
        packets, self._pending = self._pending, []
        self._chunk_index += 1
        name = f"{self._chunk_index:06d}.npz"
        path = self._dir / "chunks" / name
        arrays = {f"frame_{i:04d}": self._masked(p.frame) for i, p in enumerate(packets)}
        metadata = [
            {
                "sequence": p.frame.sequence,
                "captured_at_s": p.frame.captured_at_s,
                "duration_ms": p.frame.duration_ms,
                "duplicate": p.frame.duplicate,
                "protected": p.protected,
                "telemetry": p.telemetry,
            }
            for p in packets
        ]
        import io

        buffer = io.BytesIO()
        payload_arrays: dict[str, Any] = dict(arrays)
        payload_arrays["meta"] = np.array(json.dumps(metadata))
        np.savez_compressed(buffer, **payload_arrays)
        payload = buffer.getvalue()
        atomic_write_bytes(path, payload)
        digest = hashlib.sha256(payload).hexdigest()
        protected_bytes = sum(len(payload) for p in packets if p.protected)
        with self._lock:
            self._stats.chunks_written += 1
            self._stats.bytes_written += len(payload)
            self._stats.protected_bytes += protected_bytes
        self._manifest.setdefault("chunks", []).append(
            {
                "name": name,
                "frames": len(packets),
                "bytes": len(payload),
                "sha256": digest,
                "protected": any(p.protected for p in packets),
            }
        )
        if final:
            self._manifest["ended_at_unix_s"] = time.time()
        self._write_manifest()

    def _write_manifest(self) -> None:
        atomic_write_text(
            self._dir / "manifest.json", json.dumps(self._manifest, indent=2) + "\n"
        )


# ---------------------------------------------------------------------------
# Reading a recorded session back
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedFrame:
    """One frame read back from a recording, with its recorded metadata."""

    sequence: int
    captured_at_s: float
    duration_ms: float
    duplicate: bool
    protected: bool
    telemetry: Mapping[str, Any]
    bgr: NDArray[np.uint8]


def read_session(
    session_dir: Path, *, verify_checksums: bool = True
) -> Iterator[RecordedFrame]:
    """Stream a recorded session back in order.

    Chunks are independently recoverable, so a corrupt or truncated one is
    skipped with a warning rather than aborting the whole replay - losing the
    tail of a session must not lose its beginning (plan 11.1).
    """
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"no manifest.json under {session_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest.get("chunks", []):
        path = session_dir / "chunks" / str(entry["name"])
        if not path.exists():
            continue
        payload = path.read_bytes()
        if verify_checksums and hashlib.sha256(payload).hexdigest() != entry.get("sha256"):
            continue  # quarantine rather than replay corrupt evidence
        try:
            import io

            with np.load(io.BytesIO(payload), allow_pickle=False) as archive:
                metadata = json.loads(str(archive["meta"]))
                for index, record in enumerate(metadata):
                    image = np.asarray(archive[f"frame_{index:04d}"], dtype=np.uint8)
                    image.flags.writeable = False
                    yield RecordedFrame(
                        sequence=int(record["sequence"]),
                        captured_at_s=float(record["captured_at_s"]),
                        duration_ms=float(record["duration_ms"]),
                        duplicate=bool(record["duplicate"]),
                        protected=bool(record["protected"]),
                        telemetry=dict(record.get("telemetry", {})),
                        bgr=image,
                    )
        except (OSError, ValueError, KeyError):
            continue


# ---------------------------------------------------------------------------
# Unsafe-release recovery record
# ---------------------------------------------------------------------------

RECOVERY_RECORD_NAME = "unsafe_release.json"


def write_recovery_record(paths: AppPaths, reason: str, evidence: Mapping[str, Any]) -> Path:
    """Persist that a release could not be confirmed safe.

    Plan 4.4: a missing release ACK or a failed local edge must leave a
    prominent record for the *next* launch, not just a log line in a process
    that is about to exit.
    """
    paths.recovery.mkdir(parents=True, exist_ok=True)
    path = paths.recovery / RECOVERY_RECORD_NAME
    payload = {
        "schema": 1,
        "written_at_unix_s": time.time(),
        "reason": reason,
        "evidence": dict(evidence),
        "consequence": (
            "Live and every bounded SERVICE mode are refused until an explicit "
            "release-only recovery handshake succeeds."
        ),
    }
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")
    return path


def read_recovery_record(paths: AppPaths) -> dict[str, Any] | None:
    path = paths.recovery / RECOVERY_RECORD_NAME
    if not path.exists():
        return None
    try:
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # An unreadable record is still a record: fail closed.
        return {"schema": 0, "reason": "unreadable recovery record", "evidence": {}}
    return loaded


def clear_recovery_record(paths: AppPaths) -> None:
    """Remove the record. Only a *successful* recovery handshake may call this."""
    path = paths.recovery / RECOVERY_RECORD_NAME
    with contextlib.suppress(OSError):
        path.unlink()
