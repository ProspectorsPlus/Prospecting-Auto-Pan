# Status

Per-phase and per-gate status. Three columns, because they fail independently
(plan §15): what can be finished on this machine, what needs macOS hardware,
and what needs Windows hardware.

Last updated: 2026-08-29 (tenth pass, movement lifecycle and native probe).
Development machine: macOS 25.4, arm64, CPython 3.13.15, Tk 9.0.
Live was never started and no Roblox session was operated during
implementation. The tenth pass **did** send bounded input to the Roblox client
under `--native-control-probe`, with explicit owner authorization: one key edge
per trial with a `finally` release, and scroll-wheel events. Nothing was
clicked, joined, bought or changed; measurements are in D-074.

---

## MOVEMENT: WHAT IS FIXED AND WHAT IS STILL UNPROVEN (2026-08-29, tenth pass)

**Still unproven: that `W` moves a character.** It could not be tested tonight.
The Roblox client was running but sitting on its **home page** with no world
loaded — the player process had gone to tray at 01:34 and there was no
character to move for the rest of the session. Every `W` trial therefore
returned an honest "no motion", which is a true negative about the home page
and says nothing about the game.

**Proven tonight, natively, against the real client** (see D-074):

* This process's CGEvents **do reach Roblox and Roblox does act on them** —
  scroll moved the page by 140-173 px through both event taps, repeatedly.
* `CGEventPostToPid` reaches Roblox **not at all**: 0 of 8 trials, median
  |dy| 0.7 px against 229.6 px for the HID tap. It is ruled out as a backend.
* The key-state loopback that failed every run last night reads **`True`** when
  it is read 80 ms after the edge instead of in the same breath as the post.
  The gate was measuring scheduling, not receipt.

**Fixed, with regressions, in the lifecycle above the backend** (D-069 to
D-073): the prologue no longer disarms the actuator on its own success path;
cadence is no longer an authorization; the loopback no longer preempts the
motion evidence; an ambiguous focus reading no longer releases; a terminal
fault now leaves LIVE visibly instead of leaving a zombie; and a superseded
worker can neither press nor release.

### What the owner has to do for the last link

Leave Roblox **in a world, with a character standing still**, then run:

```sh
.venv/bin/python treasure.py --native-control-probe --key w,a,s,d --hold-ms 600
```

It sends bounded input, releases on every path, and needs no map, profile or
setup. If W moves the character, the backend question is closed and the
follower can be armed with Ctrl+N. If it does not, the probe's own numbers say
whether the edge went out, how long it was held, and how much the world moved.

---

## The ninth pass, and what it cleared (2026-08-29)

Treat the section below as ground that was cleared rather than as a fix that
worked: the character still did not move after it.

### Ruled out, by measurement, not by argument

| Claim | How it was ruled out |
|---|---|
| macOS blocks us / the laptop is at fault | Posting keycode 105 (F13) through Treasure's own `platform_mac._post`, with Roblox frontmost, and reading it back with `CGEventSourceKeyState`: `before False -> down True -> up False`. `CGPreflightPostEventAccess()` is `True`. |
| The Quartz call or the keycodes are wrong | Byte-identical to Prospector Lite's: `CGEventCreateKeyboardEvent(None, code, bool)` posted to `kCGHIDEventTap`, and W is 13 in both. Asserted in `tests/test_platform_contract.py`. |
| The actuator cannot press | `tests/test_movement.py::test_the_real_platform_port_actually_presses_a_key` (marked `native`) drives the real `MacPlatformPort` through the real actuator and reads the edge back off the window server. It passes. |

So the process **can** put a key down on this machine. What has never been
observed is that key reaching **Roblox**: `GAME_MOTION_CONFIRMED` has still
never appeared in any trace.

### Already removed — do not go looking for these again

Nine separate conditions that each stopped a healthy machine were found and
removed in passes eight and nine. The ones that mattered most (all in D-062,
D-065, D-067):

* the hidden two-gesture arm (`live.refused: no arm token`);
* `release_all` closing admission, so the first ordinary "stop walking" muted
  the session permanently — *and* `release_navigation` was wired to it;
* `DeadmanHelper.release_all` bumping its own generation, so every later
  registration returned `stale-generation`;
* `release_navigation("prologue-complete")` tripping both of the above on the
  success path of every healthy start, before the first navigation frame;
* the processed-fps gate reading 0.0 on the first two frames and releasing;
* `cursor_safe` and the focus probe both treating **unknown** as a refusal,
  when the macOS probes return `None` on any error or ambiguity.

### Where to start

1. Run the app and read the **WHAT IT IS DOING** panel. It is a plain-English
   running commentary and it is the point of the ninth pass: whatever stops
   movement now should have a sentence in it. If it does not, that gap is
   itself the bug — the sentence belongs where the reasoning is.
2. `treasure.py --forward-probe 600` with Roblox frontmost. It presses W once,
   holds it, and prints the causal chain naming the first missing transition.
   **This is the only command that sends input.**
3. The next unproven link is the one between `OS_EDGE_POSTED` and the game.
   Worth checking before anything else: whether Roblox accepts synthetic HID
   events at all in the current client/build, and whether Lite's *sequence*
   around a press (window activation, a click into the client first, timing)
   differs from ours. Lite works on this machine, so a difference exists and it
   is no longer in the primitive.

Nothing in `prospector_engine/movement.py` has been validated against a real
character. Its unit and native tests prove it presses keys, not that it walks.

---

## What changed on 2026-08-28 (eighth pass) — the gesture nobody was told about

### The root cause, read out of four traces

`logs/stop-epoch3-1900021861.jsonl`, `stop-epoch3-1900181519.jsonl`,
`stop-epoch3-1900184259.jsonl` and `stop-epoch4-1900065407.jsonl` all contain
the same three rows and nothing after them:

```
chord_recognized      Ctrl+N
intent_queued         START_LIVE from hotkey
live.refused          no arm token
```

No `arm.created`, no `ARM_TOKEN_CONSUMED`, no LIVE transition, no `W_REQUESTED`,
no `OS_EDGE_POSTED`, no lease, no camera delta. **The failure happened before
the input backend was reached**, which rules out Quartz, focus, Roblox, camera
calibration and hold timing in one reading.

Live required a click on an **Arm Live** button *and then* the chord. The
window's own message line said only *"Ready. Focus Roblox and press Ctrl+N"*.
The two-gesture protocol existed nowhere a user could read it.

