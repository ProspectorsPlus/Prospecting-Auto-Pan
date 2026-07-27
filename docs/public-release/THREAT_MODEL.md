# Prospector Lite — Threat model

Scope: the desktop app (`prospecting_app.py`), the engine subprocess (`prospector_engine/`), the packaging pipeline, and the repository itself. Written against the actual 1.0.0-rc.1 code; every mechanism named below exists in the tree and can be checked with the commands in [ROBLOX_SAFETY_BOUNDARY.md](ROBLOX_SAFETY_BOUNDARY.md) and [NETWORK_BEHAVIOR.md](NETWORK_BEHAVIOR.md).

## Assets

1. **The user's machine** — the app synthesizes OS input and reads the screen, so a malfunction or malicious change has real reach.
2. **The user's Roblox account** — automation risks moderation action; anything that made the tool more intrusive would raise that risk.
3. **The user's Discord webhook URL and optional `WEBHOOK_SECRET`** — anyone holding the URL can post to the channel.
4. **The user's Coach API key** (`prospecting_secrets.json`) — spendable money on a third-party provider.
5. **Local user data** — config/calibration, builds, scripts, run history, run logs, and any screenshots attached to notifications (screenshots show whatever is on screen).
6. **Integrity of distributed builds** — users must get the code that matches the public source.

## Trust boundaries

| Boundary | Mechanism |
|---|---|
| GUI ↔ engine | Subprocess with a local stdio protocol (`prospector_engine/ipc.py`); no sockets, no ports |
| App ↔ OS | Screen capture + input synthesis gated by macOS Screen Recording/Accessibility permissions; normal user process on Windows |
| App ↔ user-imported files | `.ppscript`/`.ppbuild`/calibration JSON are untrusted input |
| App ↔ network | Two opt-in, user-configured egress paths only (webhook, Coach AI) |
| App ↔ legacy install | One-time read-only copy from the old "Prospectors Plus" data dir |
| Repo/CI ↔ users | Build pipeline and release artifacts |

## Threats and mitigations

### T1 — Malicious imported script or build file
A shared `.ppscript`/`.ppbuild` crafted to execute code or abuse the machine.
**Mitigations:** imports are parsed as JSON only — never `eval`'d or executed (`import_build`, `_studio_sanitize` in `prospecting_app.py`); unknown/underscore keys are dropped or type-coerced against the schema; the Studio interpreter walks the tree as data, enforces a hard-coded key whitelist again at runtime (unknown tokens → safe stop), clamps waits to [100 ms, 120 s], and applies per-pass and total step budgets (see `docs/public-release/ARCHITECTURE.md` and the interpreter section of the root `ARCHITECTURE.md`).
**Residual:** a script can still waste the user's own time or drive the game badly — bounded by watchdogs and Esc.

### T2 — Runaway input (stuck keys, uncontrollable macro)
**Mitigations:** every termination path funnels through `release_all()` (`prospector_engine/engine.py`), which releases the held-set registry plus the whole injectable vocabulary; Esc quits instantly, Ctrl+K stops; sleeps run in ≤25 ms slices so stops are immediate; independent watchdogs (`NO_PROGRESS_SEC`, `RECOVER_LIMIT`, `SAFE_STOP_MAX_RETRIES`) stop the run when nothing progresses.
**Residual:** input goes to whatever window has focus; the user is told to keep Roblox focused during runs.

### T3 — Data exfiltration / tracking
**Mitigations (removals, verified in code):** the pre-1.0 access-code gate, machine locking (`ACCESS_*`, `MACHINE_SALT`), and owner-analytics endpoint (`SYNC_URL`) are gone; the migration strips those keys from imported configs; there is no update check, telemetry, or remote content. Default network activity is zero requests; both remaining paths point at user-chosen endpoints (inventory: [NETWORK_BEHAVIOR.md](NETWORK_BEHAVIOR.md)).
**Residual:** if the user enables screenshot notifications, screen content leaves the machine to *their* webhook — by design, documented in [PRIVACY.md](../../PRIVACY.md).

### T4 — Webhook secret/URL exposure
**Mitigations:** the webhook URL/secret live only in the local config; the secret is sent only as the `x-macro-secret` header to the user's URL and is never logged; the tracked sample config ships empty values; docs tell users not to paste these when asking for help.
**Residual:** R2 (SSL fallback, below); local files are unencrypted (R3).

### T5 — Compromised or tampered release artifact
**Mitigations:** builds come from public source (Windows: GitHub Actions on a hosted runner); releases ship `SHA256SUMS.txt`; a CycloneDX SBOM accompanies artifacts; [RELEASING.md](../../RELEASING.md) requires re-verifying checksums post-publish.
**Residual:** R1 — the macOS build is unsigned/not notarized, so a user tricked into downloading from a fake source has no signature to fail. Mitigated only by checksums + building from source, until a signing certificate exists.

### T6 — Secrets baked into builds or the repository
**Mitigations / findings:** a real webhook URL was committed in history (commit `6842a47`) and scrubbed from current files; publication therefore requires fresh history and revoking that webhook — full detail in [SECRET_AUDIT.md](SECRET_AUDIT.md). `prospecting_secrets.json` is gitignored, never committed, never bundled. The legacy CI/DMG secret-injection steps must be removed before the public pipeline is used (tracked in [RELEASING.md](../../RELEASING.md)).
**Residual:** none once the preconditions in RELEASING.md are met; until then, publication is blocked.

### T7 — Supply chain (dependencies)
**Mitigations:** a small dependency set (pywebview, mss, numpy, pillow, pyobjc, pythonnet/clr-loader), inventoried with versions in [THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md); SBOM per release.
**Residual:** Windows CI installs some packages unpinned; pinning is listed as release hardening.

### T8 — Malicious local help/tutorial edits
`tutorial_content.json` is user-writable local data rendered in the app's webview.
**Mitigations:** it is local-only content the user themselves edits; no remote party can write it.
**Residual:** a local attacker who can write the user's files already controls the account (general R3).

## What was removed before going public

- Access-code gate, machine-locked codes, and machine identity/salting.
- Owner analytics endpoint (`SYNC_URL`) and all IP/location collection.
- The bundled webhook destination (the "analytics" webhook) — notifications are now exclusively user-owned and off by default.

## Residual risks (accepted and documented)

- **R1 — Unsigned macOS build.** No signature/notarization until a certificate exists. Users must rely on checksums or build from source. ([SECURITY.md](../../SECURITY.md))
- **R2 — SSL-unverified fallback.** Webhook delivery (engine + app test path) and the Coach API call retry once without certificate verification after a failed verified attempt, to accommodate old bundled-Python certificate setups. On a hostile network that retry could be intercepted. Opt-in features only.
- **R3 — Plaintext local storage.** Webhook URL/secret and Coach API key are plain JSON in the user profile; OS file permissions are the only protection.
- **R4 — User-imported files.** Validation is strict, but a script that is *valid* can still play the game badly or spam the user's own webhook; budgets and watchdogs bound the damage.
- **R5 — Account risk is inherent.** The tool synthesizes input; no design choice removes the possibility of Roblox moderation action. The project never claims otherwise.
