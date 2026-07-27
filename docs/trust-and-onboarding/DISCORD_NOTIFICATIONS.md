# Discord Notifications — The Complete Truth

Internal reviewer documentation for the webhook feature: the app's only network feature that
can run during a macro. Everything below is stated against the current working tree
(v1.0.0-rc.2) with repo-relative citations. The one-line summary that must stay true:
**off by default, user's own URL only, https-only, verified TLS always, one attempt, 8 s,
screenshots are a second separate opt-in that now defaults off.**

## Config keys and defaults

All engine defaults at `prospector_engine/engine.py:660-673`; the tracked default config
(`windows/prospecting_config.json`, shipped by both platforms) is pinned clean by
`public_release_tests.py:275-283`.

| Key | Default | Meaning |
|---|---|---|
| `WEBHOOK_ENABLED` | `False` (`engine.py:660`) | Master toggle. Off = the notify path is a pure no-op. |
| `WEBHOOK_URL` | `""` (`engine.py:661`) | The user's own Discord webhook URL. Empty disables even when the toggle is on. There is no developer endpoint anywhere. |
| `WEBHOOK_SECRET` | `""` (`engine.py:662`) | Optional `x-macro-secret` header value for a user's own relay bot. No secret is ever baked into source. |
| `WEBHOOK_USER` | `""` (`engine.py:663`) | Display name/id included in the payload so a relay bot knows whom to DM. |
| `NOTIFY_SCREENSHOT` | `False` (`engine.py:673`) | **Changed this release**: was `True` at rc.1 (`680f141`), so enabling the webhook implicitly opted into full-screen captures leaving the machine (CURRENT_SYSTEM.md §8.5). Now a genuine second opt-in. |
| `NOTIFY_*` per-event toggles | on | `post_webhook` honours a per-event flag map (`_EVENT_FLAG`, checked at `engine.py:3096-3098`). |

## https:// enforcement — three independent points

1. **Save time (app API):** `Api.webhook_set` rejects any non-empty URL not starting with
   `https://` (`prospecting_app.py:3681-3696`, check at :3684).
2. **Save/send time (test button):** `Api.test_webhook` re-checks before sending
   (`prospecting_app.py:2527-2529`); the JS Notifications page also pre-validates
   (`prospecting_app.py:9840`).
3. **Send time (engine):** `post_webhook` re-checks at the moment of send
   (`engine.py:3091-3095`) — a hand-edited config cannot downgrade the only sanctioned
   egress to cleartext; the notification is dropped with a log line instead.

## The mandatory-TLS sender

`_webhook_tls_context()` (`engine.py:3034-3048`) builds the context for every send:

- `ssl.create_default_context()` — **verification on, hostname checking on** (the library
  default; nothing weakens it).
- If the interpreter shipped with an **empty trust store** (`cert_store_stats()["x509_ca"]
  == 0`, a real hazard in bundled Pythons), it loads the **certifi CA bundle packaged with
  the app** (`certifi.where()`; certifi is installed by every build path —
  `build_dmg.command:41`, `windows/build.bat:12`, `.github/workflows/ci.yml:38-41`).
- **There is deliberately no unverified mode.** `_webhook_send` (`engine.py:3051-3078`)
  translates `ssl.SSLError` (direct or wrapped in `URLError.reason`) into
  `"TLS certificate verification failed (dropped)"` (:3072-3074) — the notification is
  dropped, never retried with verification off.

The rc.1 baseline had five `ssl._create_unverified_context` retry-after-cert-failure sites
(CURRENT_SYSTEM.md §6). All are removed: `git grep _create_unverified_context` over tracked
files returns **0 hits**. The app-side twin `_tls_context()` (`prospecting_app.py:448-463`,
used by `test_webhook` at :2552 and the Coach API path at :3500) implements the identical
policy: verified default context, certifi fallback trust store, fail-closed.

## Payload construction — `_webhook_payload`

`engine.py:3006-3031` builds exactly what is sent (and, by construction, exactly what the
in-app preview shows — see below):

- `username`: `"Prospector Lite"` (constant).
- `content`: the event message, capped at **1900** chars.
- `embeds[0]`: title `"Prospector Lite"`, description capped **1500**, colour `0xC2924C`,
  fields:
  - `User` — `WEBHOOK_USER` or `"(not set)"`, capped **100**;
  - `Event` — event name, capped **60**;
  - `Stats` — only when stats exist: pans / per-hour / runtime-minutes / recoveries joined,
    capped **200**.
