# Treasure Navigator — Final Implementation Plan

**Status:** implementation-ready plan; no feature code has been changed by this document.  
**Branch/worktree:** `Treasure` at `/Users/ibraheemarif/Roblox Macro/Treasure`  
**Prepared:** 2026-08-27  
**Primary goal:** turn the existing “dig once already at the spot” Treasure macro into a documented, observable, cross-platform navigator that follows the equipped treasure-map arrow, avoids obstacles, confirms arrival, digs/collects, and—after separately proving the necessary evidence—advances to the next map.

This plan is authoritative for the implementation. It incorporates the useful evidence from the earlier Claude drafts but resolves their remaining contradictions around input ownership, cancellation, platform commissioning, direction ground truth, map lifecycle, recovery, and recording.

---

## 1. Product outcome and hard boundaries

### 1.1 Required product outcome

When the user equips a supported treasure map and starts Treasure Navigator:

1. The application verifies and pins the Roblox **client area** to a canonical physical-pixel geometry.
2. It detects or uses the explicitly selected arrow colour/profile.
3. It observes the arrow and a validated player-forward reference from coherent frames.
4. In Shadow mode it shows what it would do without emitting game input.
5. In physically armed Live mode it walks and turns toward the arrow with closed-loop corrections.
6. It detects lack of actual movement, performs bounded obstacle recovery, and abandons safely when recovery is exhausted.
7. It detects arrival from validated screen evidence, releases movement, and hands control to the bounded dig/pan-swap system.
8. Once map-completion and next-map evidence are separately validated, it equips the next map and repeats.
9. It works from the same source tree on macOS and Windows, with native behavior commissioned independently on each OS.
10. Its code, configuration, tests, diagnostics, and documentation are readable enough for public review.

### 1.2 Safety invariants

These are non-negotiable and enforced by code plus tests:

- One application-wide input authority; no feature may call native input directly.
- At most one input-emitting mode worker exists at a time.
- Every held key/button has a monotonic lease and independent watchdog.
- An out-of-process deadman can release inputs but can never press them.
- Stop, focus loss, invalid viewport, stale evidence, uncaught error, mode transfer, and shutdown release all injectable inputs.
- Releases are unconditional and never focus-gated.
- No retry loop lacks both an attempt cap and a monotonic deadline.
- A stale, duplicated beyond budget, incoherent, or low-confidence frame cannot renew input.
- No detector or controller must guess: ambiguity produces an explicit abstention.
- Live mode requires a physical click in the Treasure UI during the current process run. Arming is never persisted.
- Missing Windows or macOS hardware evidence is reported as `pending`; it is never fabricated.

### 1.3 Version-1 scope boundary

The first validated Live milestone operates on **one map manually equipped and one profile explicitly selected**. Automatic profile classification and automatic next-map selection must each pass their own evidence gates before being enabled. If they do not pass, the application stops safely after the current treasure instead of guessing.

### 1.4 Explicitly deferred

- SLAM or a persistent world map.
- Monocular depth estimation.
- YOLO/CNN training.
- OCR, earnings, Discord notifications, scripting/Studio nodes, and input-action recording.
- Replacing Tkinter with Electron or a web stack.
- Automatic next-map behavior before its evidence exists.

These are not needed to prove arrow-guided closed-loop navigation and would add packaging or reliability risk.

---

## 2. Verified starting point

The Treasure branch currently has seven tracked runtime/source documents:

| File | Current role |
|---|---|
| `treasure.py` | 21-line launcher for GUI or calibration. |
| `treasure_gui.py` | Minimal fixed-size Tk GUI; imports the engine at module scope. |
| `prospector_engine/engine.py` | Dig pixels, pan swap, reset sequence, mutable global state, run loop. |
| `prospector_engine/platform_mac.py` | Quartz/AX input, window functions, hotkeys. |
| `prospector_engine/platform_win.py` | SendInput/Win32 window functions, hotkeys. |
| `prospector_engine/__init__.py` | Version/package surface. |
| `complexion.md` | Stale pruning analysis for a different repository state. |

Current branch status contains the user-owned untracked `.venv-python38-backup/`. It must not be inspected recursively, moved, staged, deleted, or included in packaging.

### 2.1 Confirmed defects to remove during foundation work

| ID | Defect | Required disposition |
|---|---|---|
| B1 | Shared `engine.py` imports `pyautogui` and Quartz unconditionally, preventing clean Windows import. | Move OS behavior behind injected ports; remove `pyautogui`. |
| B2 | `dequip_pan()` can press forever. | Typed bounded result, attempt cap, deadline, cancellation. |
| B3 | Reset calls `dequip_pan()` outside its `try`, so cancellation can leave reset permanently active. | Move inside guarded service; unconditional cleanup. |
| B4 | F5 is advertised but absent from both hotkey listener bindings. | Route F5 through coordinator on both platforms. |
| B5 | Stop releases only LMB, not all held keys/buttons. | One global, idempotent `release_all()`. |
| B6 | Automatic and F5 pan swap can run concurrently. | Exclusive coordinator modes. |
| B7 | Multiple threads mutate class-level `State` without ownership. | Coordinator-owned immutable snapshots; intent submission only. |
| B8 | Platform button methods reference undefined `_HELD_BUTTONS`. | Held state belongs only to InputAuthority. |
| B9 | macOS cursor code references nonexistent `State.scale`. | Use canonical viewport scale. |
| B10 | `fr_move_to` references multiple missing Lite symbols. | Delete the dead callable path. |
| B11 | macOS treats outer/window geometry differently from Windows client geometry. | Canonical verified physical client rect. |
| B12 | A single logical tick reads three independently captured instants. | One coherent stamped frame per cycle. |
| B13 | While stopped, status is emitted every 10 ms into an unbounded queue. | Emit-on-change plus size-one latest snapshot. |

Characterization tests are written before changing working dig, reset, and pan-swap behavior. They preserve correct behavior without preserving the defects above.

### 2.2 Facts, hypotheses, experiments, and decisions

Keeping these categories separate prevents screenshots or plausible ideas from becoming unexplained production constants.

| Category | Item | Status/consequence |
|---|---|---|
| Verified fact | Current Treasure can detect two calibrated dig pixels, read one capacity pixel, execute reset/pan sequences, and pin/find a Roblox window on macOS. | Preserve correct behavior through characterization. |
| Verified fact | Shared engine imports and several platform backreferences are broken or unsafe on Windows. | Phase 0 fixes B1–B13 before navigation. |
| Verified fact | The supplied frames show a large coloured arrow and one positive arrival banner, but only one map/session/Mac configuration. | Useful fixture seeds, never validation corpus. |
| Hypothesis | A per-map colour/contrast profile plus geometry and temporal tracking can isolate the arrow. | Test in E-PROF; unsupported profiles stay explicit/manual. |
| Hypothesis | Centroid, tip, shaft/arrowhead, PCA, skeleton width-profile, or a fusion can provide signed direction. | Compare in E-DIR-IDEAL and E-DIR-E2E; no preselected winner. |
| Hypothesis | Screen-up may equal player-forward after deterministic camera reset. | E-FORWARD must prove it; otherwise calibrate an offset or abstain. |
| Hypothesis | Optical/phase motion can distinguish movement from collision under yaw. | E-MOTION decides; no time-only fallback. |
| Hypothesis | The arrival banner plus lifecycle context is a reliable arrival event. | E-ARRIVE decides; one screenshot is insufficient. |
| Hypothesis | Dig completion, collection, and next-map state have stable visible cues. | E-LIFECYCLE decides; one-map mode remains the safe fallback. |
| Design decision | Reactive closed-loop screen control before world mapping. | Lowest necessary complexity for a continuously visible direction cue. |
| Design decision | Explicit profile selection first; automatic classification must earn activation. | Eliminates an avoidable silent-map misclassification path. |
| Design decision | Shadow default and per-run physical Live arm. | Evidence can be collected safely without accidental unattended control. |

---

## 3. Final runtime architecture

### 3.1 Ownership and thread model

```text
Tk main thread ─────────────┐
Hotkey listener thread ─────┤ submit RuntimeIntent only
                            ▼
                   RuntimeCoordinator thread
                   - sole RunMode owner
                   - priority intent processing
                   - starts/cancels one mode worker
                   - invalidates mode generations
                   - performs ownership transfers
                            │
                            ▼
                   at most one mode worker
                   - receives the mode-specific capability
                     (NoInputSession, NavigationInputSession,
                      or ServiceInputSession)
                   - never receives raw PlatformPort or ledger
                   - posts a generation-tagged WorkerCompletion

Capture thread        publishes latest coherent stamped frame; no input
Recorder writer       bounded queue; no control-loop blocking
Safety/lease watchdog expires leases and polls health independently of workers
Deadman subprocess    release-only; survives parent scheduling failure
```

`RuntimeCoordinator` never executes navigation, dig, reset, pan swap, or next-map logic synchronously. Its event loop remains responsive even if capture or a native mode worker stalls.

### 3.2 Runtime intents

```python
class IntentType(Enum):
    PIN_WINDOW = auto()
    START_SHADOW = auto()
    ARM_LIVE_FROM_UI = auto()
    START_LIVE = auto()
    STOP = auto()
    RESET_CHARACTER = auto()
    PAN_SWAP_TEST = auto()
    SHUTDOWN = auto()


@dataclass(frozen=True)
class RuntimeIntent:
    sequence: int
    intent_type: IntentType
    source: Literal["gui", "hotkey", "system"]
    created_at_s: float


@dataclass(frozen=True)
class WorkerCompletion:
    generation: int
    mode: RunMode
    worker_id: str
    result: ModeResult


@dataclass(frozen=True)
class SafetyFault:
    generation: int | None  # None means process-global monitor evidence
    kind: SafetyFaultKind
    evidence: tuple[str, ...]
    observed_at_s: float
```

Use a priority queue:

1. `STOP` and `SHUTDOWN`.
2. Focus/viewport/deadman/capture safety faults.
3. Ordinary start, reset, and test requests.

