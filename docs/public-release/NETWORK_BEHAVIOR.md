# Prospector Lite — Network behavior

**Default policy: zero network requests.** A fresh install with untouched settings never opens a connection — no update checks, no analytics, no telemetry, no remote content, no DNS lookups of its own. The GUI↔engine link is a subprocess stdio pipe (`prospector_engine/ipc.py`), not a socket.

Below is the complete inventory of every code path in the shipped application that *can* touch the network. There are no others; the verification commands at the bottom let you confirm that.

## Inventory of network-capable code paths

| # | File / function | Trigger | Destination | Payload | Default |
|---|---|---|---|---|---|
| 1 | `prospector_engine/engine.py` — `post_webhook` → `_webhook_send` | Run events (start, stop, autostop, bag full, stats, safe stop, recovery, error) while a run is active | The Discord webhook URL the **user** pasted (`WEBHOOK_URL`) — nothing is preconfigured | JSON: event name, message text, user-chosen display name (`WEBHOOK_USER`), run stats (pans, pans/hour, runtime, recoveries), Discord embed of the same; optional downscaled screenshot (base64 PNG) when the event carries one and `NOTIFY_SCREENSHOT` is on (a **separate opt-in, default off**); `x-macro-secret` header iff the user set `WEBHOOK_SECRET` | **Off** (`WEBHOOK_ENABLED = False`; empty URL) |
| 2 | `prospecting_app.py` — `test_webhook` | User clicks "send a test" on the Notifications page | Same user-owned webhook URL (must start with `https://`) | A fixed test message in the same payload shape (no screenshot) | Never fires without a click and a configured URL |
| 3 | `prospecting_app.py` — `_coach_api` | User sends a Coach message while Coach mode = API (requires the user's own key) | `https://api.anthropic.com/v1/messages`, or an OpenAI-compatible endpoint: `https://api.openai.com/v1`, `https://generativelanguage.googleapis.com/v1beta/openai` (Gemini), `https://api.deepseek.com/v1`, or a custom/local base URL — whichever the user selected | The user's message, prior conversation, and a system prompt containing run context; auth header carries the user's key | **Offline** (`COACH_MODE` defaults to the local rule engine; no key = no call) |
| 4 | `prospecting_app.py` — `open_external` / `open_doc` | User clicks a "view source"/doc link | The OS default browser opens the URL / a whitelisted bundled `.md` file | None sent by the app itself — the browser does the fetching | Only on click; with no `PROJECT_URL` configured, `open_external` with no URL does nothing |

Notes on the table:

- TLS certificate verification is **mandatory** on paths 1–3 — there is no unverified mode. Each site builds a verifying context (`_webhook_tls_context` in `prospector_engine/engine.py`, `_tls_context` in `prospecting_app.py`); when a bundled Python ships an empty default trust store, the packaged `certifi` CA bundle is loaded instead — still verified, never unverified. A certificate that cannot be verified fails the request with an explicit error (`ssl.SSLError` → dropped, surfaced in the UI); there is no retry with verification off. `https://` is enforced at save time **and** re-checked at send time on both webhook paths.
- The exact webhook payload can be inspected in-app before anything is enabled: `webhook_payload_preview` (`prospecting_app.py`) builds it with the **same engine function that sends it** (`_webhook_payload` in `prospector_engine/engine.py`), so the preview cannot drift from reality. The optional `x-macro-secret` header appears redacted in the preview and is never logged.
- Path 1 sends the User-Agent `ProspectorLite/1.0`. If notifications are enabled but no URL is set, the event is dropped with a local log line — nothing is sent anywhere.
- Per-event toggles (`NOTIFY_START`, `NOTIFY_STOP`, `NOTIFY_STATS`, `NOTIFY_SAFE_STOP`, `NOTIFY_RECOVERIES`, `NOTIFY_ERRORS`) further gate path 1.
- The `WEBHOOK_SECRET` value is never logged and never leaves the machine except as that header to the user's own URL.

## Things that never touch the network

- Version/About info (`app_info` in `prospecting_app.py`) — local constants and the bundled `build_info.json` only. There is deliberately **no auto-update mechanism**.
- `instance_id` — a local UUID for the Prospector Studio companion handshake; written to the data dir, never transmitted.
- Calibration, detection, analytics, run history, tutorial — all local.
- The engine process has exactly one sanctioned egress function (`post_webhook`); everything else in `prospector_engine/` is screen capture, input synthesis, and stdio.

## Remote content in the UI

None by default. The single potential vector is a YouTube `<iframe>` on tutorial cards, rendered only when a tutorial entry carries a video id. Tutorial content is owner-authored and stored locally (`tutorial_content.json`) — the shipped content sets no video ids, so no embed, and no request to YouTube, ever loads unless the user edits their own local tutorial file to add one. There is no mechanism that fetches tutorial content remotely.

## Verify it yourself

```sh
# All network-stack usage in the shipped Python files:
grep -rn -E 'urllib|urlopen|http\.client|requests\.|socket\b' \
    prospecting_app.py prospecting_ui.py prospecting_assistant.py prospector_engine/

# Expect matches only in: engine.py (webhook), prospecting_app.py (webhook test,
# coach, webbrowser/open_external), prospecting_ui.py (see below), plus imports.
#
# prospecting_ui.py's matches are a LOCAL fallback, not egress: when you run
# from source without pywebview installed, it serves the settings UI to your
# own browser on a socket bound to 127.0.0.1 only. Packaged builds never run
# this path (pywebview is always bundled), and it accepts no remote traffic.

# No listening ports / sockets in the IPC layer:
grep -n 'socket' prospector_engine/ipc.py    # no matches

# No unverified-TLS code anywhere in the tree:
grep -rn '_create_unverified_context' --include='*.py' .   # no matches

# Watch it live (macOS example): run the app, then
lsof -a -p <pid> -i   # shows no network connections until you use an opt-in feature
```