| Reported | What the measurement says | Fix |
|---|---|---|
| Ctrl+N does nothing | Confirmed and located: the chord is heard, the intent is queued, and the coordinator refuses it for a token no gesture in the UI's own instructions creates | one gesture; the chord mints and consumes its own authorization in one transaction, and must carry a `PhysicalChordProof` only the listener can produce (D-062) |
| The GUI says Ready while the coordinator refuses | Confirmed: the message line is derived from `progress.ok`, the refusal from `arm_token()`; nothing compared them | one derived `RunState`, read from `coordinator.blockers()` and `live_authorization`, rendered by both the badge and the message line (D-062) |
| The keys are held, not tapped — and still nothing moves | The hold *mechanism* was right after D-060; the hold *horizon* was not. A lease was granted `min(250 ms, what is left of this frame's 100 ms budget)`, so a command from a 70 ms-old frame asked for 30 ms, the next frame arrived after 33 ms, the watchdog lifted the key and the next command pressed it again | the horizon is the plan's own `max_rolling_lease_horizon_ms`, and a separate `max_capture_stall_ms` bounds a hold between frames (D-063) |
| A brief occlusion stops the route dead | Confirmed: the grace was **two frames** — 33 ms at 60 fps — and a rejected *heading* had no grace at all | a 2 s duration grace covering both losses, coasting on the last stable heading with yaw released; past it, a bounded reacquisition episode rather than standing still |
| 796 of 800 event rows are one repeated status line | Confirmed: 791 `worker.status` rows in `stop-epoch3-1900184259.jsonl` | per-frame chatter rate-limited on the way in; milestones kept in a second ring chatter cannot evict |
| The dashboard cannot say why nothing is moving | Confirmed: it showed the mode and the leases and nothing else | eight measured rows read from the ledger — listener health, authorization, keys held, forward hold duration with edge counts, last posted yaw, backend, last confirmed displacement, and the blocking reason |

### Found while fixing, not reported

* `treasure_gui.py` still read `snapshot.arm_state` in the INPUT SAFETY card
  after the field was removed. Nothing reached that branch in a test — the
  fixture always had a standing blocker — so an `AttributeError` was sitting
  on a path that runs several times a second the moment a session becomes
  unblocked. Fixed, and every branch of the card is now covered.
* `LifecycleJournal.note` raised `TypeError` on a `detail=` keyword collision,
  on the coordinator thread, inside the handler for the event being recorded —
  so a mistyped diagnostic safe-stopped the run it was describing (D-061).

### Edge-count evidence (fakes, deterministic)

`tests/test_hold_continuity.py` drives frames through the real navigation
session and counts edges at the platform port:

| scenario | down edges | up edges |
|---|---|---|
| 30 frames of "keep walking" | 1 | 0 |
| 6 gaps of 120 ms (over the evidence budget, under the hold budget) | 1 | 0 |
| the same 6 gaps, against the pre-D-063 horizon | 1 | **2** |
| a deliberate release, then walking again | 2 | 1 |
| a stall past `max_capture_stall_ms` | 1 | ≥1, ledger empty |

Measured continuous hold in the end-to-end test
(`tests/test_live_start_to_stop.py`): **> 100 ms** across thirty-odd applied
commands with corrective yaw in both signed directions posted while forward
stayed down, one down edge, no up edge until Stop.

**None of this is native evidence.** It is a fake platform port recording what
it was asked to post. See the movement gate below.

### One native measurement that does bear on it

`treasure.py --shadow-bench 12` against the live client, 2026-08-28. Read-only:
no window moved, no input sent.

| | consumed fps | unique fps | capture-to-observation p50 / p95 / p99 / max | cpu | rss |
|---|---|---|---|---|---|
| capture only | 56.8 | 56.8 | 5.1 / 6.9 / 9.0 / **131.2** ms | 54% | 109 MB |
| capture + headless perception | 55.9 | 56.2 | 11.5 / 19.7 / 25.7 / **206.6** ms | 106% | 189 MB |

Client was `adopted_noncanonical: 1800x1053 pt` (the owner's current window;
automatic setup would fit it), backend `screencapturekit`, profile
`yellow_map_v1`.

**Why this is evidence for D-063 rather than a throughput note.** The *old*
lease horizon was `max_evidence_age_ms` minus the frame's age — with a p50
observation latency of 11.5 ms that is about 88 ms, and at the p99 about 74 ms.
The measured worst-case gap between observations on this machine is **206.6
ms**. So the old horizon was smaller than the real tail by a factor of two to
three, on the owner's own hardware, with nothing else running. The rattle was
not a possibility; it was the expected behaviour of those two numbers.

206.6 ms is also inside `max_capture_stall_ms` (250 ms) with little room, which
is worth watching: a machine with a longer tail than this one will release the
key rather than rattle it, which is the correct failure but still a failure.
Sustained cadence is comfortable — 55.9 processed fps against a
`min_processed_fps` of 30.

E-PERF stays **PENDING**: this is one machine, one window size, one session.

---

## What changed on 2026-08-28 (seventh pass) — the listener, the pulse, the tracker

Three defects, each measured here before it was touched, each fixed, and one
of them not what it was reported to be.

### Root causes, confirmed

| Reported | What the measurement says | Fix |
|---|---|---|
| The macOS hotkey listener is broken by a pynput callback-arity `TypeError` | **Half right, and the diagnosis was wrong.** pynput 1.8.2's `Listener._wrap` inspects the signature and adapts a one-argument callback, so no `TypeError` occurs — verified by calling `listener.on_press(key, False)` against a one-argument bound method. The real mechanism is the `False` return: `AbstractListener` raises `StopException` on it, and the adapter returned `False` for every unrecognized key, **including a lone Ctrl**. The listener died on the first keypress of the session. | direct Quartz event tap; callbacks that cannot signal "stop" at all (D-050) |
| Live enters a prologue and never reaches W | Confirmed exactly. `logs/stop-epoch7-1886181997.jsonl` has its last frame at `1886148.84` and was written at `1886181.997` — **33.15 s** later, the `characterize_turn` deadline plus poll slack. No `turn-response.json` has ever been written. | `VERIFY_INPUT`: one bounded forward pulse, confirmed causally, before any camera stage (D-052) |
| `OS_EDGE_POSTED` exists as an enum and is never emitted | Confirmed: one definition, one docstring reference, one test, no emission | the authority writes it where `_emit_down` returns (D-054) |
| Motion is evaluated on the same pre-command frame, and `abs(speed) > 0` counts noise | Confirmed, both, in one line | `ForwardMotionWitness`: post-edge frames only, threshold measured from this session's idle noise (D-053) |
| The GUI says movement is being sent for every LIVE state | Confirmed: `Navigating. Press Stop at any time.` for the whole input-free prologue | each armed stage has its own sentence, with a test that every one has one (D-055) |
| Stop JSONL omits the hotkey, arm, worker, prologue, OS-edge, lease and motion lifecycle | Confirmed: every stop trace in `logs/` contains exactly `frame`, `preview`, `governor` | sixteen named stages, appended with the raw event stream (D-054) |
| The keys are tapped, not held — *"it taps it so fast and for so short it either doesn't register or moves like 10 atoms forwards"* (owner) | **Confirmed, and it explains the camera stage too.** A command may not outlive its evidence, and the frame a decision is taken on is already 20–45 ms old (measured `capture_to_observation_ms` 44.86), so the longest hold a *single* command can express is 55–80 ms. Every camera probe was one command, so `key_probe_ms`'s top rungs were unreachable and all sent the same short press. The acceptance pulse written earlier in this pass asked 160 ms of one command and was **rejected outright** — it would never have pressed W at all | probes hold by renewing against each newer frame, as navigation already does; ladder raised to 60–320 ms, pulse to 320 ms (D-060) |
| The tracker blacks out after fast movement | Confirmed and reproduced to the millisecond at 60 fps: 100 px → 67 ms, 180 px → 233 ms, 250 px → 367 ms, 400 px → 567 ms + a new identity; scale 70→160 → 567 ms + a new identity | `_resume_outside_gate` (D-058) |

