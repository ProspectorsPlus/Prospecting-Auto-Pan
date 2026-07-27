# Prospector Lite — Security audit (public-release pass)

Performed as part of the 1.0.0-rc.1 public-release engineering pass and
updated for the 1.0.0-rc.2 trust-and-onboarding pass, on the actual tree at
each state. Every claim below was produced by a command run in one of those
sessions; re-run commands are included so a reviewer can reproduce them.

Status legend: **fixed** (changed in this pass), **accepted** (known,
documented residual risk), **blocked** (needs a user-owned action),
**n/a** (not present), **not verified** (couldn't be executed here).

## Summary table

| Area | Result | Status |
|---|---|---|
| IP / geolocation collection (`ip-api.com` beacon) | Removed entirely, incl. callers and config keys | fixed |
| Owner analytics webhook (`SYNC_URL`) | Removed; key scrubbed from configs on load and on disk | fixed |
| Hardware fingerprint (`_machine_id`: IOPlatformUUID / MachineGuid) | Removed with the machine-lock | fixed |
| Access-code gate (remote hash list, revocation re-check) | Removed; welcome onboarding replaces it | fixed |
| Auto update check + silent installer download | Removed; no automatic network path remains | fixed |
| Remote tutorial-content fetch at boot | Removed (local content only) | fixed |
| Google Fonts loaded by the analytics window | Removed (system fonts) | fixed |
| CI secret injection into shipped configs | Pipeline rewritten; no secrets step exists | fixed |
| Engine webhook accepts `http://` from a hand-edited config | https-only guard added in `post_webhook` | fixed |
| SSL-unverified fallback retry (webhook/coach, after verified attempt fails) | **Removed at every site in rc.2** (engine webhook, app webhook test, Coach API, plus the `windows/` mirrors): TLS verification is mandatory via a verifying context that falls back to the bundled `certifi` CA store when the interpreter ships an empty trust store — verified, never unverified; an `ssl.SSLError` drops the request with an explicit error, no unverified retry. `grep -rn _create_unverified_context .` → 0 hits | fixed (rc.2) |
| Webhook screenshot attachment defaulted on (`NOTIFY_SCREENSHOT=True`), so enabling the webhook implicitly opted into screen captures leaving the machine | Default flipped to `False` in rc.2 (`prospector_engine/engine.py`); screenshots are a separate, explicit opt-in | fixed (rc.2) |
| Macro could start without required macOS permissions (TCC prompt fired mid-run, attributed to whichever process touched the API first) | `launch()` now blocks Start — and only Start — when a required permission (screen, input, stop hotkeys) is definitively not granted, returning `perm:<ids>`; the wizard/Trust Center explain and request. Detection uses real preflight APIs and never prompts by itself | fixed (rc.2) |
| `finds_sim.py` uses `exec` on a slice of the app's own tracked source | Dev-only test harness; input is the tracked file itself | accepted |
| Browser-fallback UI writes a launcher with mode 0755 | Executable launcher; expected | accepted |
| Secrets in git history (webhook URL `6842a47`, bot secret `e0f3d4f`) | Revoke + fresh-history publish required | blocked (user) |
| Process injection / memory APIs | None anywhere in the tree | n/a |
| `shell=True` / `os.system` | None in tracked Python | n/a |
| Unpickling / deserialization of foreign objects | None; imports are strict-validated JSON | n/a |
| Windows runtime behaviour on a real machine | CI workflow exists; not executable pre-push | not verified |

## Tooling runs (this session)

- **bandit 1.9.4** over `prospecting_app.py`, `prospecting_ui.py`,
  `prospecting_assistant.py`, `prospecting_old.py`, `prospector_engine/`
  (27,153 LOC): **0 high**, 9 medium. All 9 mediums triaged above (B310
  urlopen-scheme audits on the three user-configured egress paths — https
  enforced at save time and now engine-side; B323 unverified-context
  fallbacks — accepted at rc.1, **removed in rc.2** so these findings no
  longer occur; B103 launcher chmod 0755 — expected).
  Re-run: `bandit -ll -r prospecting_app.py prospecting_ui.py prospecting_assistant.py prospecting_old.py prospector_engine/`
- **pip-audit** over the frozen macOS build closure (pywebview 6.2.1,
  pyobjc 12.2.1, mss 10.2.0, numpy 2.5.1, pillow 12.3.0, pynput 1.8.2,
  pyinstaller 6.21.0 + transitive): **no known vulnerabilities**.
- **Secret scans**: tracked tree clean (credential-shape regexes in
  `public_release_tests.py`); full-history `-S` sweeps found the two known
  historical secrets (see `SECRET_AUDIT.md`) and no API keys.
- **Injection/memory API scan**: zero hits for ReadProcessMemory /
  WriteProcessMemory / CreateRemoteThread / VirtualAllocEx / task_for_pid /
  mach_vm / ptrace / SetWindowsHookEx / DYLD_INSERT_LIBRARIES across all
  tracked sources (`public_release_tests.py`, section 3).

## Package audit (macOS candidate)

The built `Prospector Lite.app` (1,167 files) was scanned in-session:

- no `prospecting_secrets.json`, personal configs, run history, builds,
  logs or `.bak` files inside the bundle;
- the bundled default config is the sanitized tracked one (37 keys,
  `WEBHOOK_ENABLED: false`, empty `WEBHOOK_URL`, no legacy keys);
- no forbidden brand/endpoint strings outside the shipped docs' sanctioned
  migration/history mentions;
- no personal filesystem paths in the bundled first-party sources;
- `build_info.json` carries the exact source commit.

Launch verification from the mounted read-only DMG (restricted PATH,
`PYTHONNOUSERSITE=1`, isolated `PP_DATA_DIR`): clean first boot, config
seeded from the sanitized default, and `lsof -a -p <pid> -i` shows **zero
sockets** owned by the process across boot + idle. Details:
`PRIVACY_VERIFICATION.md`.

## Automated regression gate

`python3 public_release_tests.py` fails the build if any of this reappears:
prohibited branding, tracking endpoints, fingerprint code, access-gate
tokens, credential-shaped strings, `shell=True`, unsafe exec, a
non-sanitized default config, version drift — plus four network-denied
runtime children (app boot offline, a full engine sim cycle offline with
all inputs released, webhook-disabled no-op, config scrub + migration).
CI runs it on every push on both platforms.