Duplicate ordinary requests are coalesced. Safety requests are never dropped. Only a `WorkerCompletion` matching the current generation, mode, and worker ID may change the FSM; a late mismatched completion is recorded and discarded. A process-global safety fault always releases. A generation-tagged worker fault releases only when current, while a stale one is retained as diagnostics and cannot perturb a newer mode.

### 3.3 Coordinator transition protocol

Starting or transferring a mode (after the special Live proof conversion in §3.4, when applicable):

1. Invalidate the previous authority generation.
2. Set the prior worker’s cancellation token.
3. Call `release_all()` immediately.
4. Join the prior worker within a bounded deadline; ignore any later stale result.
5. For a new input-emitting mode, require both an empty ledger and a safe `ReleaseReport` with no `release_uncertain` latch. Shadow may start with `NoInputSession` while the unsafe-release latch remains prominently visible; it can never clear or bypass that latch.
6. Validate mode-specific readiness. Live/input modes require viewport, positive focus, watchdog, deadman, and evidence gates. Shadow requires a valid capture viewport but uses a no-input sink and does not require focus, watchdog, deadman, or Live arming.
7. Activate a new generation and issue the narrow mode-specific capability: `NoInputSession` for Shadow, `NavigationInputSession` for Live navigation, or `ServiceInputSession` for a bounded reset/dig/pan operation.
8. Start exactly one cancellable mode worker.

Stopping a pending/active input mode:

1. Priority-dispatch Stop without waiting for capture or feature work.
2. Invalidate the generation so a stale worker cannot press or renew.
3. Set cancellation.
4. Release every input immediately.
5. Join within the configured deadline.
6. Enter `SAFE_STOP`, publish the reason/evidence, and return to `IDLE` only after the ledger is empty and release is known safe. Shadow may still be offered when release is uncertain, but Live/input modes may not.

Stopping Shadow cancels/joins its observer to the same bounded deadline and defensively calls `release_all()`, but because `NoInputSession` could not have emitted an edge it may return to `IDLE` while preserving and prominently displaying any pre-existing/new `release_uncertain` latch. That latch continues to block every Live/`SERVICE` start until release-only recovery succeeds.

The initial in-process engineering budget is stop-to-up-event p95 under 100 ms and worst observed under 250 ms on commissioned hardware. This is a safety target, not a measured claim: record its provenance in configuration, measure it in E-PERF/native gates, and either meet it or leave that platform’s Live gate pending. Do not silently loosen it after testing.

### 3.4 Shadow and physically armed Live startup

Shadow executes the same capture, perception, lifecycle, and controller decision path through a `NoInputSession`. It records proposed `NavigationCommand`s but is structurally unable to reach a raw input port. Shadow may continue while Tk is frontmost so the user can inspect diagnostics.

Live arming is a one-use `LiveArmToken` bound to the current coordinator generation and process run. The physical **Arm Live** Tk callback creates it with a configured 30-second initial TTL. Clicking Tk makes Tk frontmost, so the supported sequence is explicit:

1. User physically clicks **Arm Live**.
2. UI displays the remaining token time and instructs the user to refocus Roblox.
3. User physically focuses Roblox and presses the Live-start hotkey.
4. Under the coordinator lock, the `START_LIVE` handler verifies token/run/generation/TTL/hotkey source, atomically consumes it **before** normal transition invalidation, and creates a one-use `ConsumedArmProof` bound only to that intent sequence.
5. The normal transition protocol invalidates the old generation and performs readiness checks using that proof. The proof cannot authorize any other intent or generation.

The application never silently refocuses Roblox later. Focus loss/unknown focus is a releasing safety fault only while an input generation is pending or active. While `IDLE`, Shadow, or merely waiting with an arm token, non-positive focus only marks Live readiness unavailable and does **not** consume the token—the expected refocus sequence would otherwise be impossible. `START_LIVE` still requires positive Roblox focus. Any failed readiness check after proof conversion or failed transition spends the proof and requires a new physical arm. The token is cleared on expiry, Stop, session completion, mode failure, active-input focus/viewport/deadman fault, shutdown, unrelated coordinator-generation change, duplicate `START_LIVE`, or conversion into the single accepted proof. Tests cover successful conversion, expected unfocused armed waiting, expiry, an unrelated transition, duplicate start, and failed readiness; there is no exemption that leaves a reusable token behind.

### 3.5 Thread liveness and bounded shutdown

The Tk/main thread is the only non-daemon application thread. Coordinator, mode worker, capture, recorder, safety/watchdog, and hotkey threads are named daemon threads with explicit cancellation/close methods; daemon status is an exit backstop, not a substitute for cleanup. No adapter may create an undocumented non-daemon listener.

Shutdown is ordered and bounded:

1. Stop accepting ordinary intents; invalidate authority and close the native-edge gate.
2. Cancel the active worker and execute `release_all()`, including deadman release-all ACK.
3. Stop capture/hotkey/watchdog production and request a bounded recorder finalize.
4. Join each component only to its named deadline; record any survivor and never wait forever.
5. Close the deadman pipe so EOF supplies another release trigger, wait to a deadline, then terminate only the release-only helper if it will not exit.
6. Persist the final shutdown/release report and allow the packaged parent to exit even if an uncancellable daemon worker survives.

A missing release ACK leaves a prominent unsafe-release recovery record for the next launch; the helper’s independent expiry/EOF path remains active. Tests inject a permanently blocked worker, capture backend, recorder write, listener, and helper response and prove bounded parent shutdown.

---

## 4. Platform boundary, viewport, input authority, and deadman

### 4.1 Canonical viewport contract

```python
@dataclass(frozen=True)
class ClientRectPhysicalPx:
    origin_px: tuple[int, int]
    size_px: tuple[int, int]
    scale: float
    verified_at_s: float
    display_id: str
    valid: bool
    invalid_reason: str | None
```

All capture, click, detector, recorder, and diagnostic coordinates use physical pixels relative to this **client area**. No feature uses outer-window coordinates.

- Requested client size: `1280 × 720` physical pixels.
- macOS requests an OS-legal deterministic position; it does **not** require absolute client origin `(0, 0)`, which the menu-bar safe area may reject.
- Windows uses Per-Monitor V2 DPI awareness, `AdjustWindowRectExForDpi` when available, and client-rect readback.
- Both ports read back the achieved client origin and exact size and use that returned value everywhere.
- A size error above one physical pixel, display/DPI change, resize, move outside policy, fullscreen transition, or lost client identity invalidates the contract.
- Invalidating the viewport releases input before reacquisition or repinning.

Legacy fixed dig/pan pixels must be transformed from their old macOS window-frame basis to client coordinates and manually reverified. Do not silently reuse the old numbers.

### 4.2 Raw platform port

`PlatformPort` is private to `InputAuthority`, viewport management, and the deadman’s release-only backend.

```python
class PlatformPort(Protocol):
    @property
    def vocabulary(self) -> InputVocabulary: ...
    def focus_state(self) -> FocusState: ...
    def find_client_rect(self) -> ClientRectPhysicalPx | None: ...
    def pin_client_rect(self, size_px: tuple[int, int]) -> PinResult: ...
    def raw_key_down(self, code: int) -> None: ...
    def raw_key_up(self, code: int) -> None: ...
    def raw_button_down(self, button: MouseButton) -> None: ...
    def raw_button_up(self, button: MouseButton) -> None: ...
    def raw_pointer_move_client(self, point_px: tuple[int, int]) -> None: ...
    def raw_pointer_delta(self, dx: int, dy: int) -> None: ...
    def raw_scroll_lines(self, lines: int) -> None: ...
    def create_hotkey_source(self, submit: Callable[[RuntimeIntent], None]) -> HotkeySource: ...
```

Platform modules are instance based. They do not bind back into an engine module, mutate `_eng`, or maintain an authoritative held-input set.

### 4.3 Capability-bound input

Feature workers receive only:

```python
class ServiceInputSession:
    def hold_key(self, key: InputKey, max_hold_ms: int) -> LeaseHandle: ...
    def tap_key(self, key: InputKey, hold_ms: int) -> None: ...
    def hold_button(self, button: MouseButton, max_hold_ms: int) -> LeaseHandle: ...
    def tap_button(self, button: MouseButton, hold_ms: int) -> None: ...
    def pointer_move_client(self, point_px: tuple[int, int]) -> None: ...
    def pointer_delta(self, dx: int, dy: int) -> None: ...
    def scroll_lines(self, lines: int) -> None: ...
    def renew(self, lease: LeaseHandle, horizon_ms: int) -> None: ...
    def release(self, lease: LeaseHandle) -> None: ...


class NavigationInputSession:
    def apply_navigation_command(
        self,
        command: NavigationCommand,
        evidence: EvidenceToken,
    ) -> NavigationApplyResult: ...
    def release_navigation(self, reason: str) -> ReleaseReport: ...
```

Every call validates generation, active mode, cancellation, viewport, positive focus, watchdog, and deadman health. Navigation code never receives the generic hold/tap/pointer/scroll methods. `apply_navigation_command` is the sole navigation input path and atomically validates an authority-issued opaque `EvidenceToken`: process run, generation, frame sequence, conservative capture timestamp, capture-duration gate, viewport identity, freshness deadline, and match to the command fields. Tokens cannot be constructed by feature code, are revoked on any safety fault/generation change, and a repeated sequence cannot acquire or renew. The session translates the accepted axes/jump/yaw into bounded leases under the native-edge barrier.

Absolute pointer points are bounded, physical-pixel, client-relative coordinates and are converted through the currently verified client origin only inside the platform port. Scroll counts are bounded signed logical lines. Neither API accepts desktop coordinates from feature code.

Focus policy is exact:

- New input and renewal require `focus_state is True`.
- `False` releases immediately.
- `None` permits no new press, pointer delta, or renewal; existing leases release immediately.
- A release never checks focus.

### 4.4 Sole held-state registry