### What was measured, before and after

Tracker recovery at 60 fps on rendered frames (**training stress, never a
gate** — plan §7.2). Reproduce with `treasure.py --tracking-report`:

| family | before | after |
|---|---|---|
| jump 100 px | 67 ms | 0 ms |
| jump 180 px | 233 ms | 0 ms |
| jump 250 px | 367 ms | 0 ms |
| jump 400 px | 567 ms, new identity | 0 ms, same identity |
| scale 70→110 | 200 ms | 0 ms |
| scale 70→130 | 367 ms | 0 ms |
| scale 70→160 | 567 ms, new identity | 0 ms, same identity |
| scale 70→200 | 567 ms, new identity | 0 ms, same identity |
| sweep 25°/frame | 200 ms, **179.9° heading error** | 0 ms, 0.3° |

Real-frame corpus, `tune` and `eval`, before and after: **every sequence
identical** — recall, false locks and identity switches unchanged on all
fourteen. That is the expected result: the corpus is sampled at about 5 fps and
resuming is bounded to 60 ms, so it cannot reach the regime this fixes. The
tuning decision was taken on `tune` alone; `eval` was read to report.

Corpus totals, unchanged: recall 80.2%, absent-precision 90.0%, false-lock
2.6%, median heading error 10.4°, p95 104.6°, direction sign 90.9%.

### What is *not* done, stated plainly

* **No optical-flow bridge, no ROI tracker, no covariance state, no
  `FLOW_BRIDGED` / `PREDICTED_ONLY` provenance.** The measured blackout was an
  association-rule problem and is entirely gone without a second tracker.
* **The tracker acceptance gates cannot be run.** They need dense green-arrow
  sessions at 60 fps; `green_arrow_v1` has seven owner crops and the committed
  real corpus is yellow-only at 1–5 fps. This is a missing-recording blocker,
  not a missing-code one.
* **A tightness this pass exposed and deliberately did not fix.** The lease
  window one command may ask for is `max_evidence_age_ms` minus the frame's
  age — about 55 ms with the measured pipeline latency — so a renewal chain
  stays continuous only while frames arrive faster than that, roughly 18 fps
  against a `qualify_min_fps` of 20. There is no margin. Lengthening the lease
  would be weakening a safety bound, so instead the consequence is counted: a
  key re-pressed within 500 ms of coming up raises `HOLD_LAPSED`, and the
  drawer shows the tally. A hold that is rattling now says so.
* **A detector weakness this pass did not fix:** a same-coloured blob landing
  near the arrow's last position is accepted by the ordinary positional gate.
  Two of nine rendered clutter layouts show it, with resuming off as well as
  on, and it is what `sand-near-a`'s 26.7% false-lock rate is.

---

## What changed on 2026-08-28 (sixth pass) — merge, Live, chords, overlay

The fifth pass did structural work and did not touch the four systems that
were actually broken. This one does, and merges the friend branch.

### origin/Treasure, merged

Nine commits, merged rather than rebased. One conflict, in `treasure_gui.py`,
and it was semantic: the fifth pass had moved the composition root out to
`prospector_engine/application.py`, so git saw "deleted here, modified there"
for the whole `build_application` block — the shape of conflict where a hand
resolution quietly drops someone's work. Their three edits were ported into the
new root by hand and each of the nine is now pinned by a behavioural test
(`tests/test_integration_merge.py`): first-capture reacquisition, UNPINNED
healing, `on_safety_fault`, worker completion reasons, `TREASURE_VERBOSE`,
`savePatel.md`, and the egg-info untracking.

### Root causes confirmed and fixed

| Reported | Confirmed mechanism | Fix |
|---|---|---|
| The UI cannot tell WOULD_APPLY from APPLIED | `context.on_observation()` ran *before* `apply()`, so the packet was published before the authority answered | act, then publish; `CommandVisualization` reads `leases_held` (D-044) |
| No purple command renderer | there was none | purple action layer, dark underlay under a vivid stroke, boxed key labels, dashed WOULD / solid ACTIVE (D-044) |
| MINIMAL calls `_draw_cue_arms` unconditionally | it did — and the bbox, centroid, shaft and tip were never gated at all | all internals are Full-only; `set_mode` hides what is already drawn (D-045) |
| A TERMINAL packet still draws a live-looking field | frozen packets kept every vector, and Full's 20 Hz overlay throttle could keep **ACTIVE** on screen with no next frame to clear it | frozen packets go flat grey, lose every internal, clear the action layer, and are never throttled (D-045) |
| macOS and Windows only implement F1–F6 | two tables, declared twice, spelled out a third time in the GUI | one registry, Ctrl+Option / Ctrl+Alt chords, shared `ChordRecognizer` (D-046) |
| "0 useful fps" then downshifts to 15 Hz | `wait_for_new` latched `_has_consumer` forever; setup and the prologue read frames without ticking the processed rate; the governor read a real zero against a consumer that no longer existed and walked 60 → 30 → 15 Hz, under the 30 steering requires | scoped consumers, measurement separated from consumption, no resting below the Live floor (D-047) |
| Arm tokens spent on transient failures | the token was consumed before the readiness check by design | focus and capture freshness keep the arm; a real fault still spends it |

### Two defects only the live client showed

**Automatic setup failed intermittently with `capture_stale` on a correctly
fitted window.** `window_geometry()` can only ever report
`ADOPTED_NONCANONICAL`; `CANONICAL_VERIFIED` is the guard's verdict. Comparing
full identities compared the verdict against the reading on every poll after a
successful fit and called it a mismatch, while the unchanged branch separately
overwrote the verdict. The result was a revision storm — about forty state
flips a second, the supervisor rebuilding the source on each — with in-flight
frames from stopped sources carrying the pre-fit rect and re-poisoning the
guard. Measured: `capture_stale` on **4 of 6** native runs before, **0 of 6**
after (D-048).

**A recovery record that could never be escaped.** The persisted record's own
evidence read `deadman_acknowledged: True, ledger_empty: True, failures: []`
and it still blocked Live, run after run: `shutdown()` persisted on
`release_known_safe`, which is also false while an *earlier* run's latch is
set, so every run re-wrote the record from a perfectly clean release. One
uncertain shutdown poisoned the machine permanently. The preflight's remedy
compounded it by naming Stop & Release, which cannot clear a record (D-049).

### Permissions, measured — the failure was never a permission

`AXIsProcessTrusted`, `CGPreflightPostEventAccess` and
`CGPreflightListenEventAccess` all read **True** for this launcher (Terminal).
`prospector_engine/preflight.py` now reports them as typed capabilities,
separating *faults* (a denied permission, a dead listener, stuck input) from
*preconditions* (not armed, Roblox not focused, cadence too low), and naming
the launcher that owns the grant and the exact System Settings pane.

