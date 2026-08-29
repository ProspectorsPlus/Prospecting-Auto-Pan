# savePatel — F1/F6, Arm, and where new functionality goes

Testing new branch!

## 1) What happens when you press F1, right now

F1 is bound to `IntentType.START_LIVE` (`platform_mac.py:133`), which starts
the new **Live navigator** (walk/turn toward the treasure-map arrow),
implemented in `navigation.py`'s `make_live_worker`.

That worker's first move is to check `gates.steering_enabled`
(`navigation.py:921`). `gates` is built in `treasure_gui.py:234` as
`NavigationGates(os_name=..., profile_id=...)` with every `e_*` evidence
field left at its default, `EvidenceStatus.PENDING`. So `steering_enabled`
is `False`, unconditionally, on this build.

Result: F1 arms Live, spawns the worker, the worker immediately does
`session.release_navigation("gates-pending")` and returns `FAILED`. The
coordinator treats that as terminal and safe-stops back to `IDLE`. **No
input is ever sent** — no walking, turning, digging, or pan-swapping.

This isn't a bug — it's `TREASURE_NAVIGATION_PLAN.md`'s evidence gating
working as designed: Live steering stays off until E-VIEW, E-ANCHOR,
E-FORWARD, E-PROF, E-DIR-E2E, E-YAW, E-STEER-CAL, E-STEER-E2E are all
`VALIDATED` for this OS/profile. None have been run yet.

The old "dig, pan-swap when full, dig, loop" behavior you're used to still
exists — it just moved to **F6** (`IntentType.DIG_LOOP`), per `DECISIONS.md`
D-015 (2026-08-27). Per that decision: the plan would only reach `DIG` via
`LIVE → ARRIVED`, which makes it unreachable until the whole gate chain
above passes — i.e. indefinitely. F6 was added specifically so the
capability the pre-navigator build actually had (and you actually use)
stayed reachable. Same underlying logic (`run_dig_at_current_spot`,
`run_pan_swap` in `engine.py`), just bounded now (attempt caps + deadline
instead of an infinite loop) and reached through a different key.

## 2) Arm + the 30-second countdown

Arming is a **safety gate specific to Live mode**, not something F6/dig
needs. Mechanism (`coordinator.py`):

- Clicking "Arm Live" in the Tk GUI submits `IntentType.ARM_LIVE_FROM_UI`.
  `_on_arm` (`coordinator.py:510`) only honors this if `intent.source ==
  "gui"` — i.e. only a physical mouse click in the app window can arm it.
- On success it mints a `LiveArmToken` (`coordinator.py:84`): a random
  one-use ID, tied to the current `run_id` and generation, with
  `expires_at_s = now + arm_ttl_s`. `arm_ttl_s` defaults to **30.0s**
  (`CoordinatorConfig.arm_ttl_s`, `coordinator.py:70`).
- Pressing F1 (hotkey, not GUI) submits `START_LIVE`. `_on_start_live`
  (`coordinator.py:541`) fetches the current token, and refuses if: no
  token, the token expired, the intent didn't come from `"hotkey"`, or the
  token's `run_id` doesn't match. If it's valid, the token is consumed
  (single use) and *then* readiness/gates are checked.

Why it exists: `CLAUDE.md` rule 1 — an agent (or an accidental double-press,
or stale state) must never be able to start Live on its own. Arming forces
two separate, deliberate physical actions — a GUI click *and* a hotkey press
while Roblox is focused — within a short window, so Live can't start from a
leftover arm state minutes later or from something other than a human at
the keyboard right now.

Net effect today: since Live no-ops anyway (see §1), arming currently just
gates you into a 30-second window to press F1 and get an immediate
`gates-pending` failure. It's fully live infrastructure waiting for a
feature that isn't turned on yet.

## 3) Where new functionality (e.g. `getAngle`) and its ordering should go

Your instinct is basically right, with one addition: **F6's "main loop" is
not a wrapper file — `run_dig_loop` inside `engine.py` *is* the main loop.**
There's no separate "coordination layer" you're missing; for this pathway,
functionality and sequencing already live in the same file, by design.

Concretely, in `engine.py` right now:

- **Functionality** (the building blocks): `on_dig_spot`, `capacity_full`,
  `run_dequip_pan`, `run_pan_swap`, `run_reset`,
  `run_dig_at_current_spot` — each does one bounded thing and returns a
  typed result.
- **Coordination** (the order things happen in): `run_dig_loop`
  (`engine.py:653`) — it's a `while` loop that calls
  `run_dig_at_current_spot`, and based on the returned outcome, either taps
  again, calls `run_pan_swap` when the pan reads full, or stops. **That
  `while` loop is your "main loop."**

So if you want to add `getAngle` and have it participate in F6's sequence:

1. **Detection math** goes in `vision.py` if it's arrow segmentation/PCA
   work (it mostly already exists there — `ArrowSegmenter`,
   `DIRECTION_STRATEGIES`), or directly in `engine.py` next to
   `on_dig_spot`/`capacity_full` if it's a simple pixel-sample check like
   those two.
2. **A function that produces the angle from one frame** goes in
   `engine.py`, following the existing pattern: take a `CapturedFrame` (or
   `ServiceContext`, via `ctx.frame()`), call into `vision.py`, return a
   plain typed value. Example shape:
   ```python
   def get_angle(ctx: ServiceContext) -> float | None:
       frame = ctx.frame()
       arrow = segmenter.observe(frame)          # from vision.py
       if not arrow.valid:
           return None
       direction = DIRECTION_STRATEGIES["fusion"](arrow, anchor_px, forward_deg)
       return direction.error_deg if direction.valid else None
   ```
3. **Wiring it into the sequence** — i.e., deciding *when* `get_angle` gets
   called relative to digging/pan-swapping — is an edit to `run_dig_loop`
   itself (or a new sibling function it calls), not a new file and not
   `coordinator.py`. `coordinator.py` never needs to know this function
   exists; it only ever sees "the dig_loop worker finished with outcome X."

Only step outside `engine.py` you'd ever need for something like this: if
`get_angle` should be reachable as its own key (not just folded into F6's
loop), you'd add an `IntentType`, bind a key to it in
`platform_mac.py`/`platform_win.py`, and register a worker for it in
`treasure_gui.py`'s `workers` dict (same pattern as `_service_worker`). That
last part is boilerplate, not logic — the logic itself still lives in
`engine.py`.

The one thing to keep separate in your head: **this is a different
pipeline from the Live navigator's arrow-angle math in `navigation.py`**
(`PerceptionPipeline`, `Navigator`, `SteeringController`). That one is
gated behind the evidence chain in §1 and is owned by the FSM, not by
`engine.py`. If your `getAngle` is meant to eventually drive real
steering/turning (not just inform F6's dig loop), that's the pipeline it
should plug into instead — which is the architectural fork with your
teammate we flagged earlier and still needs resolving.