`InputAuthority` owns the only held-state ledger. `InputVocabulary` is the immutable release floor containing every key/button Treasure can inject.

`release_all()` returns a typed `ReleaseReport` and is failure-isolated:

1. While holding the shared edge barrier, atomically invalidates the generation and closes native-edge admission.
2. Attempts up-events for every active lease, catching and recording each failure separately.
3. Attempts unconditional, idempotent up-events for the full vocabulary even if an earlier edge failed; local release edges finish before the barrier is reopened.
4. Commands the deadman to release all, advances its generation, and requires a positive ACK even if local release raised.
5. Reconciles the ledger and returns attempted edges, failures, deadman status, and whether release is known safe.

One exception may not abort the remaining release floor. Any local failure, missing deadman ACK, or unresolved lease latches `release_uncertain`; ledger emptiness by itself is insufficient. The coordinator refuses every new Live/input mode until an explicit release-only recovery handshake succeeds. Shadow and diagnostics may remain available.

### 4.5 Lease/deadman ordering

Acquisition:

1. Validate and create a pending lease under the authority lock.
2. Register it with deadman and require a positive ACK.
3. Enter the single native-edge barrier shared by acquisition, pointer moves, scrolls, and `release_all()`.
4. Revalidate generation, token, focus, viewport, capture freshness, deadman, and cancellation after the ACK and immediately before the edge.
5. Emit native down and atomically commit the active lease before leaving the barrier; recheck the authority epoch as part of the commit.
6. On any failure or epoch mismatch, emit an unconditional up, tell deadman to forget/release, and roll back.

Stop/fault closes admission and invalidates the epoch under that same barrier before executing the release floor. Thus a down cannot occur after a completed Stop release. Pointer moves and scrolls use the same barrier and pre-edge revalidation. Tests pause execution at every acquisition boundary and race Stop/fault against it. Native calls themselves must meet a commissioned bounded-latency gate; the already-registered deadman lease is the fallback if a native call stalls.

Renewal is not additive. It snapshots the current authority epoch/active lease, validates health, and asks the deadman to ACK the replacement expiry. After that ACK it re-enters the authority/native-edge commit gate, revalidates epoch, generation, active lease identity, cancellation, focus, viewport, capture/evidence freshness, and helper health, and only then atomically sets `expires_at = now + min(requested_horizon, configured_max_rolling_horizon)`. On mismatch it asks the helper to release/forget and rejects the renewal. Repeated renewals cannot move expiry farther than the configured horizon from the most recent acknowledged health check. Tests race Stop at every pre-ACK/post-ACK/commit boundary.

Release:

1. Emit native up.
2. Mark inactive.
3. Tell deadman to forget the lease.

The independent safety/lease watchdog polls positive focus, client identity/geometry, capture freshness, deadman health, and lease expiry at a frozen maximum interval even when the mode worker is stalled. Initial provisional configuration is `safety_poll_interval_ms = 25` and `max_rolling_lease_horizon_ms = 250`; both carry provenance and must meet E-PERF/native release gates rather than being reported as achieved facts. For an active/pending input generation, invalid/unknown focus, viewport change, over-age capture, helper failure, or expired lease closes admission and runs the release path without waiting for a worker call. Outside an input generation, those conditions update readiness/diagnostics but do not manufacture a releasing fault or invalidate an arm token. A platform that cannot sustain the frozen interval safely remains Live-ineligible.

The root `deadman.py`:

- is dispatched by `treasure.py --deadman` before importing Tk, OpenCV, capture, or engine code;
- is token- and generation-authenticated;
- imports only the platform’s release-only adapter;
- releases on lease expiry, parent death, stdin EOF, explicit release-all, or generation change;
- cannot issue down-events;
- has source and PyInstaller-frozen launch paths covered by tests.

All helper environment variables and protocol names use the `TREASURE_DEADMAN_*` namespace. No `PP_*`/Prospector Lite product namespace remains in the shipping implementation.

Live refuses to arm until the watchdog and deadman complete readiness handshakes.

---

## 5. Core immutable contracts

Anything crossing a thread boundary is frozen and contains tuples/read-only arrays. A frozen dataclass containing a mutable NumPy array is insufficient; arrays are marked non-writeable or copied.

Minimum contracts:

```python
@dataclass(frozen=True)
class CapturedFrame:
    sequence: int
    captured_at_s: float       # capture began; conservative pixel-age origin
    completed_at_s: float
    duration_ms: float
    client_rect: ClientRectPhysicalPx
    bgr: NDArray[np.uint8]       # read-only
    duplicate: bool
    capture_error: str | None


@dataclass(frozen=True)
class ArrowObservation:
    profile_id: str | None
    track_id: int | None
    bbox_px: tuple[int, int, int, int] | None
    centroid_px: tuple[float, float] | None
    tip_px: tuple[float, float] | None
    axis_unit_xy: tuple[float, float] | None
    confidence: float
    valid: bool
    abstain_reason: str | None


@dataclass(frozen=True)
class DirectionObservation:
    error_deg: float | None
    confidence: float
    cue_id: str
    cue_disagreement_deg: float | None
    valid: bool
    abstain_reason: str | None


@dataclass(frozen=True)
class MotionObservation:
    forward_speed_norm: float | None
    lateral_speed_norm: float | None
    confidence: float
    inlier_count: int
    inlier_ratio: float
    spatial_coverage: float
    residual: float
    yaw_contamination: float
    valid: bool
    abstain_reason: str | None


@dataclass(frozen=True)
class ArrivalObservation:
    confidence: float
    support_hits: int
    support_window: int
    latched_map_id: str | None
    valid: bool
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class NavigationCommand:
    generation: int
    source_frame_sequence: int
    source_captured_at_s: float
    forward_axis: Literal[-1, 0, 1]  # reverse, neutral, forward
    lateral_axis: Literal[-1, 0, 1]  # left, neutral, right
    jump: bool
    yaw_delta_px: int
    issued_at_s: float
    valid_until_s: float
    reason: str


@dataclass(frozen=True)
class TelemetrySnapshot:
    sequence: int
    mode: RunMode
    phase: NavigationPhase | None
    viewport: ClientRectPhysicalPx | None
    arrow: ArrowObservation | None
    direction: DirectionObservation | None
    motion: MotionObservation | None
    arrival: ArrivalObservation | None
    command: NavigationCommand | None
    ledger_empty: bool
    focus: FocusState
    frame_age_ms: float | None
    warnings: tuple[str, ...]
```

Every measurement identifies units explicitly. Durations use monotonic seconds internally and are rendered as milliseconds only at boundaries.

Freshness is computed when a consumer reads the frame: `age_s = now_monotonic - captured_at_s`, where `captured_at_s` is the conservative start of image acquisition. A capture whose `duration_ms` exceeds the frozen budget is rejected even if it just completed; completion time remains useful for throughput diagnostics but never makes old pixels young. Do not store a durable `stale` boolean that can become false information while a frame waits in a queue. Property tests reject impossible command combinations defined by the active phase (for example, simultaneous reverse and a forward-only recovery level).

Every command lease is evidence-bound: `valid_until_s` may not exceed `source_captured_at_s + max_evidence_age_s`. Re-reading, duplicating, or republishing the same frame sequence cannot extend that deadline. Applying/renewing navigation requires a strictly newer accepted `EvidenceToken`, and the authority rechecks source age independently of the worker. Direct commissioning actuator pulses use a separately bounded `ServiceInputSession`; they cannot renew a navigation command.

The capture/evidence registry publishes an immutable `FrameEnvelope(frame, evidence_token)`. The token is an opaque object registered privately with `InputAuthority`; it attests only to original process/run, capture sequence/timestamps/duration, and viewport identity—not to detector correctness. Feature code cannot instantiate or alter one. The worker may derive observations from the paired read-only frame, but `apply_navigation_command` accepts only the exact registered token whose fields match the command, then records its sequence as consumed. This makes provenance/freshness enforceable without allowing perception code to mint its own authority.

---

## 6. Application and navigation state machines

### 6.1 Application lifecycle

`RunMode` and lifecycle phase are orthogonal. The coordinator owns `IDLE | SHADOW | LIVE | SERVICE | SAFE_STOP`; only `LIVE` and the explicitly started bounded `SERVICE` mode can receive an input capability. The lifecycle phase below is real in Live and a clearly labelled proposed/observed phase in Shadow.

```text
IDLE ──START_SHADOW──► SHADOW OBSERVER ──STOP/END/ERROR──► IDLE
IDLE ──valid START_LIVE proof──► LIVE:NAVIGATE
IDLE ──focused RESET/PAN hotkey──► SERVICE:RESET/PAN ──result──► IDLE
Any input mode ──STOP/FAULT──► SAFE_STOP ──safe release report──► IDLE

SHADOW OBSERVER
 ├─ runs capture/perception/navigation decision FSM with NoInputSession
 ├─ records WOULD_APPLY NavigationCommand and WOULD_RECOVER decisions
 ├─ ARRIVED ─► records WOULD_HANDOFF_DIG, then ends or waits for a new map
 └─ never invokes dig, pan-swap, reset, next-map, or any ServiceInputSession

LIVE:NAVIGATE
 ├─ ARRIVED ─► DIG
 ├─ ABANDONED ─► SAFE_STOP
 └─ failure/cancel ─► SAFE_STOP

DIG
 ├─ DIG_PROGRESS ─► DIG                    bounded attempts/time
 ├─ PAN_FULL ─► PAN_SWAP
 ├─ TREASURE_COMPLETE ─► NEXT_MAP or SESSION_COMPLETE
 ├─ CUE_LOST ─► bounded DIG_REACQUIRE
 └─ timeout/cancel/failure ─► SAFE_STOP

PAN_SWAP
 ├─ SUCCESS ─► DIG
 └─ timeout/cancel/failure ─► SAFE_STOP

NEXT_MAP
 ├─ EQUIPPED ─► NAVIGATE
 ├─ NO_MAPS ─► SESSION_COMPLETE
 └─ ambiguous/failure ─► SAFE_STOP

SERVICE:RESET
 ├─ SUCCESS ─► IDLE
 └─ failure ─► SAFE_STOP

SAFE_STOP/SESSION_COMPLETE ─► release + ledger-empty assertion ─► IDLE
```