### Native, this pass

`.venv/bin/python treasure.py --setup-probe`, six consecutive runs after the
fixes: fit requested and achieved **1280×720 pt / 2560×1440 px** every time,
origin preserved at (0,67), one capture restart, **zero input edges held**,
window restored to 1800×1053 every time. All six stop at `SELECT_PROFILE` with
`profile_ambiguous` — the correct answer, because **no treasure map is
equipped**: on a frame with no arrow, `yellow_map_v1` and `green_arrow_v1`
genuinely are indistinguishable (within 0.02) and a classifier that picked one
would be guessing.

### What the owner still owes — and it is short

Everything below needs a physical human at the machine. **No agent may
simulate the start chord** (CLAUDE.md rule 1).

1. Equip a treasure map so its arrow is on screen.
2. `treasure.py --setup-probe` → expect **READY**. This is the first time
   `ESTABLISH_REFERENCE` and `SHADOW_QUALIFY` will have run on real frames.
3. `treasure.py --forward-probe 600` with Roblox frontmost. **This presses a
   key.** It prints the causal chain and names the first missing transition;
   run it before anything else, because nothing downstream can work if
   `GAME_MOTION_CONFIRMED` is missing here.
4. Open the dashboard. Confirm the header reads
   **READY - CTRL+N STARTS MOVEMENT** and that the LIVE READOUT's
   *Ctrl+N listener* row says it is hearing keys.
5. Focus Roblox and press **Ctrl+N**. That is the whole gesture (D-062);
   there is no button to click first.
6. Watch the prologue characterize the turn actuator, then the route.
7. Press **Ctrl+X** to stop, from an unfocused window as well.

Record, for each: the chord being received, the authorization created and
consumed, the Live worker entered, cadence eligible, the turn characterization
outcome, the requested command, the OS edge result, the
`NavigationApplyResult`, the leases actually held, **whether the character
moved**, and the release after Stop. An internal `APPLIED` with no observed
movement is **not** a pass — the overlay says `NO MOTION` for exactly that
case.

Windows has still never executed a line of `platform_win.py`. The chord poller
is contract-tested from macOS in a subprocess only.

---

## What changed on 2026-08-28 (fifth pass) — the setup machine, run natively

Roblox was in native fullscreen for the whole fourth pass, so every native
check was blocked and the machine could only be observed diagnosing that. It
is windowed now, and the checks that were owed have been run.

Getting there needed one structural change. The composition root — the wiring
that decides *what automatic setup is* — lived inside `treasure_gui.py`, which
imports Tk at the top. So the single most important native question, *does
Start Navigator reach READY on this machine?*, could not be asked without
opening a dashboard, and the fourth pass answered it with a throwaway script
that was never committed and never repeatable.

| Change | Why | Where |
|---|---|---|
| `Application`, `build_application`, `EngineSetupPort` and `shift_lock_probe` move out of the Tk module into `prospector_engine/application.py` | one composition root, callable without a user interface; the move also put ~450 lines under `mypy` for the first time and immediately found a real `Any` leak (`make_setup` was annotated `-> Any`, so every `SetupProgress` the setup runner returned was unchecked) | D-042 |
| `treasure.py --setup-probe` | the real `build_application`, the real coordinator, the real bounded stages, no Tk, no dashboard, no input; stops at the observation half and prints the held-lease ledger so "sends no input" is checkable rather than asserted | `tests/test_cli_lifecycle.py` |
| The restoring read-back settles | found by the probe on its own first native run — see below | D-043 |

### The bug the probe found in itself, on its first native run

The client started at 1800×1053 pt. Setup fitted it to 1280×720. The restore
asked for 1800×1053 and **succeeded** — and the single read-back taken
immediately afterwards reported `1063x610 pt at (18,499)`, a size nobody had
asked for. A second later the window was exactly 1800×1053 at (0,67), where it
began.

macOS animates a resize, so a read taken straight after `pin_client_rect`
lands mid-flight. The fit machine has settled for three stable read-backs
since D-032 for exactly this reason, which is why the fit stage was never
wrong about its own result while a fifteen-line restore was. Worth recording
because of what it *looked* like: a platform defect, and a false geometry
about to be written into this table.

### Automatic setup, run natively — four consecutive runs

`.venv/bin/python treasure.py --setup-probe`

| Run | Fit requested → achieved | Capture restarts | Stabilized | Terminal stage | Input edges held | Restored to |
|---|---|---|---|---|---|---|
| 1 | 1280×720 pt → **1280×720 pt / 2560×1440 px** | 1 | 5 fresh matching frames by 1.27 s | `failed: profile_ambiguous` at 7.49 s | `()` | 1800×1053 pt at (0,67) |
| 2 | same | 1 | by ~1.0 s | same | `()` | exact |
| 3 | same | 1 | by ~1.0 s | same | `()` | exact |
| 4 | same | 1 | by ~1.0 s | same | `()` | exact |

Read this honestly, in both directions.

**What is now observed fact.** The transactional fit works against the live
client through the production path: requested 1280×720 and achieved it, with
the origin preserved at (0,67), on four consecutive runs. Exactly **one**
capture restart per run — the fence works, and no intermediate geometry was
classified as `CAPTURE_MISMATCH`. Capture re-stabilized in five fresh matching
frames in about a second. Automatic setup emitted **zero input edges**, every
run, with the ledger printed as proof. The window was returned to the size it
was found at, every run.

**What is not.** Setup did **not** reach READY, and no amount of engineering
here will change that: no treasure map was equipped, so `SELECT_PROFILE`
correctly refused with `yellow_map_v1 and green_arrow_v1 score within 0.02`
and the remedy *"Equip a treasure map so its arrow is on screen"*. That is the
machine being right — on a frame with no arrow, the two profiles genuinely are
indistinguishable, and a classifier that picked one would be guessing. The
stages past `SELECT_PROFILE` — `ESTABLISH_REFERENCE`, `SHADOW_QUALIFY` — have
therefore still never run against real frames, and `CHARACTERIZE_TURN` still
needs a physical arm. **READY on real frames remains owed by the owner**, and
the action is one sentence long: equip a map and run `--setup-probe` again.

### Performance was measured and the numbers are not usable

Two other Claude sessions were running on this machine (98 % and 15 % of a
core), with WindowServer at 47 % and Roblox at 38 %, load average 5.8–7.1.
Consecutive `--shadow-bench 15` runs disagreed by a factor of five — capture
read 57.9 fps in one run and 10.5 fps in the next, which is not a property of
the code. The fourth pass's quieter-machine numbers below stand as the
reference; nothing from this pass is added to them. **E-PERF stays PENDING.**

Capture alone, via `--capture-probe`, was consistent with the reference: 58.0
unique fps at 60 Hz, p50 5.3 / p95 7.3 ms, 0 duplicates, 0 superseded.

### Detector: unchanged, and checked

