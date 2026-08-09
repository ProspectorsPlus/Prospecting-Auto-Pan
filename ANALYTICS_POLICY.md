# Analytics policy

**Current state, Prospector Lite 5.0.0: the app contains no analytics and no
telemetry of any kind.** Normal startup and macro use make zero network
requests; the packaged-app acceptance run verifies 0 sockets on boot. The only
network paths in the app are opt-in and point at servers the user chooses (their
own Discord webhook, their own Coach API key). Evidence:
`docs/public-release/NETWORK_BEHAVIOR.md`, `PRIVACY.md`, and the in-app Trust
Center.

The only usage signal the project reads today is the **public GitHub release
download counter**, which is aggregate metadata about release assets and
contains no user data. `tools/release_download_stats.py` prints it.

## If anonymous product analytics are ever added

Any future analytics implementation is bound by the rules below. A change that
cannot meet them does not ship.

### Release discipline

- Analytics ship only in a **new, version-bumped release** with updated release
  notes and an updated `PRIVACY.md`. Existing release binaries are never
  replaced or patched.
- The public website and this repository must describe the behavior accurately
  before the release is published. Claims like "no telemetry" or "zero network
  requests by default" must be removed or qualified in the same release.

### Disclosure and control

- A visible preference appears on the **Welcome screen**, before the first
  analytics event is ever sent. It is not buried in Terms, Privacy text, or a
  collapsed disclosure.
- If the preference defaults to enabled, it is labeled as a statement of fact
  ("Share anonymous usage analytics"), never as consent language ("I consent"),
  because a pre-checked box is not an affirmative opt-in.
- The same setting is also available afterwards in Settings and the Trust
  Center. Disabling must not be visually harder than leaving it enabled, must
  never use guilt wording, and must never degrade or block the app.
- The app provides "View exactly what is collected" and a **payload preview**
  showing a real example event before anything is sent.

### When disabled

- Zero analytics requests. No event queueing for later. Session tracking stops
  immediately. The analytics ID is deleted and is not recreated unless the user
  re-enables analytics.

### Identity

- The only identifier is a **random locally generated UUID**. It must not be
  derived from hardware, MAC address, serial number, hostname, username, IP, or
  any Roblox or Discord identity. A visible **Reset analytics ID** control
  deletes it and generates a fresh one only if analytics remain enabled.

### Allowed event content

Coarse product facts only, bucketed where practical: app version, platform,
app/macro lifecycle events, mode id, non-sensitive warning and error codes,
bucketed session duration, reliability counters.

### Banned from any payload, without exception

- IP addresses (public or private), GPS or any location, city-level or finer
- Roblox usernames, Discord usernames, OS usernames, hostnames
- Serial numbers, hardware UUIDs, MAC addresses
- Screenshots or any captured pixels
- Build names or build contents, calibration data or coordinates
- File paths, typed keys, clipboard contents, raw logs or stack traces
- Webhook URLs, API keys, or any secret

If a hosting provider observes a source IP as part of ordinary HTTP transport,
that IP is never copied into the analytics store, never used to identify users,
server-log retention is minimized where configurable, and the hosting behavior
is disclosed accurately in `PRIVACY.md`.

### Engineering constraints

- HTTPS only, bounded timeouts, no aggressive retries, no secrets in logs.
- Analytics failure must never block or delay macro operation or app startup.
- Owner-facing dashboards show aggregates (actives, platform split, version
  adoption, completion and failure rates). They must not become person-tracking
  interfaces: no per-person activity timelines, no IP or location display.
- Tests must cover: default state, Welcome checkbox reflects stored state,
  disable-before-first-event sends nothing, disabled means zero requests, the
  ID is random and resettable, and the payload schema contains only allowed
  fields.

## History

An early private distribution of this project (pre-rebrand "Prospectors Plus")
posted launch cards with IP-derived location to a private Discord webhook. That
system was removed before the public 5.0.0 release, its webhook was revoked,
and the release pipeline's package scan now fails the build if those endpoint
patterns reappear. It is documented here so it is never reintroduced by
accident.