Until `E-NEXT_MAP` passes, `TREASURE_COMPLETE` goes to `SESSION_COMPLETE` and tells the user to equip the next map manually.

### 6.2 Navigation worker FSM

```text
ACQUIRE ─► ALIGN ─► FOLLOW
                    ├─ low progress with valid evidence ─► CONTACT
                    ├─ arrival evidence ─► ARRIVAL_CONFIRM
                    └─ bounded arrow loss ─► REACQUIRE

CONTACT ─► RECOVERY ─► FOLLOW
RECOVERY exhausted ─► ABANDONED
ARRIVAL_CONFIRM confirmed ─► ARRIVED
Any invariant/cancel/deadline failure ─► FAILED
```

No state transition is justified only by elapsed time. Time can expire an action, but it cannot prove collision, movement, arrival, or success.

Within each navigation update, event priority is fixed: **safety/cancellation fault → credible arrival candidate → contact/recovery → ordinary reacquisition/steering**. The first credible arrival candidate releases movement immediately and enters stationary `ARRIVAL_CONFIRM`; arrival monitoring preempts recovery. Any invalid/stale motion evidence during recovery releases movement and returns to bounded reacquisition or safe-stop—it cannot continue or escalate a maneuver on elapsed time.

---

## 7. Evidence and evaluation protocol

The supplied screenshots are priors only: one Mac, one session, one outfit, one map colour, one render configuration, and window chrome. They do not establish production thresholds.

### 7.1 Recording metadata and labels

Every evidence session records:

- monotonic frame sequence/timestamp and actual capture interval;
- canonical client rect, scale, display mode, OS, game FPS;
- map profile and whether selection was explicit/inferred;
- camera pitch/zoom/reset state;
- issued intent and actual leased command;
- raw observation, confidence, validity, abstention reason;
- engine commit and profile/evaluation versions.

Manual labels use physical client coordinates and include avatar control anchor, player-forward reference, arrow mask/bbox/tip/axis, required signed turn, arrival interval, actual movement/contact, yaw state, and recovery outcome. Uncertain labels are `unknown`, never forced positive or negative.

Splits are by complete session, route, machine, OS, and map profile—not adjacent frames.

### 7.2 Freeze-before-held-out rule

Before evaluating held-out data, write an immutable `evaluation_spec.json` containing:

- algorithm/version and fitted profile values;
- confidence and abstention thresholds;
- absolute gates and sample/exposure requirements per stratum;
- non-zero minimum accepted-coverage and recall lower-confidence bounds per supported stratum, plus maximum consecutive loss/reacquisition and decision-latency bounds where applicable;
- handling of unknown labels;
- consequence of failure.

Training fits parameters. Validation selects algorithms and thresholds. Held-out is evaluated once. Any change after viewing held-out results requires a fresh held-out session.

### 7.3 Required abstention cases

- Stale, failed, or over-budget duplicated frame.
- Invalid viewport; and, for a pending/active input generation only, non-positive focus.
- Unknown/unsupported profile.
- Arrow clipped beyond support, severely occluded, or ill-conditioned.
- Competing candidates inside the ambiguity margin.
- Direction cues materially disagree.
- Player-forward reference is invalid.
- Motion has insufficient features, poor spatial coverage, or yaw contamination.
- Arrival evidence occurs outside the expected map lifecycle.

For a pending/active input generation, a stale, failed, capture-error, invalid-viewport, non-positive-focus, or over-budget duplicate frame releases all navigation input immediately. The Live arrow-loss grace applies only when the current frame is fresh/coherent, viewport and positive focus remain valid, and the sole missing evidence is the temporarily absent arrow after a recent high-confidence track. Even then yaw releases immediately; only the previously safe forward command may persist until the frozen grace expires, after which everything releases and the navigator reacquires or safe-stops. In Shadow, focus is only Live-readiness telemetry: the observer continues perception and proposed decisions while Tk is frontmost, and because it has `NoInputSession` there is no real command to preserve or release.

### 7.4 Experiments in dependency order

The evidence dependency has two passes and no component is allowed to define its own truth. First collect/fix estimator candidates on training/validation data. Separately, after Phase 0 input safety is complete, run bounded physically armed actuator/outcome trials: E-YAW measures the smallest stable correction, while manual-target E-STEER trials freeze the **maximum usable alignment deadband** at which route outcomes remain acceptable. Neither uses the production arrow/forward estimator. Before held-out evaluation, freeze those actuator limits, an absolute total perception-error budget, per-component allocations, non-trivial coverage/recall requirements, and `evaluation_spec.json`. Then evaluate E-ANCHOR/E-FORWARD/E-DIR against the frozen budget. Only a passing combined system may select a controller deadband inside the independent actuator interval; it may not enlarge the deadband to excuse perception error.

#### E-VIEW — client geometry and capture coordinates

Pin/read back the client, use fiducial checks, test DPI/Retina, movement/resizing, fullscreen, multi-display changes, and frame freshness.

Gate: exact `1280×720` physical client size within one pixel, consistent origin/readback, all scripted invalidations detected. A failed OS/display condition remains unsupported and cannot arm Live.

#### E-ANCHOR — avatar control anchor

Reviewer labels the avatar control pivot. Measure median/p95 absolute error, systematic bias, induced angular error at the minimum supported arrow radius, invalidation recall, and false invalidation/hour.

Gate: p95 induced angular contribution is within the frozen anchor allocation (initially no more than 25% of the independently frozen total perception-error budget); every scripted invalid condition is detected. No outfit/darkest-mass runtime fallback.

#### E-FORWARD — player/camera forward reference

Anchor position does not establish forward. On open ground:

1. Normalize camera and verify viewport; record a reproducible physical reset/zero-heading procedure before fitting candidates.
2. Record the candidate’s pre-pulse forward estimate without revealing it to the operator/reviewers.
3. In a physically armed trial, issue one bounded `W` pulse.
4. Independently label at least five static world landmarks and the avatar/path displacement before/after; inverse robust scene displacement is label evidence, not an input reused by a candidate under test.
5. Repeat full resets and both turn signs. Two reviewers, blinded to candidate output, label a subset twice; report inter-reviewer and test-retest uncertainty.
6. Repeat after camera-only/player-only rotations, pitch/zoom changes, slopes, swimming, reset, and on both OSes. Unsupported states must be identified before the pulse and abstain.

Gate: p95 forward-reference error is within the frozen forward allocation (initially no more than 25% of total perception-error budget); sign is correct in every accepted trial; lower-confidence coverage meets the predeclared per-stratum minimum; unsupported camera states abstain. Label uncertainty is included rather than treated as zero.

#### E-DIR-IDEAL — direction cue selection on manual masks

Compare:

- player-to-centroid ray;
- player-to-detected-tip ray;
- shaft/arrowhead orientation;
- PCA/min-area rectangle as coarse axes;
- skeleton/width-profile tip-tail orientation;
- position-plus-pose fusion.

Ground truth comes from repeatable reset/aligned-zero outcome trials, not from the estimator or from the yaw a human happened to apply. The operator aligns a manually defined arrow/path target, performs bounded left/right perturbations and short forward outcome probes, and resets between trials. Candidate overlays remain hidden during capture; independent reviewers label target axis, forward path, and signed correction from raw video/landmarks. Repeated sessions, both signs, inter-reviewer agreement, and test-retest uncertainty are required.

Each candidate returns estimate, confidence, and abstention. Metrics include signed-turn correctness, zero crossing, median/p95 error, monotonicity, coverage, calibration, and worst supported stratum.

Gate: no accepted wrong-sign episode on held-out data; p95 cue error stays within the frozen direction allocation (initially no more than 50% of total perception-error budget); zero crossing fits the independently frozen usable-alignment interval; ambiguity/degeneracy abstains; and the lower confidence bound for accepted coverage meets the frozen non-zero minimum in every supported stratum. Choose the simplest candidate within an operational tie, not merely the relative “best.”

#### E-PROF — per-profile arrow detection

Compare end-to-end pipelines using chromaticity/channel relationships, HSV, Lab, local-background contrast, blur/no-blur, morphology, connected components/face merging, geometry, temporal prediction, and strict versus tracked relaxed masks. The yellow-map `R≈G` relationship is only one candidate.

Metrics: false high-confidence acquisitions/hour, promotion errors, track switches, coverage, longest loss, reacquisition time, centroid/tip/axis error, confidence calibration.

Gate: each claimed profile/condition meets its frozen accepted-coverage/recall lower bounds, false-acquisition bound, maximum consecutive-loss duration, and reacquisition deadline. A detector that merely abstains is a failure. Production defaults to explicit profile selection. Automatic classification is enabled only after zero observed held-out misidentifications **and** the one-sided upper confidence bound is below the frozen maximum misclassification rate in every supported stratum.

#### E-DIR-E2E — direction with actual detector output

Run the selected direction strategy on actual segmentation, quantify degradation, and re-run all direction gates on fresh held-out data including pale terrain, clipping, occlusion, transparent/multi-face arrows, and UI overlap. The end-to-end accepted-coverage lower bound, wrong-sign upper bound, consecutive-loss cap, and reacquisition limit apply per supported stratum; abstention cannot bypass them. Only this end-to-end result can enable steering.

#### E-ARRIVE — arrival event

Evaluate the arrival banner’s edge/gradient/glyph-outline response at actual geometry and multiple UI scales. Use a validation-selected N-of-M temporal rule, expected lifecycle context, recent valid approach, and a per-map latch. The current arrow need not still exist on the banner frame.

Include full approach/fade sequences, arrow loss near arrival, other banners, and long negatives on each supported OS/profile. Report recall/latency and a false-arrival/hour upper confidence bound. Freeze a non-zero recall lower bound, maximum confirmation/arrival latency, and maximum tolerated pre-arrival cue-loss/reacquisition interval per stratum. Exactly one latch may occur per map lifecycle, and a detector that never latches fails.