`--detector-report --corpus tests/corpus/real`, eval split: recall **80.2 %**
(69/86), **0** false locks, **0** identity switches, sign **90.9 %**, median
**10.4°**, p95 104.6°. Identical to the fourth pass, per sequence and in
total. No detector code was touched.

---

## What changed on 2026-08-28 (fourth pass) — production navigation

The third pass measured the detector and fixed it. This one audited why
nothing downstream of the detector could ever run, and rebuilt that half.

| Confirmed root cause | Evidence it was real | Now |
|---|---|---|
| **The commissioning system could not complete.** `NavigationGates` was constructed once with every `E-*` field `PENDING` and frozen; no production code validated or persisted a gate; `CommissioningWindow` was a periodically rewritten read-only `Text` widget that performed no procedure and no state transition. "Collect Calibration Evidence", "Calibrate Live Control" and "Enable Live Control" all converged on it, and `_arm()` redirected there whenever blockers existed. | `commissioning_steps()` only rendered gate statuses; no call site anywhere set one. `_collect_evidence()` opened the window and started a recording. | Deleted. `prospector_engine/autosetup.py` runs nine typed bounded stages to READY; capability is `NavigationCapabilities`, derived from what this run observed (D-036) |
| **Live was unreachable twice over.** `make_live_worker` refused while the static gates were pending; even with them fabricated, `Navigator` built a default `ShiftLockController` whose empty `YawCalibration` also refused. Green tests hid both by injecting `ALL_PASSED` and a fabricated calibration. | `tests/test_navigation.py` `_calibrated_navigator` constructed exactly that. | `tests/test_setup_flow.py` builds the real application through `build_application` and drives it to READY with nothing injected |
| **Fit was not a transaction.** Capture, telemetry and guard polling continued during a resize, so an intermediate size was classified `CAPTURE_MISMATCH`, restarting capture and churning the epoch. The macOS AX lookup fell back to the largest, then the first, window. | `ViewportGuard.check()` compared against the adopted identity with no fence; `_ax_window_for` ended in `largest window` / `first window`. | `ViewportGuard.transaction()` fences mismatch classification, bounded by its own deadline (D-035); AX ambiguity is refused with a remedy (D-034) |
| **The GUI had no geometry contract.** Unconstrained status and blocker labels, `grid_remove` on conditional controls, and `CommissioningWindow.refresh()` scheduling its own `after` at the end of the render — so opening it repeatedly multiplied the refresh loop. | Four clicks produced four loops; a long Accessibility message widened the toplevel. | Fixed-width leaves, fixed-height wrapped message boxes, state-not-grid for conditional controls, one `Ticker` per loop, one expanding row — each asserted in `tests/test_gui.py` (D-041) |
| **Navigation was scaffolding.** Left/Right were not in the vocabulary; the player reference was a hardcoded anchor; the pipeline supplied `motion=None`; applied `W` never reached the progress ledger; recovery changed labels and emitted nothing; two steering controllers coexisted. | `PerceptionPipeline.analyze` set `motion=None`; `RecoveryLadder.escalate()` returned a level nobody executed; `SteeringController` was constructed and only ever asked for its config. | Turn keys in the vocabulary and the release floor (D-038); a measured `TurnResponse` per run (D-037); `estimate_lk_affine` wired; `NavigationApplyResult.leases_held` feeds the ledger; a recovery ladder that emits real bounded maneuvers with a sticky side; one `ArrowFollowerController` |

**Detector: deliberately unchanged.** The real-frame corpus report is
byte-identical before and after this pass, per sequence and in total.

## What changed on 2026-08-28 (third pass) — regression recovery

The second pass rebuilt the detector and the dashboard. This one measured
them on real frames and found that they had regressed, then fixed what the
measurements said.

| Observed regression | Root cause confirmed | Now |
|---|---|---|
| Arrow locks onto terrain, HUD and particles; identity changes between objects; direction sign is a coin flip | The two-notch topology was a *precondition* and real outlines are nicked by UI strokes; the boundary term was judged against the frame median and read 0 on flat sand; polarity came from taper about a notch line that a low camera angle inverts; `analyze()` selected the held track while the pipeline took direction and contour from the *first accepted* hypothesis | Stateless `propose` → `fuse` → one `commit` per unique frame; exactly one **selected** candidate feeds everything; ACQUIRE/TRACK/AMBIGUOUS/REACQUIRE/LOST with time-based bounds; polarity led by the **barb asymmetry**; structure is weighted evidence, contrast the only soft veto |
| 15 unique fps, 8 analysis fps, 264 ms p95 | An ROI miss ran a second stateful full pass on the same screenshot; per-candidate measurement windows up to 1 M px dilated with a 41 px element; a 240-sample p95 kept one old 274 ms sample "current"; two 0.5 s bad polls downshifted; DEGRADED had no path to a probe; zero processed fps fell back to capture fps | Bounded windows, bounded candidates, spaced idle searches; readiness judged on a 2 s recent window with history kept beside it; 1 s of shortfall to downshift; DEGRADED probes after cooldown; settling and SCK reconfiguration acknowledged before judging; a real zero is a zero |
| Fit & Lock frequently did not visibly resize | `_ax_window()` took `windows[0]` while `_scan_roblox()` picked a different CG window; a move to (0,0) was requested with the resize and a denied move failed the whole call; an OS clamp returned `ok=False` | AX window correlated by frame/title/largest; size only, origin preserved; clamp is `ok=True, clamped=True`, classified by the guard after three stable read-backs; typed `FitCompletion` through the coordinator; progress in the dashboard |
| 14 static, duplicated blockers | A default `ShiftLockController` was instantiated to ask why it could not steer, so E-YAW appeared three times; "not frontmost" was a permanent failure | Keyed, scoped `LiveBlocker`s recomputed on every read; one row per gate; "not frontmost" is an *expected* condition with an instruction; an 11-step guided commissioning window |
| `--detector-report` alive for hours in a Tk lifetime | Not reproduced here (the rendered report exits in 72 s) | Every offline mode is mutually exclusive and bounded, and `tests/test_cli_lifecycle.py` proves it as subprocesses: no Tk, no dashboard, no child process, bounded exit |
| Synthetic soak and rendered fixtures proved nothing about the game | Rendered frames are training stress by plan §7.2 | A **real-frame corpus** (`tests/corpus/real`, 178 frames, 14 sequences, split by contiguous sequence) with a bbox-aware evaluator and a regression gate; a native `--shadow-bench` |

---

## Legend

`done` · `partial` · `pending` (gate exists, not run) · `blocked` · `n/a`

---

## Phases

