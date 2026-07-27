# Privacy

Prospector Lite is a local desktop application. It has **no server side, no accounts, no login, no access codes, and no telemetry**. This document lists everything the app stores and every way data can leave your machine.

## What the app does NOT do

- No update checks, no "phone home" of any kind.
- No analytics or usage tracking.
- No IP address or geolocation collection.
- No remote content: every screen, tutorial, and help page ships inside the app.
- No transmission of your `instance_id`, hardware identifiers, or machine fingerprints anywhere.

Default network activity is **zero requests**. The complete inventory of code that *can* touch the network (all opt-in, all pointed at endpoints you choose) is in [docs/public-release/NETWORK_BEHAVIOR.md](docs/public-release/NETWORK_BEHAVIOR.md).

## Data stored on your machine

| Location | When |
|---|---|
| Next to the scripts | Running from source |
| `~/Library/Application Support/Prospector Lite` | Packaged macOS app |
| `%APPDATA%\Prospector Lite` | Packaged Windows app |

| File | Contents |
|---|---|
| `prospecting_config.json` | Settings and screen calibration (includes your webhook URL/secret if you set one) |
| `prospecting_builds.json` | Your saved builds |
| `prospecting_scripts.json` | Your Prospector Studio scripts |
| `run_history.json` | Run statistics kept for the Analytics page |
| `run_logs/` | Per-run engine logs (rotated; the app keeps a bounded number) |
| `prospecting_calib_log.csv` | Calibration session log written by the engine |
| `tutorial_content.json` | Your local edits to the built-in help |
| `onboarding_state.json` | Setup-wizard progress (which step you are on, declined capabilities); written atomically; resetting the wizard deletes only this |
| `instance_id` | A random UUID used only for the local Prospector Studio companion handshake; never transmitted |
| `coach_history.json` | Your Coach chat transcript (the text you typed and its replies), kept locally so conversations survive restarts |
| `prospecting_secrets.json` | Your Coach API key, if you set one. Stored in this data folder by packaged builds (next to the scripts, gitignored, when running from source); never bundled into builds |

**Deleting your data**: quit the app and delete the folder above (or individual files). That is the entire data footprint; nothing exists anywhere else.

**Managing your data in-app**: the Trust Center's **Local Data** section lists these files live with their sizes, and offers: Open folder, Export calibration, Export diagnostics, Delete history, Delete logs, and Delete ALL (double-confirmed). The delete actions are scoped to the known Prospector Lite files only — they never touch anything else. The diagnostics export (`export_diagnostics` in `prospecting_app.py`) is **secret-free by construction**: it contains readiness/identity/capability statuses and no webhook URL, no webhook secret, no API key, and no config dump. `prospecting_secrets.json` is never exported by any action.

## The two opt-in network features

### 1. Discord webhook notifications (default: off)

If you paste your own Discord webhook URL on the Notifications page and enable notifications (`WEBHOOK_ENABLED`, default `false`), the app posts run events to **that URL and nothing else**. Each payload contains (see `_webhook_payload` and `post_webhook` in `prospector_engine/engine.py`):

- the event name (`start`, `stop`, `autostop`, `bag_full`, `stats`, `safe_stop`, `recovery`, `error`, or `test`),
- a human-readable message,
- the display name you chose (`WEBHOOK_USER` — free text; leave it blank if you want),
- run statistics (pans, pans/hour, runtime, recoveries),
- optionally a downscaled screenshot of your screen, only for events that carry one and only while the separate "attach screenshot" toggle (`NOTIFY_SCREENSHOT`, **default `false` as of 1.0.0-rc.2**) is on. Remember that a screenshot shows whatever is on your screen at that moment.

You can see **exactly** what would be sent before enabling anything: the in-app payload preview is built by the same engine function that sends (`_webhook_payload`), so the preview and the wire payload cannot diverge.

If you set the optional `WEBHOOK_SECRET` config value (for self-hosted receivers), it is sent as an `x-macro-secret` HTTP header on those posts. It is never logged and is redacted in the payload preview. Per-event toggles let you disable individual notification types.

Delivery is over verified TLS only: `https://` is enforced at save and at send, certificates are always verified (falling back to the bundled `certifi` trust store if the interpreter ships without CA certificates), and there is no insecure retry — a host with a broken certificate simply does not receive the notification. See [SECURITY.md](SECURITY.md).

### 2. The Coach's AI mode (default: offline)

The Coach's default "offline brain" is a local rule engine — no network. If you switch it to API mode and enter **your own** API key, your chat message, the conversation so far, and a system prompt containing run context are sent directly to the provider you selected (Anthropic, OpenAI, Google Gemini, DeepSeek, or a custom/local base URL — see `_coach_api` in `prospecting_app.py`). Your key is stored in the local `prospecting_secrets.json` file, sent only to that provider, and never placed in the config file. The provider's own privacy policy applies to what you send it.

## Browser links

"View source" and documentation links open your OS browser only when you click them (`open_external` / `open_doc` in `prospecting_app.py`; `open_doc` accepts only a whitelist of bundled `.md` files). The app itself fetches nothing.

## Migration from "Prospectors Plus" (the pre-1.0 private builds)

On first run, a packaged build performs a **one-time, copy-only** import from an existing legacy data folder (`~/Library/Application Support/Prospectors Plus` or `%LOCALAPPDATA%\Prospectors Plus`) if one exists:

- Only user data is copied: config, builds, scripts, run history, tutorial edits.
- The legacy private keys `ACCESS_OK`, `ACCESS_HASH`, `ACCESS_MACHINE`, `MACHINE_SALT`, and `SYNC_URL` (the old access-gate and analytics fields) are **stripped and never carried over**.
- The old folder is never modified or deleted.
- A marker file (`.migrated_from_prospectors_plus`) makes the migration run at most once, and the welcome screen tells you it happened.

See `_migrate_legacy_data` in `prospecting_app.py`.

## Verifying this document

```sh
grep -rn --include='*.py' -E 'urllib|urlopen|requests\.|socket' \
    prospecting_app.py prospector_engine/ prospecting_ui.py prospecting_assistant.py
```

Every match should belong to the webhook path, the Coach API path, or an import line. If you find one that does not, please report it — see [SECURITY.md](SECURITY.md).