- Raw top-level `event`, `user`, `stats` — for a custom relay reading the POST body directly
  (plain Discord webhooks drop unknown JSON keys).

No IP, no location, no machine identifier, no analytics ride along — the fields above are
the complete set. (`instance_id` / the local engine fingerprint never leave the machine —
CURRENT_SYSTEM.md §6.)

## Screenshot: a second, separate opt-in

`post_webhook(..., shot=True)` attaches an image only when **both** the call site requests it
and `NOTIFY_SCREENSHOT` is true (`engine.py:3100`). The encoder is
`_grab_screenshot_b64` (`engine.py:2986-3003`): full primary monitor, downscaled toward
`SHOT_TARGET_W` (~1280 px), base64 PNG added as `payload["screenshot"]` +
`screenshot_format: "png"` (`engine.py:3057-3059`). This is the **only** screenshot encoder
in the codebase, and the capability card for screen detection says so
(`lite_trust.py:140-142`). Default is now off (see the table above).

## Secret header handling and redaction

- Engine send: `x-macro-secret` header added only when `WEBHOOK_SECRET` is non-empty
  (`engine.py:3063-3064`). Test send: same (`prospecting_app.py:2543-2545`).
- The secret is **never logged** — failure paths print only the error class/HTTP code
  (`engine.py:3105-3107`).
- The in-app payload preview **redacts it by design**: the header value is replaced with
  `"(your secret -- never shown or logged)"` (`prospecting_app.py:2816-2818`).
- There is no UI field for the secret; it exists only for users who hand-edit their config
  for a relay bot (CURRENT_SYSTEM.md §6).

## Timeout and retry policy

- **8-second timeout**, both engine (`engine.py:3067`) and test send
  (`prospecting_app.py:2552`).
- **One attempt per event, ever.** `post_webhook` fires a single fire-and-forget daemon
  thread (`engine.py:3104-3109`); `_webhook_send` contains no retry loop and no backoff — a
  failure prints one log line. No retry storm is possible.

## The payload-preview API contract

`Api.webhook_payload_preview()` (`prospecting_app.py:2796-2826`) exists so users can see the
exact bytes **before** enabling the feature (rendered from the wizard/Trust Center card,
`prospecting_app.py:9986-9989`):

- It imports the engine and calls **the same `_webhook_payload` function that sends**, with
  example stats (`{"cycles": 120, "pans_per_hr": 96, "runtime_s": 4500, "recoveries": 1}`),
  temporarily substituting the user's saved `WEBHOOK_USER` — parity by construction, not by
  copy-paste.
- Returns `{ok, payload, headers, url_set, enabled, screenshot_optin}`. `headers` mirrors the
  real send headers (Content-Type, `User-Agent: ProspectorLite/1.0`, redacted
  `x-macro-secret` only when a secret is configured). `screenshot_optin` reflects
  `NOTIFY_SCREENSHOT` so the UI can state whether the separate opt-in is on.

## Regression tests that pin all of this

| Behaviour | Pinned by |
|---|---|
| Webhook off + URL empty by default (engine constants) | `public_release_tests.py:380-390` (`child_engine_webhook_default`, network-denied: a send attempt with defaults must be a no-op) |
| Shipped default config clean (enabled=False, no URL/secret) | `public_release_tests.py:275-283` |
| `http://` rejected at save, `https://` accepted, no-URL test refused | `public_release_tests.py:350-359` (`child_app_offline`) |
| Zero network at startup / during engine runs | network-denied children `public_release_tests.py:309-377` (socket module disabled; any attempt raises) |
| No credential-shaped strings (incl. real webhook URLs with numeric ids) in the tree | `public_release_tests.py:230-246` |
| No unverified-TLS fallback anywhere | `git grep _create_unverified_context` = 0 tracked hits; pinned as a scan in the onboarding/trust suite (`onboarding_trust_tests.py`, being written in parallel; CI at `.github/workflows/ci.yml:56-57`) |
| TLS policy (verified context, certifi fallback, SSLError → drop) and preview/send parity | the onboarding/trust suite, same file |

Honesty notes: the `windows/prospecting_app.py` mirror is synced in this same hardening pass
(CURRENT_SYSTEM.md §6); the Windows runtime itself has **not been executed** in this pass —
its rows in TEST_MATRIX.md are marked prepared, not executed. Historical git history contains
a real webhook URL and bot secret (`docs/public-release/SECRET_AUDIT.md`); they exist in
HISTORY only, the owner must revoke them and publish via the fresh-history procedure, and no
document may claim that revocation has already happened.