| Phase | Local implementation / replay | macOS commissioning | Windows commissioning |
|---|---|---|---|
| 0A Characterization | **done** | n/a | n/a |
| 0B Platform ports and viewport | **done** — explicit coordinate spaces, connect/fit split, bounded fit machine, AX-window correlation, honest clamps, typed completions | **partial** — repeated fit measured natively on this Mac (2x, one display), stable across four runs with one capture restart each; the DPI/display matrix is **pending** (E-VIEW) | **pending** (E-VIEW) |
| 0C Input authority and deadman | **done** — unchanged this pass | **pending** | **pending** |
| 0D Coordinator migration | **done** — plus fit completions as typed queue items | n/a | n/a |
| 0E Bounded legacy services | **done** | **pending** | **pending** |
| 1 Observation foundation and GUI | **done** — automatic setup, stable-geometry dashboard, one timer per loop, bounded per-frame trace, honest governor | **partial** — native headless observation measured at 57 of 57 unique fps (third pass); an owner-observed session with a map equipped is **pending** | **pending** |
| 2 Offline perception | **partial** — detector v2 measured on the real corpus (below); no separately held-out session exists | **blocked** on a second recording | **blocked** |
| 3 Navigation and controller | **done locally** — one follower, measured turn actuator, wired motion, bounded recovery; 94 simulated routes at 30/60/90/120 fps | **pending** — setup now runs natively as far as `SELECT_PROFILE`, which refuses correctly with no map equipped; the stages past it need a map on screen, and the armed ones need a physical arm | **pending** |
| 4 One-map live lifecycle | **partial** — the armed prologue is now three stages (input acceptance, control mode, turn characterization), all tested against fakes and a simulated world, and input acceptance is tested end to end through the real control port, the real probe and real optical flow on rendered frames | **pending** — the whole chain past a physical Arm + Ctrl+N is unobserved | **pending** |
| 5 Multi-map lifecycle | **blocked** | **pending** | **pending** |
| 6 Packaging and release | **partial** | **pending** | **pending** |

---

## Native evidence taken this pass

Two things were measured on this machine, without arming anything and without
sending any input.

| What | Command | Result |
|---|---|---|
| The macOS hotkey listener hears the keyboard | `treasure.py --hotkey-test 3` | Tap reached **READY**; **42 real key edges** arrived in three seconds and normalized correctly (named keys recognized, the Shift modifier read off `CGEventGetFlags`); **0** tap re-enables, **0** exceptions; ordinary keys and lone modifiers did not stop it. |
| The read-only OS key-state probe | `port.key_state(InputKey.W)` | Reports `False` for every key at rest and flips to `True` while a key is physically held. This is the `OS_EDGE_LOOPBACK_OBSERVED` source. |

**Neither is the movement gate.** Both are the halves that can be checked
without a person. The half that cannot is a physical **Ctrl+N** with Roblox
focused — one gesture since D-062, not two — or the `--forward-probe` command,
and neither has been run. `GAME_MOTION_CONFIRMED` has never been observed.

---

## Experiment gates

None of these is passed. Two moved from "nothing measured" to "measured here,
not validated".

| Gate | Status | What changed / what it still needs |
|---|---|---|
| E-VIEW | **partial** | Fit ran against the live client through the production setup path on **four consecutive runs** (fifth pass): 1280×720 pt / 2560×1440 px every time, origin preserved, exactly one capture restart per run, re-stabilized in five fresh matching frames, window restored exactly. One display, one DPI, one OS. Needs the DPI/display matrix and Windows. |
| E-ANCHOR | pending | Reviewer-labelled avatar control pivot across sessions. The runtime *reference check* (heading stability with the screen anchor, measured jitter recorded) is a different and weaker claim and never presented as this gate. |
| E-FORWARD | pending | **Physically armed** bounded `W` pulses with blinded labelling. The machinery for it now exists and runs automatically as the `VERIFY_INPUT` stage — one ~160 ms pulse, judged only against frames captured after its own down edge, against an idle noise floor measured seconds earlier. What is missing is a human pressing Arm and Ctrl+N; nothing here can supply that. |
| E-DIR-IDEAL | pending | Manual masks plus aligned-zero outcome trials. |
| E-PROF | **pending, with regression evidence** | A real corpus exists and the eval split is a regression gate (`tests/test_corpus.py`). It is one session, one map, one machine, with the previous overlay drawn on most arrows. Needs a second, separately held-out recording of a live session with Shadow running (Start Diagnostic Recording). |
| E-DIR-E2E | **pending, with regression evidence** | Same corpus; headings are reviewer-read to about ±12°, which cannot certify a 10° p95. |
| E-ARRIVE | pending | Approach/fade sequences and long negatives. Arrival now *stops* the route after three consecutive candidates rather than being ignored; stopping needs no gate, digging on arrival would. |
| E-MOTION | pending | Labelled clips and an armed locomotion baseline. A **session-scoped** baseline is now sampled at runtime from frames where forward was genuinely applied (D-040); it is prefixed `runtime:` and its provenance says in words that it is not this gate. |
| E-SHIFTLOCK | pending | Armed stationary micro-yaw with Shift Lock on and off. The runtime check observes the centred pointer cue and never toggles Shift (D-037); the armed micro-yaw method is defined and unrun. |
| E-YAW | pending | **Physically armed** yaw pulses confirmed by perception. Superseded in the production path by the per-run `TurnCharacterizer`, which measures sign, gain, minimum pulse, latency and reliability from bounded stationary probes each session; that has been exercised against a simulated camera only and is **pending** a native run. |
| E-STEER-CAL | pending | Armed manual-target trials. |
| E-STEER-E2E | pending | Guarded routes. |
| E-RECOVERY | pending | Now **in** the control path and bounded: release, reacquire, sticky-side strafe, forward probe, jump, opposite side once, abandon. Judged on a simulated world only; needs a native obstacle test after open-ground following is safe. |
| E-DIG / E-LIFECYCLE | pending | Labelled dig episodes. |
| E-NEXT_MAP / E-SKIP_MAP | pending | Off. |
| E-PERF | **pending, with native measurements** | Headless Shadow on the live client measured below. Stop latency in wall-clock ms and the dashboard-with-Live path are not measured. |

Because no gate is validated: **Live navigation refuses to steer**, automatic
profile classification is off, recovery is off, next-map automation is off.

---

## Observed local facts (not gate passes)

### Real detector metrics — `tests/corpus/real`, eval split

`.venv/bin/python treasure.py --detector-report --corpus tests/corpus/real --json out.json`

122 eval frames: 86 arrow-present, 30 arrow-absent (22 from the recording, 8
live), 6 `unknown` (excluded and counted). Sequences marked `tune` were used
for every choice; `eval` was only read. Headings are reviewer-read (±12°).

| Build | Recall | False locks | False acquisitions (recording / live) | Identity switches | Single-frame replacements | Sign accuracy | Median error | p95 error | Perception p50 / p95 |
|---|---|---|---|---|---|---|---|---|---|
| f3f7b63 (baseline segmenter, `yellow_map_v0`) | 91.9 % (79/86) | 2 (1.9 %) | 0/22 / not run | 2 | 0 | 61.6 % | 28.2° | 172° | 3.4 / 5.3 ms |
| 4756ab7 (second pass, HEAD at start) | 52.3 % (45/86) | 8 (7.4 %) | 0/22 / not run | 0 | 0 | 51.6 % | 87.1° | 160° | 11.0 / 51.5 ms |
| **this pass** (detector v2, `yellow_map_v1`) | **80.2 % (69/86)** | **0** | **0/22 / 3/8** | **0** | **0** | **90.9 % (30/33)** | **10.4°** | 104.6° | **5.2 / 8.3 ms** |