If the dataset cannot statistically support unattended arrival claims, allow only physically armed guarded beta and mark unattended release pending.

#### E-MOTION — actual movement/contact evidence

Compare LK+robust affine/RANSAC, phase correlation, and a simpler displacement method. Compare affine derotation, opportunistic no-yaw windows, and measured post-yaw hold-off windows.

Use independently labelled stationary, unobstructed, genuinely blocked, turn-only, move-only, move+yaw, slide, jump, water, slope, speed/buff, crowd/particle, lag, frozen/duplicate, and dropped-frame clips at 30/60/100/120 FPS.

Normalize displacement by actual monotonic `Δt`. Seed/freeze each locomotion-condition baseline from independently labelled, physically armed open-ground trials—not from the production estimator’s own “unobstructed” decision. Runtime adaptation is bounded and may only account for supported upward/within-band changes; it never lowers the minimum-progress baseline automatically. A lower baseline requires an explicit recalibration dataset/version. Missing or out-of-condition baseline disables Live rather than guessing.

Contact requires forward command, fresh frame, valid high-confidence motion, low forward progress for a measured interval, and yaw contamination below threshold. There is no time-only fallback.

Gate: no false-contact episode on accepted moving held-out clips; blocked-event recall and p95 latency meet the frozen evaluation spec; low-confidence cases abstain. Failure disables Live recovery and Live navigation.

#### E-YAW / E-STEER-CAL / E-STEER-E2E — physically armed control evidence

Before perception held-out evaluation, run bounded E-YAW yaw/A-D pulses on both OSes to establish sign, smallest stable response, linear range, settling time, and variability. In the same pre-freeze stage, E-STEER-CAL uses manually labelled target headings and observable route outcomes to find the largest deadband that still gives acceptable alignment; it does not use production arrow/forward estimates. Freeze `min_stable_correction_deg` and `max_usable_deadband_deg` with their conditions and uncertainty.

Only after perception gates and controller implementation, E-STEER-E2E runs guarded open-ground routes with the already-frozen controller. Routes must converge from both sides without wrong-direction episodes or sustained oscillation, and release 100% on Stop/focus/viewport invalidation/force-kill. E-STEER-E2E cannot redefine the deadband; failure requires a new evaluation version and fresh held-out evidence.

If A/D does not materially improve speed or stability, use `W` plus right-drag yaw only.

#### E-RECOVERY — guarded recovery policy

E-MOTION proves contact evidence; it does not prove that the recovery ladder is safe or effective. First replay/simulate labelled contact and non-contact episodes. Then run physically armed, capped private-server trials with independently labelled obstacles and outcomes: both detour sides, wrong-side traps, slopes, water edges, crowds/particles, simultaneous yaw, lag/frame loss, arrow loss, motion invalidation, Stop, and focus/viewport loss.

Freeze and report per stratum: false-recovery upper bound, recovery-success lower bound, maximum input/time/path cost, oscillation/side-switch count, correct-abandon rate, immediate cancellation latency, and leaked-input count. Require a minimum number of independent episodes, routes, and sessions for each claimed recovery level. Any stale/invalid evidence must release without escalation; every exhaustion must abandon safely. Live recovery remains disabled unless both E-MOTION and E-RECOVERY pass for that OS/profile/condition.

#### E-DIG / E-NEXT_MAP — lifecycle evidence

`E-DIG` covers dig registered, partial progress, pan full, pan swap success/failure, treasure complete, cue loss, and explicit failure.

`E-NEXT_MAP` covers inventory state, map available, selected slot, equip confirmation, profile selection, no maps left, and ambiguity. Until it passes, next-map automation stays disabled.

Skipping an unresolved/abandoned map is a different operation from advancing after verified treasure completion. `ABANDONED` always safe-stops by default. A future opt-in **E-SKIP_MAP** must independently validate that the current map/inventory state can be identified and skipped without consuming, deleting, or equipping the wrong item; only then may that explicit policy transition `ABANDONED → NEXT_MAP`. E-NEXT_MAP success-after-completion does not satisfy E-SKIP_MAP.

Treat the combined work as **E-LIFECYCLE**: create a typed map-state detector and record/label dig progress, treasure completion, collection completion, map depletion, inventory/modal interruption, death/reset, map equip, profile identity, and no-map states. A transition may enter production only when its particular evidence is validated; do not infer `TREASURE_COMPLETE` merely because the arrow disappeared.

#### E-PERF — cadence and latency

Measure p50/p95 capture, perception, control, preview, frame age, duplicate/stale rate, and Stop latency. The initial engineering budget is a 20 Hz controller with p95 capture-plus-perception under 40 ms and p95 accepted-frame age under 100 ms. These numbers are provisional targets with configuration provenance, not achieved-performance claims. E-PERF may select a lower cadence only if the full steering/recovery stability gates are repeated and pass; otherwise the hardware/condition stays unsupported. Preview is independently rate-capped and dropped before control work is delayed.

### 7.5 Rare-event reporting

Every frozen gate declares minimum **independent sessions, routes, and episodes per stratum** in addition to hours/frames. Consecutive frames and repeated events within one run are clustered evidence, not independent samples. For plausibly independent homogeneous exposure with zero events, `3/T` is only an approximate one-sided 95% Poisson upper rate. For heterogeneous or session-clustered data, use episode/session-aware confidence intervals or hierarchical/cluster bootstrap reporting and show each stratum separately; do not pool the corpus merely to manufacture a smaller bound. Never claim a rarer rate than the number and diversity of independent sessions can support.

---

## 8. Perception and tracking pipeline

Profiles are versioned package data with provenance, supported viewport/camera contract, fitted sessions, and evaluation-spec ID.

```text
coherent frame
  → fixed UI exclusion masks
  → candidate colour/contrast model
  → connected components and optional face merging
  → geometry/plausibility scoring
  → strict global acquisition
  → relaxed mask only near a strong predicted track
  → ambiguity test
  → temporal tracking
  → candidate direction strategies
  → accepted DirectionObservation or abstention
```

Tracking uses a bounded constant-velocity prediction only to prioritize/search—not to fabricate a missing measurement. Confidence decreases on clipping, exclusion-region contact, implausible size, disagreement, or competing candidates.

The controller consumes the arrow direction relative to the **validated player-forward ray**. It never silently assumes screen-up is player-forward unless E-FORWARD proves that camera contract.

---

## 9. Steering and obstacle recovery

### 9.1 Steering

Use a bounded proportional controller with filtered derivative damping:

```text
turn = clamp(Kp × filtered_error + Kd × d(error)/dt,
             -max_turn_rate, +max_turn_rate)
```

- Derivatives use monotonic `Δt`.
- Duplicate/missing frames freeze or reset derivative state.
- Lower confidence reduces command magnitude; it never raises gain.
- Deadband plus hysteresis prevents left/right chatter.
- Yaw rate and acceleration are capped.
- Sign ambiguity releases yaw immediately.
- Every hold/pulse expires unless fresh valid evidence renews it.
- Deadband is selected on validation and frozen before held-out control trials. It must be large enough to suppress the measured combined estimator chatter/noise, no smaller than the stable actuator resolution, and no larger than independently measured `max_usable_deadband_deg`; the combined perception error must already fit its separately frozen budget. Increasing deadband after seeing estimator failures is forbidden.

### 9.2 Recovery entry and side score

Recovery starts only from valid E-MOTION contact evidence.

For each candidate side, compute a score from:

- measured progress restored by a short bounded side probe;
- change in signed path error after the probe;
- motion confidence and spatial coverage;
- proximity to viewport/terrain risk cues if a validated cue exists;
- recent side failures and oscillation penalty;
- total added path/input cost.

The chosen side locks for the episode. It cannot flip inside the cooldown unless the current side reaches an explicit failure predicate.

### 9.3 Finite recovery ladder

Each level has an input-lease limit, attempt cap, monotonic deadline, success predicate, failure predicate, and next state. Initial values are named provisional configuration and are tuned only on validation data.

| Level | Action | Success evidence | Failure transition |
|---|---|---|---|
| R1 | Bounded tangent bias while preserving forward intent. | High-confidence forward progress restored and path error not diverging. | R2 |
| R2 | Jump while continuing the locked tangent. | Sustained restored progress. | R3 |
| R3 | Release forward, bounded reverse, then rotate away from contact. | Reacquired arrow plus restored forward progress. | R4 |
| R4 | Mark first side failed and try the opposite side once. | Sustained restored progress. | R5 |
| R5 | Release movement, normalize camera, reacquire anchor/forward/arrow. | All evidence valid again. | ABANDONED |

An entire recovery episode has a total time cap and total input cap. Success requires evidence; elapsed time alone is never success. Exhaustion returns `ABANDONED`, releases everything, records the evidence, and safe-stops. Skipping that unresolved map is unavailable unless the separate, explicit E-SKIP_MAP gate has passed and the user enabled that guarded policy.

Simulation/property tests prove every recovery episode terminates, stale/low-confidence observations cannot escalate, side lock cannot chatter, and no input outlives its lease.

---

## 10. Dig, pan-swap, completion, and next-map contracts

Bounded dig/pan/reset/next-map services receive `ServiceInputSession`, never a navigation session, raw port, or ledger.

```python
class DigOutcome(Enum):
    DIG_PROGRESS = auto()
    TREASURE_COMPLETE = auto()
    PAN_FULL = auto()
    CUE_LOST = auto()
    TIMEOUT = auto()
    CANCELLED = auto()
    FAILED = auto()


@dataclass(frozen=True)
class DigHandoffResult:
    outcome: DigOutcome
    evidence: DigEvidence
    elapsed_s: float
    attempts: int
    detail: str


class PanSwapOutcome(Enum):
    SUCCESS = auto()
    TIMEOUT = auto()
    CANCELLED = auto()
    FAILED = auto()


class NextMapOutcome(Enum):
    EQUIPPED = auto()
    NO_MAPS = auto()
    AMBIGUOUS = auto()
    TIMEOUT = auto()
    CANCELLED = auto()
    FAILED = auto()
```

