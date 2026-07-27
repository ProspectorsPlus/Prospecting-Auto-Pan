# Prospector Lite — Privacy verification (empirical)

What was actually executed and observed during the 1.0.0-rc.1 release pass,
distinct from what the code promises. Platform: macOS (Apple Silicon), the
only locally executable platform in that session; the same checks run on
Windows in CI (`.github/workflows/ci.yml`).

## Network-denied runtime tests (source)

`public_release_tests.py` replaces `socket.socket`, `create_connection` and
`getaddrinfo` with hard failures in isolated child processes
(`PP_DATA_DIR`/`PPENGINE_HOME` → throwaway temp dirs), then:

1. **App boot offline** — imports the real app, instantiates the API,
   walks welcome → continue, exercises webhook set/get validation and the
   whitelisted doc opener. Passes: zero network attempts.
2. **Full macro cycle offline** — replays the `classic-standard` scenario
   through the real engine main loop under the sim world. Passes; the
   world's input ledger confirms **every key/button released at exit**.
3. **Webhook disabled = zero network** — engine defaults
   (`WEBHOOK_ENABLED=False`, empty URL) make `post_webhook` a pure no-op
   under socket denial.
4. **Scrub + migration** — legacy `ACCESS_*`/`MACHINE_SALT`/`SYNC_URL`
   keys are dropped from loads, removed from disk once, and never copied
   by the data-dir migration (tested including a non-ASCII target path and
   idempotent re-run; the old directory's listing is byte-identical after).

Result in-session: **ALL PASS** (`python3 public_release_tests.py`).

## Packaged app, observed (mounted DMG)

The built DMG was mounted read-only and the packaged app launched with
`env -i` (restricted `PATH=/usr/bin:/bin`), `PYTHONNOUSERSITE=1` and an
isolated data dir:

- boot reached the GUI loop (all windows created) with **no system Python
  and no user site-packages** available;
- `lsof -a -p <pid> -i` at ~14 s and ~30 s: **0 sockets** owned by the
  process; no child processes were spawned at idle;
- the data dir afterward contained only `prospecting_config.json` (seeded
  from the sanitized bundled default: webhook off, no URL, no legacy keys)
  and an empty coach history — no identifier, no machine value, no
  screenshot, no unexpected file;
- the app terminated cleanly on signal; the DMG detached.

## What is deliberately NOT collected (verified by code removal + scans)

- public IP, geolocation, ISP (the `ip-api.com` beacon is gone);
- hardware identifiers (IOPlatformUUID / MachineGuid code removed);
- analytics events, crash uploads, usage pings (no sender exists);
- remote content fetches (update manifest, codes list, tutorial cache
  all removed);
- fonts or assets from CDNs (analytics window uses system fonts).

DNS note: with the socket layer denied, `getaddrinfo` is also denied, so
the tests prove no DNS lookup happens either during normal use.

## Remaining egress paths (opt-in only, user destinations)

| Path | Trigger | Destination | Default |
|---|---|---|---|
| Discord webhook notifications (engine + in-app test button) | user enables + saves their own `https://` URL | the user's webhook | off / empty |
| Coach AI mode | user enters their own API key and switches off the offline brain | the provider the user picks | offline brain |
| "View source" / doc links | user click | `PROJECT_URL` (empty ⇒ buttons hidden/no-op) / local files | inert |

Payload contents for the webhook are documented in
[NETWORK_BEHAVIOR.md](NETWORK_BEHAVIOR.md) and [PRIVACY.md](../../PRIVACY.md).

## Not verified here

- Windows runtime observation (no Windows machine in-session) — the same
  suite runs in CI on `windows-latest`; treat Windows runtime privacy as
  CI-verified only after the repo is pushed and the workflow is green.
- Live-game behaviour (screen capture of actual Roblox) — outside the
  scope of privacy verification; capture never leaves the machine.