Per stratum, this pass: pink-crystal 90.9 % (20/22), purple-night 83.3 %
(5/6), purple-pale 75 % (9/12), sand-same-colour 72.2 % (13/18), open-water
90 % (9/10), sand-occluded 72.2 % (13/18); no-arrow-ui 7/7 and no-arrow-sand
15/15 clean. The direction abstains on most occluded frames rather than
guessing; of 33 frames where it answered, 3 were reversed.

Read this honestly:

- The baseline finds *a fragment of* the arrow more often (its box overlaps
  the label) and points it wrong four times in ten. This pass finds the whole
  arrow less often and points it right nine times in ten, with no false
  locks. The occluded stratum is the weak one.
- **Tune split** (what the detector was chosen on): recall 55.6 %, 4 false
  locks — all on the player's yellow hat beside a hidden arrow — sign 100 %
  (12/12). Reported, not averaged in.
- The live event scene (rainbow lighting, yellow banners) still acquires a
  banner or particle in 3 of 8 frames. The fixed HUD bands are excluded by
  `yellow_map_v1`; the rest is held at the measured count by the gate.
- Against the production targets (recall ≥ 95 %, precision ≥ 99 %, false
  locks < 1 %, sign ≥ 99 %, p95 ≤ 10°): false locks and switches meet them;
  recall, sign and angular error do not, and the sample cannot certify any of
  the percentages. **PENDING.**

### Native Shadow — `treasure.py --shadow-bench`, live client, Balanced 60

Same machine, same scene (a busy no-arrow view, which is the worst case for
the search), headless, 15 s per configuration, read-only.

| Build | Configuration | fps consumed / unique | processed ÷ unique | capture→observation p50 / p95 / p99 / max | CPU | RSS |
|---|---|---|---|---|---|---|
| f3f7b63 | capture only | 78.2 / 78.2 (its 120 Hz tier) | 1.00 | 4.5 / 5.5 / 6.2 / 8.8 ms | 35 % | 108 MB |
| f3f7b63 | capture + perception | 77.4 / 77.6 | 0.997 | 8.9 / 11.4 / 13.6 / 95 ms | 88 % | 170 MB |
| 4756ab7 | — | not measured natively; the dashboard read 15 unique / 8 processed fps, p95 264 ms | | | | |
| this pass, first cut | capture + perception | 32.0 / 32.4 (governor at 30 Hz) | 0.99 | 16.5 / 24.9 / 38.2 / 150 ms | 73 % | 191 MB |
| **this pass, final** | capture only | 57.8 / 57.8 | 1.00 | 4.6 / 5.6 / 6.3 / 7.2 ms | 27 % | 105 MB |
| **this pass, final** | capture + perception | **57.0 / 57.2 at 60 Hz** | **0.997** | **9.7 / 17.3 / 19.8 / 25.5 ms** (outside settling; 213 ms at startup) | **86 %** | 181 MB |

Against the Balanced-60 targets, headless: ≥ 54 processed fps **met**; ratio
≥ 0.98 **met**; p95 ≤ 25 ms **met**; p99 ≤ 50 ms **met**; max ≤ 100 ms outside
settling **met**; superseded 4, pool exhausted 0 **met**. CPU: 86 % against
the baseline's 88 % at 77 fps, which is about +21 points at equal frame rate
— at the edge of the +20 criterion. With the dashboard (next table): 57.6
processed fps **met**, Minimal p95 23.9 ms **met**, Full Diagnostics p95
25.5 ms **missed by half a millisecond**, and CPU 149–176 % is the dashboard's
own cost on top; the baseline's dashboard was not measured. Memory was not
soaked natively (15 s). **E-PERF stays PENDING.**

### Native dashboard — the real Tk dashboard with Shadow running

The dashboard itself, Shadow started through the coordinator, the real Tk
preview, 15 s per overlay mode, same no-arrow scene. The first run exposed
the last regression mechanism and is kept in the table.

| Build / cadence | Overlay | unique / processed / preview fps | tier | end-to-end p50 / p95 / p99 / max (history ring) | CPU | RSS |
|---|---|---|---|---|---|---|
| this pass, before the throughput rule (Auto) | Minimal | 29.4 / 29.3 / 29.1 | 30 Hz | 18.9 / 26.8 / 30.8 / 34.2 ms | 142 % | 287 MB |
| this pass, before the throughput rule (Auto) | Full Diagnostics | 29.3 / 29.2 / 29.2 | 30 Hz | 19.6 / 26.3 / 28.4 / 34.0 ms | 140 % | 287 MB |
| **this pass, final (Auto)** | Minimal | **82.4 / 82.1 / 24.9** | 90 Hz | **10.9 / 21.9 / 27.2 / 28.4 ms** | 172 % | 283 MB |
| **this pass, final (Auto)** | Full Diagnostics | 82.1 / 75.0 / 25.2 | 90 Hz | 11.8 / 28.9 / 35.3 / 35.9 ms | 164 % | 288 MB |
| **this pass, final (Balanced 60)** | Minimal | **57.1 / 57.6 / 24.9** | 60 Hz | **10.4 / 23.9 / 25.7 / 25.8 ms** | 176 % | 292 MB |
| **this pass, final (Balanced 60)** | Full Diagnostics | 56.9 / 57.2 / 25.0 | 60 Hz | 14.5 / 25.5 / 29.2 / 32.5 ms | 149 % | 281 MB |
| f3f7b63 dashboard | — | not measured: its metrics API differs and the bench script could not read it; its headless numbers are above | | | | |

What the first run's trace said: the worker processed 52 frames a second at
60 Hz with 13 % superseded because the Tk preview at 60 fps competed for the
interpreter (full passes p50 15.9 ms against 10.3 headless); the governor
counted the superseded frames as "observation loss 10 %", downshifted to
30 Hz, and the probe back needed 54. A latest-only pipeline supersedes by
design, so the governor now judges a tier against the tier below (D-031
amendment), the preview ticks at 30 fps, and one telemetry publish samples
metrics once. Full Diagnostics still costs about seven processed frames a
second at 90 Hz through the overlay's canvas work on the Tk thread; its
overlay is capped at 20 Hz and skips rather than queues.

### Fit & Verify — live client

`before: client 1800x1054 pt at (0,67), backing 3600x2108 px`
→ `canonical_verified` in 0.35 s, `1280x720 pt / 2560x1440 px (3/3 stable)`,
origin unchanged → restored to `1800x1053 pt` afterwards (one point of
title-bar rounding). Accessibility and Screen Recording were already granted
to this terminal.

### Rendered stress strata — `treasure.py --detector-report`

Still bounded (exits in 72 s). With the new scoring the rendered
same-colour-clutter and translucent strata read far lower than the second
pass reported (1 % and 0 % coverage): the contrast veto and the profile floor
that fixed the real frames are hostile to a half-transparent rendered arrow
on pale terrain. Rendered frames are training stress; this is recorded, not
optimised for.