`run_dig_at_current_spot`, `run_pan_swap`, `run_reset`, and future `run_next_map` accept a frame source, `ServiceInputSession`, cancellation token, and deadline. Every outcome carries the exact evidence used.

Transitions:

- `DIG_PROGRESS → DIG` within a total attempt/time budget.
- `PAN_FULL → PAN_SWAP → DIG` only after verified success.
- `TREASURE_COMPLETE → SESSION_COMPLETE` in the one-map milestone.
- After E-NEXT_MAP passes: `TREASURE_COMPLETE → NEXT_MAP → NAVIGATE`.
- `NO_MAPS → SESSION_COMPLETE`.
- Ambiguous, timeout, lost-cue, or failure outcomes enter bounded reacquisition or `SAFE_STOP`.

---

## 11. Recording, telemetry, privacy, and live diagnostics

### 11.1 Evidence recorder

```text
recordings/<session-id>/
  manifest.json
  telemetry.jsonl
  chunks/000001.npz
  chunks/000002.npz
```

- Controller publishes to a bounded recorder queue without waiting.
- Compression, checksum, manifest rewrite, flush, and fsync occur on one writer thread.
- Queue overflow drops according to policy and increments visible telemetry; it never stalls control.
- Segments are checksummed and independently recoverable; the manifest is atomically replaced.
- Configurable full-rate, decimated, and pre/post-event capture tiers avoid impossible disk budgets.
- Labelled, positive, contact, arrival, manually pinned, and event-triggered chunks are protected from normal eviction.
- If only protected chunks remain, recording stops with a warning instead of deleting evidence.
- Shutdown has a bounded flush; unfinished chunks are recovered or quarantined on next start.

Initial explicit recorder bounds, stored as typed configuration with provenance:

- writer queue: 32 full-frame packets maximum;
- pre-event in-memory ring: 40 frames maximum;
- chunk: 30 frames or 256 MiB uncompressed, whichever comes first;
- whole session: 8 GiB maximum;
- protected/labelled subset: 2 GiB maximum;
- ordinary background sampling: at most 2 fps; transition/event clips may use controller rate within the ring/chunk bounds.

On queue overflow, drop the oldest ordinary non-event packet first and increment visible counters; never drop a protected packet silently. At either protected or total ceiling, stop accepting frames, finalize the manifest with `truncated=true`, and warn prominently. During Shadow/guarded commissioning, where evidence recording is required, recorder exhaustion causes a safe-stop. A later release mode may continue without raw frames only if the user explicitly chose optional recording and bounded telemetry remains healthy.

Raw recordings stay local and gitignored. Fixed chat/HUD regions are masked at write time. Moving world-space names are **not** claimed to be anonymized. Only manually reviewed sanitized fixtures may enter git.

### 11.2 GUI

Retain Tkinter/ttk, but replace the fixed minimal layout with a resizable, high-DPI-aware dashboard:

- Large Off / Shadow / Live state with distinct colors and explicit physical-arm status.
- Start Shadow, Arm Live, Stop, Pin Window, and Record. There is no actionable Tk **Start Live**, **Reset**, or **Pan Test** input button: clicking Tk necessarily removes Roblox focus. After arming, Live becomes non-clickable guidance reading **Refocus Roblox, then press F1** (or configured hotkey); reset/pan cards display **Focus Roblox, then press F4/F5**. The actual hotkeys submit their bounded coordinator intents only while Roblox is positively focused.
- Readiness cards: viewport, focus, capture freshness, profile, watchdog, deadman, ledger empty, platform gate status.
- Lifecycle card: application mode, navigation phase, recovery level/side, map state, dig/pan result.
- Profile selector and provenance/validation status. Explicit selection is default.
- Clear unsupported/pending labels rather than silently hiding unvalidated features.

Textual layout target:

```text
[ Treasure Navigator ] [ OFF | SHADOW | LIVE-ARMED ]             [ STOP ]
[ Roblox ✓ | Focus ✓ | Client 1280×720 @ scale | Deadman ✓ | Profile ]
[ Start Shadow ] [ Arm Live… ] [ Refocus Roblox → F1 ] [ Pin ] [ Record ]
[ Reset: focus Roblox → F4 ] [ Pan test: focus Roblox → F5 ]
┌ Live view / evidence ─────────────────────┐ ┌ Decision ────────────────┐
│ frame, masks, accepted/rejected boxes      │ │ FSM phase and command    │
│ player-forward and desired direction rays  │ │ confidence + frame age   │
│ motion vectors and optional relative trail │ │ recovery level/side/why  │
└────────────────────────────────────────────┘ └──────────────────────────┘
[ map lifecycle / dig handoff ] [ bounded recent events / last safe-stop ]
```

Use Studio’s dark-surface semantics through `ttk.Style`, not CSS: bone/off-white text, jade for interactive controls, gold for active/value emphasis, green/warn/red/info status colours, compact consistent spacing/radii, and platform font fallbacks. Observed facts and inferred estimates must remain visually distinguishable. Validate tab order, focus order, actual button hitboxes, resizing, and 125–200% scale behavior.

### 11.3 Live perception panel

The test/debug canvas shows the current client frame or ROI with:

- player anchor and validated forward ray;
- arrow mask, chosen/rejected components, track and confidence;
- arrow direction ray and signed screen-space error arc;
- candidate cue disagreement/abstention;
- raw/smoothed control command;
- motion vectors, inliers, progress confidence;
- contact/recovery level and locked side;
- optional relative dead-reckoned trail, clearly labelled as drift-prone—not world position.

Preview snapshots cross a size-one drop-oldest queue. Tk renders at a separate capped rate, and stale snapshots are greyed with age.

### 11.4 Read-only resources and writable data roots

Bundled defaults, UI assets, schemas, and built-in profiles are read with `importlib.resources`; shipping code never assumes the bundle or current working directory is writable. Mutable data uses a single resolved `AppPaths` contract:

- macOS: `~/Library/Application Support/Prospector Treasure/`;
- Windows: `%LOCALAPPDATA%\ProspectorTreasure\` (fail clearly if the environment/known-folder lookup is unavailable);
- development/tests only: an explicit `TREASURE_DATA_DIR` override pointing to a validated non-root directory.

Under that root, keep versioned `config/`, `profiles/`, `recordings/`, `logs/`, `manifests/`, `recovery/`, and `crash/` directories. Atomic writes use a temporary file in the destination directory plus replace. Tests run against temporary roots and assert that launch from a read-only/random working directory succeeds. No recorder, configuration, log, update, or crash-recovery path is relative to `cwd`.

---

## 12. Reuse/adapt/reject matrix

Adapt a mechanism only after reading its tests, dependencies, and licensing status. Prefer small proven invariants over importing entire subsystems. Prospector Studio’s vendored engine copies are byte-identical to the corresponding `Claude` files, so they count as one implementation—not independent corroboration—and the table cites the `Claude` copy once.

| Source and symbol | Decision | Invariant to preserve | Why/how |
|---|---|---|---|
| Treasure `engine.py`: `color_close`, existing dig/capacity sampling semantics | Adapt | Known pixel/color comparisons remain understandable and testable. | Convert to detectors over one coherent `CapturedFrame`; migrate coordinates to client space. |
| Treasure `engine.py`: `pan_swap`, reset sequence | Adapt | Existing click/key order where correct. | Make bounded services with typed results, cancellation, and `ServiceInputSession`. |
| Treasure `engine.py`: `State`, `run`, direct `mouse_*`/`key_*` calls | Reject | None. | These violate ownership, responsiveness, and cross-platform boundaries. |
| Treasure `platform_mac.py`: AX/Quartz window lookup, scale, raw event construction, hotkey logic | Adapt | Physical-pixel conversion and Roblox-compatible native events. | Refactor into `MacPlatformPort`; add client readback/focus/release-only adapter. |
| Treasure `platform_win.py`: EnumWindows, ClientToScreen, SendInput, hotkey poller | Adapt | DPI-aware client coordinates and scancode injection. | Refactor into `WindowsPlatformPort`; declare Per-Monitor V2 and remove `_eng` binding. |
| Lite `Claude/prospector_engine/input_lease.py`: `InputLedger`, `LeaseWatchdog`, `DeadmanClient` | Adapt, not wholesale copy | Independent expiry, generation invalidation, idempotent release. | Strengthen with mode capabilities and mandatory deadman ACK-before-down. |
| Lite `Claude/prospector_engine/deadman.py`: protocol and parent-death behavior | Adapt | Release-only, authenticated, independent monotonic expiry. | Move OS release primitives behind Treasure platform adapters; explicit source/frozen dispatch. |
| Lite `Claude/prospector_engine/capture.py`: `CaptureService`, `Frame`, `CaptureStall`, fake-backend seams | Adapt | One stamped capture path, freshness/stall visibility, backend recreation, input-independent stall detection. | Treasure uses one latest full-client frame thread; the UI never captures independently. Remove unrelated tracker/notification priorities. |
| Lite `Claude/prospector_engine/vision.py`: NumPy template matcher | Test oracle only | Cross-check template behavior without sharing production implementation. | OpenCV is the production backend; avoid dual production paths. |
| Lite `Claude/prospector_engine/settings.py`: `atomic_write`, schema validation pattern | Adapt narrowly | Atomic, versioned configuration with migration. | Use for profile/evaluation metadata; do not import the full Lite settings schema. |
| Lite `Claude/prospector_engine/recorder.py`: explicit consent, bounds, Secure Input checks | Reuse general principles only | Recording is explicit, visible, bounded, and local. | It records user inputs, not video evidence; do not copy it as the frame recorder. |
| Lite `Claude/engine_sim.py`: `Clock`, `TimeShim`, `InputLog`, `FakeSct`/`FakeMSS` patterns | Adapt test-harness pattern | Deterministic virtual time, frames, and input transcripts. | Build a small Treasure-specific harness; do not transplant Lite’s domain simulator or scenarios. |
| Lite `hybrid_characterization.py`, `capture_tests.py`, `input_lease_tests.py`, `platform_input_tests.py` | Adapt test cases | Before/after transcripts, stall recreation, lease expiry, deadman protocol, native event encoding. | Rewrite as focused pytest tests against Treasure contracts. |
| Prospector Studio node/scripting VM, recorder node generation, Electron UI | Reject for v1 | None required for navigation. | Major unrelated coupling and file-count growth. |
| Lite recovery/flows | Study, do not transplant | Bounded escalation and observable results. | Treasure recovery must be driven by validated arrow/motion evidence, not Lite’s water/pan cues. |
| Studio `src/ui/tokens.css` and `DESIGN_SYSTEM.md` | Inspiration only; independently implement | Clear state hierarchy, compact spacing, accessible status colours. | Do not copy proprietary CSS/text, import React, or add a UI framework. |
| Studio sidecar build/verification scripts | Independently implement equivalent build principles | Native clean builds, explicit data/hidden imports, bundle smoke, fail-loud verification. | Do not copy proprietary scripts or inherit webview/OCR/WinRT dependencies/version pins. |

Native yaw detail: move Treasure `engine.py`’s `_right_click_down`, `_right_click_up`, `_right_drag_step`, and `_right_drag_relative` into `MacPlatformPort`. Its yaw primitive must hold RMB and emit Quartz delta drag fields. Windows yaw holds right mouse through SendInput and emits relative `MOUSEEVENTF_MOVE`. The current generic macOS `move_relative` behavior is not accepted as a yaw implementation until E-YAW verifies it.

Shipping code may not import, read, or depend on paths in the `Claude` or `ProspectorStudio` worktrees. Adapted internal mechanics receive provenance in the decision log/commit. `Claude` and Treasure share an origin but that repository presently has no granted redistribution licence; Prospector Studio is a separate proprietary `UNLICENSED` repository whose beta notice forbids redistribution. Therefore:

- implementation may study the owner’s local worktrees, but public reuse/distribution is not presumed legally cleared;
- do not copy Prospector Studio source, documentation, or CSS verbatim into Treasure;
- independently implement general engineering ideas and keep clean provenance;
- before any public beta/release, the owner must confirm first-party reuse rights and choose/document Treasure’s distribution terms;
- generate third-party notices from the final native lock/wheel metadata and inspect OpenCV’s bundled native notices; third-party notices do not resolve first-party licensing.

### 12.1 Credible alternatives considered

| Alternative | Why it is not the default | When to revisit |
|---|---|---|
| Full world map/SLAM | Far more state, drift, and validation than a visible closed-loop arrow requires. | Arrow becomes intermittent for long distances and reactive recovery cannot complete representative routes. |
| Template matching only | 3D arrow scale, face, lighting, clipping, and different colours make a single template brittle. | A controlled corpus proves a multi-scale template simpler and equally safe. |
| Colour threshold only | Terrain/UI false positives and pale arrows require geometry, context, confidence, and tracking. | Never as an ungated production decision; it may remain one profile feature. |
| Neural detector | Requires a much larger labelled corpus and adds model/package governance. | Classical candidates fail with sufficient representative evidence. |
| Open-loop timed turning/walking | Ping, FPS, camera sensitivity, terrain, and collision change outcomes. | Never as the primary controller; bounded pulses remain actuator primitives inside closed-loop control. |
| Transplant Lite/Studio wholesale | Imports unrelated state, modes, dependencies, and old coupling. | No revisit; reuse only small proven invariants. |

---

## 13. File structure

Keep cohesion high without creating hundreds of files.

### 13.1 New Python files

```text
deadman.py
prospector_engine/contracts.py
prospector_engine/ports.py
prospector_engine/coordinator.py
prospector_engine/input_authority.py
prospector_engine/capture.py
prospector_engine/vision.py
prospector_engine/motion.py
prospector_engine/navigation.py
prospector_engine/telemetry.py
```

Ten new Python modules total keep the branch compact:

- `contracts.py`: frozen messages, enums, shared protocols, units.
- `ports.py`: platform-port protocol/factory only; no OS imports during opposite-OS import tests.
- `coordinator.py`: intent loop, mode/generation ownership, worker lifecycle.
- `input_authority.py`: capability sessions, ledger, watchdog, deadman client, global release.
- `capture.py`: canonical viewport guard, coherent frame source, freshness/stall handling.
- `vision.py`: arrow profiles, segmentation/tracking, direction strategies, arrival detection, internally sectioned.
- `motion.py`: candidate motion estimators, confidence, progress/contact evidence.
- `navigation.py`: navigation FSM, steering, bounded recovery.
- `telemetry.py`: immutable snapshots, bounded latest queue, segmented recorder.
- root `deadman.py`: release-only helper.

Do not split one class per file. Split a module only if it becomes genuinely difficult to review (for example, substantially beyond roughly 800 cohesive lines or with proven circular ownership), and document the reason.

### 13.2 New non-code/package data

```text
pyproject.toml
CLAUDE.md
README.md
requirements-macos.lock
requirements-windows.lock
prospector_engine/profiles/arrow_profiles.json
prospector_engine/profiles/evaluation_spec.json
prospector_engine/assets/arrival/
packaging/treasure.spec
packaging/build_macos.sh
packaging/build_windows.ps1
packaging/verify_bundle.py
tests/fakes.py
tests/test_characterization.py
tests/test_capture_input.py
tests/test_vision.py
tests/test_navigation.py
tests/test_runtime_concurrency.py
tests/test_platform_contract.py
tests/test_replay.py
tests/test_packaging.py
tests/fixtures/
```

Profiles/templates live beneath the existing importable `prospector_engine` package and load through `importlib.resources.files("prospector_engine")`; they are never resolved relative to the current working directory or a reference worktree.

### 13.3 Existing files modified

- `treasure.py`: argument dispatch; `--deadman` before heavy imports; source/frozen entry.
- `treasure_gui.py`: coordinator-owned app shell, dashboard, telemetry queues, physical Live arm.
- `engine.py`: bounded legacy services and coherent-frame detectors; remove global run ownership and OS imports.
- `platform_mac.py` / `platform_win.py`: instance ports, viewport/focus/raw input/release/hotkeys.
- `prospector_engine/__init__.py`: exports/version only after gates pass.
- `.gitignore`: local recordings, build output, caches; preserve user-owned venv backup.

`complexion.md` is marked historical/stale in the README; do not rely on it as architecture truth.

---

## 14. Dependencies, tools, and packaging

### 14.1 Runtime dependencies

| Dependency | Platform | Purpose |
|---|---|---|
| `numpy` | all | arrays/math |
| `mss` | all | capture |
| `opencv-python-headless` | all | morphology, flow, RANSAC, templates |
| `Pillow` | all | Tk preview |
| `pyobjc-framework-Quartz` | macOS | input/capture APIs |
| `pyobjc-framework-ApplicationServices` | macOS | AX window/focus |
| `pynput` | macOS | global hotkeys |

`pyautogui` is removed.

### 14.2 Development/build dependencies

`pytest`, `pytest-timeout`, `pytest-benchmark`, `hypothesis`, `ruff`, `mypy`, `pip-tools`, and `PyInstaller`. Hypothesis is used only for the state-machine/invariant properties named in §16.2; deterministic scenario tests remain the primary debugging surface.

Generate platform locks with `pip-compile --generate-hashes` from `pyproject.toml` extras. Clean verification installs the platform lock with `pip install --require-hashes -r requirements-<platform>.lock`, then installs the project itself with `pip install --no-deps -e .`.

### 14.3 Packaging

- Build separately in clean native macOS and Windows environments.
- PyInstaller includes profiles/templates through package data and supports `sys.executable --deadman` when frozen.
- Windows manifest declares Per-Monitor V2 DPI awareness.
- macOS UI explains Accessibility and Screen Recording requirements.
- Smoke test GUI launch, resource loading, deadman dispatch, pin/readback, permissions, version metadata, and clean shutdown.
- Emit dependency/NOTICE list and SHA-256 artifact hash.
- Report Windows signing and macOS signing/notarization independently; `pending` is acceptable, pretending is not.

Build scripts are host-guarded, create isolated build environments, install the matching hashed lock, probe `import tkinter, _tkinter, cv2, mss, numpy, PIL`, run local gates, freeze, execute `--smoke-test` and a deadman file-sink test, and verify resource/binary closure. No GitHub workflow is required merely to satisfy the word “CI.” Local validation scripts plus native manual gates are sufficient unless automated remote runners are deliberately added later.

---

## 15. Implementation phases and rollback points

Every phase has three independent statuses:

- **Local implementation/replay exit**: can be completed on the current machine without claiming native behavior elsewhere.
- **Native commissioning**: macOS and Windows evidence tracked separately; either may be pending.
- **Live/release eligibility**: blocked only for the affected OS/profile/condition until its native gates pass.

### Phase 0A — Characterization

- Add test harness and deterministic fake frame/input/clock.
- Capture correct current dig, pan, reset, Stop, and F1–F5 expectations.
- Record known failures B1–B13 explicitly rather than blessing them.

Local exit: characterization suite green.  
Rollback: test-only checkpoint.

### Phase 0B — Platform ports and viewport

- Remove shared OS imports and module-global binding.
- Create instance Mac/Windows ports.
- Implement canonical client pin/readback/focus/hotkeys.
- Fix B1, B9, B10, B11.
- Migrate/reverify legacy pixels.

Local exit: mocked opposite-OS import and viewport tests green.  
Native commissioning: E-VIEW separately on macOS/Windows; may be pending.  
Live: disabled.

### Phase 0C — Input authority and deadman

- Add sole ledger, capability sessions, watchdog, global release, deadman.
- Fix B5 and B8.
- Test ACK-before-down, expiry, stale generations, helper crash/restart, parent death.

Local exit: deterministic safety/concurrency suite green.  
Native commissioning: real up-events and force-kill on each OS.  
Live: disabled.

### Phase 0D — Coordinator migration

- Add priority coordinator and exactly one cancellable mode worker.
- GUI/hotkeys submit intents only.
- Fix B3, B4, B6, B7, B13.

Local exit: Stop latency under injected stalls; simultaneous intents cannot interleave input.  
Live: disabled.

### Phase 0E — Coherent bounded legacy services

- Add one stamped frame path.
- Convert dig, reset, dequip, and pan swap to typed bounded services.
- Fix B2 and B12.
- Re-run all characterization tests.

Local exit: all B1–B13 regressions green and ledger empty after every scenario.

### Phase 1 — Shadow foundation and GUI

- Implement coherent capture, telemetry, evidence recorder, responsive dashboard, and diagnostic canvas.
- No navigation input exists yet.
- Record sanitized manual routes using each available profile/condition.

Local exit: replayable recording with bounded memory/disk and nonblocking preview.  
Native commissioning: manual recording on each OS when available.

### Phase 2 — Offline perception/evidence

- Collect/fix candidates for E-ANCHOR, E-FORWARD, E-DIR-IDEAL, E-PROF, E-ARRIVE, E-MOTION, E-DIG, and initial E-NEXT_MAP on training/validation data.
- Do not run held-out gates yet; independent actuator limits, absolute perception budgets, coverage/recall floors, and evaluation specs are not frozen.
- Keep every failed feature visibly unsupported.

Local exit: candidate implementations, labelled splits, and draft evaluation manifest are reproducible.  
Live: disabled.

### Phase 3 — Shadow navigation and controller

- After Phase 0 safety gates, perform physically armed bounded E-YAW and manual-target E-STEER-CAL characterization without using production arrow/forward estimators.
- Freeze actuator interval, perception budgets, deadband, coverage/recall floors, and absolute evaluation specs; run E-ANCHOR, E-FORWARD, E-DIR-IDEAL/E2E, E-PROF, E-ARRIVE, E-MOTION, and lifecycle held-out gates once.
- Implement navigator FSM, controller, recovery simulation, and complete live diagnostic overlay only for passing conditions; then run E-RECOVERY replay and guarded native gates.
- Replay routes deterministically with zero emitted OS input.
- Complete E-STEER-E2E using guarded native trials with the already-frozen controller; do not retune on those trials.

Local exit: deterministic traces and property gates green.  
Native commissioning: guarded open-ground trials per OS.  
Live eligibility: selected OS/profile only after its gates pass.

### Phase 4 — One-map Live lifecycle

- Run NAVIGATE → ARRIVAL → DIG → PAN_SWAP as needed → SESSION_COMPLETE.
- Automatic recovery requires both E-MOTION and E-RECOVERY pass for the exact OS/profile/condition.
- Automatic next map remains off.

Native exit: bounded routes, zero input leaks, no false transitions in named corpus, owner-observed private-server trials.

### Phase 5 — Multi-map lifecycle

- Finish E-NEXT_MAP and enable only validated inventory/equip/profile outcomes.
- Run full lifecycle through multiple maps.

Failure fallback: retain one-map mode; never infer next-map state.

### Phase 6 — Packaging and release commissioning

- Clean native builds, smoke tests, permissions, artifacts, hashes.
- 2-hour then 8-hour soak on each supported OS/profile.

Release requires zero stuck inputs, zero unbounded loops, bounded memory/disk, complete mode/event log, and all claimed native gates proven.

**G-LICENSE (public-distribution blocker, not an implementation blocker):** before publishing a beta or installer, the owner confirms rights to every adapted first-party source, adds the intended first-party licence/terms, updates publication language accordingly, and verifies that no proprietary Prospector Studio material was copied. Until then, builds may be private/internal only and must not be described as open source or redistribution-authorized.

---

## 16. Verification matrix

### 16.1 Local gates

- `pytest`: unit, characterization, deterministic replay, golden sanitized fixtures, concurrency, deadman, lifecycle.
- `ruff check` and `ruff format --check`.
- `mypy --strict prospector_engine` on the new and modified engine package.
- Import tests with mocked Mac and Windows adapters.
- No OS input in tests unless a test is explicitly marked native and physically armed.

### 16.2 Required invariant tests

- No lease survives its deadline.
- `release_all()` is complete and idempotent under concurrent calls.
- Every native down/pointer/scroll edge is ordered against Stop by the shared edge barrier; boundary-race tests produce no post-Stop edge.
- Repeated renewal never extends expiry beyond the configured rolling horizon from the latest deadman ACK.
- A partial local release failure still attempts the complete vocabulary and deadman release-all, latches uncertainty, and blocks Live.
- Stale generations cannot press or renew.
- Deadman ACK occurs before a down-edge.
- Stop remains responsive while capture/recorder/mode worker stalls.
- Packaged shutdown remains bounded with a permanently stalled daemon component.
- Exactly one input-emitting worker/mode exists.
- Late worker completions with mismatched generation/mode/worker ID cannot transition state.
- Live arm proof is one-use and survives only its accepted `START_LIVE` transition; expiry, duplicate/unrelated intent, and readiness failure require re-arm.
- All retry/recovery/lifecycle loops terminate.
- No stale or abstained observation renews input.
- Repeated frame sequences cannot extend a command lease.
- Angle wrapping works around ±180°.
- Recovery side lock cannot flip inside cooldown.
- Frame/telemetry contracts are actually read-only.
- Recorder overflow cannot block control.
- One map produces at most one arrival latch.
- `ABANDONED` safe-stops unless separately gated E-SKIP_MAP is explicitly enabled.

### 16.3 Native matrix per OS

| Area | macOS | Windows |
|---|---|---|
| Client pin/readback | Retina, non-Retina/scaled, legal origin, AX permissions | 100/125/150/200% DPI, Per-Monitor V2 |
| Capture | Screen Recording, p50/p95, stale/frozen behavior | MSS timing, DPI physical pixels |
| Input/yaw | Quartz scancode/mouse delta, focus | SendInput scancode/mouse delta, focus |
| Hotkeys | F1–F5 | F1–F5 |
| Stop/deadman | Stop, focus loss, force-kill, helper death | Same |
| Packaging | `.app`, resources, permissions, deadman | `.exe`, manifest, resources, deadman |

An opposite-OS import test is useful but is never reported as proof of native behavior.

### 16.4 Release metrics

Per-frame IoU is diagnostic. Release is based on episodes/sessions:

- accepted wrong-turn episodes;
- false high-confidence arrow acquisitions/hour;
- track switches and longest loss;
- false arrivals/hour;
- false contacts/recoveries/hour;
- route and recovery success;
- oscillation/overshoot;
- input-safety violations;
- Stop/release latency;
- capture/control p95 and stale rate.

No aggregate pass may hide a failing profile, OS, or camera/display condition.

---

## 17. Human-readable engineering standard

- Declare a Python support range only after native wheel, `_tkinter`, PyInstaller, PyObjC, and OpenCV verification. The ambient Python 3.8 is unsupported; build scripts use an explicit verified interpreter rather than `python3` by accident.
- Type annotations on public/internal contract boundaries.
- Frozen dataclasses/enums for messages and outcomes; no stringly typed lifecycle.
- Small, descriptive functions; explicit units in names (`_px`, `_ms`, `_s`, `_deg`).
- Docstrings explain invariants and failure behavior, not obvious syntax.
- Comments explain why a constraint exists and cite the experiment/bug ID where useful.
- Configuration values live in typed/versioned profile/config data with provenance; no unexplained magic numbers.
- No circular engine↔platform binding, wildcard global injection, or feature-to-native calls.
- Errors become typed outcomes at subsystem boundaries; unexpected exceptions safe-stop at the coordinator boundary.
- Logs use stable event names plus structured fields; UI text is derived from the same snapshots.
- Public docs distinguish observed fact, estimate, provisional configuration, validated support, and pending evidence.

### Proposed `CLAUDE.md` content

After this plan is accepted, create a concise repository-specific file manually from verified commands and invariants. `/init` is optional and adds little value here; do not accept generic generated boilerplate as authoritative. The reviewed file contains:

- exact macOS/Windows setup and lock-respecting commands;
- architecture boundaries and sole-input-authority rule;
- safe local test commands;
- physical Live-arming prohibition for agents;
- immutable contract/style conventions;
- user-owned `.venv-python38-backup/` preservation;
- gate terminology and rule that pending native evidence is never invented;
- no destructive git commands and no unrelated repo cleanup.

Do not put volatile measurements or giant architecture essays in `CLAUDE.md`; link this plan and the evaluation manifests instead.

---

## 18. Implementation-agent operating instructions

1. Read this entire plan and current Treasure files. Because `CLAUDE.md` does not yet exist, the first preflight edit is to draft it from §17, verify every command/invariant against the repository, and reread the reviewed file before any feature-code edit. On later sessions, read the existing `CLAUDE.md` first.
2. Inspect reuse sources directly; do not copy a subsystem based only on this table.
3. Keep existing user changes and `.venv-python38-backup/` untouched.
4. Work one phase/checkpoint at a time, with tests before the next checkpoint.
5. Make reasonable local implementation decisions autonomously and record them in a short decision log.
6. Do not ask the owner for routine naming, formatting, or reversible implementation choices.
7. Do not operate Roblox or arm Live. Native/Live gates that require the owner remain pending with exact instructions.
8. Do not claim Windows behavior from a Mac, or macOS behavior from mocked tests.
9. Do not add workflows, MCP integrations, databases, web services, or new frameworks unless a demonstrated requirement cannot be met otherwise.
10. Stop only for genuinely missing authority, destructive scope, or an architectural conflict that cannot be resolved from this plan and the code.

The implementation is complete only when the code and documentation state precisely which OS/profile/lifecycle features are validated, guarded beta, unsupported, or pending.