### Stop safety, governor traces, soak

Unchanged tests still pass (10 000 stop races clean; every watchdog condition
releases; Shadow emits zero input edges). The governor's five required traces
pass with the new hysteresis. The ten-minute synthetic soak was measured on
4756ab7's detector and has **not** been rerun with this one; the three-second
soak in the lifecycle tests passes.

---

### Fourth pass, native — what was and was not measurable

Roblox was in **native fullscreen on another Space** for the whole of this
pass. That blocks capture and window sizing outright, so every native check
below is *pending on the owner leaving fullscreen* — not deferred by choice.

What that did give is a real native result for the diagnosis itself. The real
`build_application`, the real coordinator and the real setup machine, run
against this machine:

```
stage:   failed
kind:    fullscreen
summary: Roblox is in fullscreen, where its window cannot be sized or captured
remedy:  Leave fullscreen so Roblox is an ordinary window, then press
         Start Navigator again.
detail:  Roblox is running but not on this Space - exit native fullscreen
input edges held: ()
```

`--capture-probe` and `--shadow-bench` report the same condition and exit
cleanly without touching the window.

### Dashboard cost — measured, this pass

Real Tk, real dashboard, 200 samples per loop, no capture and no worker:

| Loop | Cadence | mean | p50 | p95 |
|---|---|---|---|---|
| status | 150 ms | 0.135 ms | 0.129 | 0.160 |
| setup panel | 120 ms | 0.012 ms | 0.011 | 0.014 |
| metrics + summaries | 500 ms | 0.022 ms | 0.021 | 0.025 |
| preview (idle) | 33 ms | 0.001 ms | 0.001 | 0.001 |
| diagnostics drawer, open, data changed | 700 ms | 0.147 ms | 0.143 | 0.166 |
| diagnostics drawer, open, nothing changed | 700 ms | suppressed - 50 of 50 renders skipped |
| diagnostics drawer, folded away | 700 ms | 0.0001 ms |

**Idle GUI duty cycle: 0.11 % of one core.** The drawer was previously the
most expensive thing the window did and ran unconditionally; it now renders
only when it is visible *and* something changed.

### Pipeline latency — synthetic source, 20 s, this pass

The whole path: capture service, coordinator, observation worker, real
detector, real navigator. Synthetic frames, because Roblox was unreachable;
the numbers are about the *pipeline*, not about the game.

```
tier requested 90 Hz     governor stable
source 76.1   unique 76.1   processed 76.1   control 76.1   fps
duplicates 0   superseded 3   unobserved 3   slot depth 1
capture     p50  0.00 ms   p95  0.00   max  0.00   (the fake source is free)
perception  p50  5.53 ms   p95  7.99   max 14.33
decision    p50  0.02 ms   p95  0.03   max  0.14
end-to-end  p50  5.68 ms   p95  8.15   max 14.51
frame age   2.0 ms         cpu 77 %    rss 188 MB (peak 268)
```

Unique, processed and control rates are equal and the slot depth is one: the
control loop is taking the newest frame and building no backlog. Three
superseded frames in twenty seconds is the latest-only slot doing its job, not
loss. Perception is the whole cost; the decision is two hundredths of a
millisecond.

### Synthetic soak — 90 s, this pass

```
.venv/bin/python treasure.py --soak 1.5
```

51 unique fps and 51 processed fps sustained; RSS 122-168 MB against a 249 MB
peak; 4 607 frames; one thread before and after; clean capture shutdown;
0 of 8 pool buffers live; RSS slope -28 MB/min. **PASS** as a local soak; this
is not E-PERF.

## Native checks the owner must run

Everything below takes a few minutes and needs Roblox open, windowed, with a
map equipped so an arrow is on screen. Nothing before step 7 sends input.

### Morning, in order (about five minutes)

**Before anything: leave native fullscreen.** Roblox must be an ordinary
window on the current Space, with a treasure map equipped so an arrow is on
screen. Nothing before step 4 sends input.

1. `.venv/bin/python treasure.py`. Press **Start Navigator** — nothing else.
   Watch the stage strip: Find Roblox → Size window → Rebind capture → Check
   frames → Identify map → Check direction → Qualify. Note where it stops if
   it stops, and copy the sentence it prints.
2. If it reaches READY: check the LIVE READOUT. *Viewport* should read
   `1280x720 canonical` (or an honest clamped size), *Map profile* should be
   the map you actually have equipped, and *Alignment error* should be a
   number that changes as you turn. Press **Retry Automatic Setup** under
   Advanced twice more and confirm the window is sized the same way each time
   and the preview never blanks.
3. Under Advanced press **Record Diagnostics** and walk a normal route for two
   or three minutes — turning, grass, water, sand, standing under the arrow.
   This is the held-out session E-PROF needs. Press **Stop & Release All
   Input**; a trace lands beside the logs and the recording under recordings/.
4. **Only with a hand on Ctrl+X**, and only in a private server on open
   ground: focus Roblox and press **Ctrl+N** — one gesture, no button first
   (D-062). Watch the guidance line, which names each stage:

   * *Testing whether Roblox accepts a key* — one ~160 ms forward pulse. The
     character may twitch. If it says the game is not acting on the key, stop
     here and send the trace: nothing after this can work.
   * *Checking the camera control mode*, then *Measuring how the camera turns*
     — stationary. The LIVE READOUT's *Turning by* row should change from
     `not measured` to `arrow keys` or `mouse yaw`.
   * *Navigating — your character is moving* — and only now is it moving.

   Let it walk for no more than thirty seconds and press **Ctrl+X**.

Send back: the trace file, the recording directory, one line about the size
the window ended up, the *Turning by* value, the title bar (it carries the
commit and process id), and anything the character did that you did not
expect.

**If step 4 goes wrong in any way, Ctrl+X is always live** and releases every
key, both turn keys, and the mouse buttons, without consulting focus.

**Before step 4, one minute that needs no arming.** Run
`.venv/bin/python treasure.py --hotkey-test 30` and press **Ctrl+N** while it
runs. It prints every key edge it normalizes and every chord that completes.
This mode cannot arm anything and cannot press anything — it submits to a list,
not to the coordinator. If Ctrl+N does not appear there, nothing in step 4 can
work and the trace from step 4 will not say why.

### Windows — everything, from scratch

Unchanged from the previous pass: **no line of `platform_win.py` has ever
executed.** What this pass added to it - the Left/Right scancodes and the
`KEYEVENTF_EXTENDEDKEY` flag they require - is contract-tested from macOS
(`tests/test_platform_contract.py`) and is otherwise entirely unverified. The
extended-key flag in particular is the kind of thing that is either right or
sends numpad 4/6, and only a Windows machine can tell which.

---

## Release blocker

**G-LICENSE** is unresolved. Nothing may be pushed to a release, tagged,
published, or described as open source until the owner confirms first-party
reuse rights and records Treasure's distribution terms. The feature branch
`origin/treasure-production-navigation` is the only remote this pass writes.
