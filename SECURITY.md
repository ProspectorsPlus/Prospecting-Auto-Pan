# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 1.0.0-rc.x | Yes |
| Anything older ("Prospectors Plus" 1.x–4.x) | No — those were private pre-releases and are not supported |

## Reporting a vulnerability

Please report vulnerabilities **privately** rather than in a public issue.

- Once the repository is public: use **GitHub private vulnerability reporting** (the "Report a vulnerability" button under the repository's Security tab / Security Advisories).
- Until then: `<security contact — to be filled in when the repository is published>`.

Include what you found, the file/function involved, and reproduction steps if you have them. You should get an acknowledgment within a reasonable time; coordinated disclosure is appreciated.

## Security model in brief

Prospector Lite is a local desktop app with no server side:

- **No network by default.** The app makes zero requests unless you configure one of two opt-in features (your own Discord webhook; the Coach's AI mode with your own API key). The complete inventory of network-capable code paths is in [docs/public-release/NETWORK_BEHAVIOR.md](docs/public-release/NETWORK_BEHAVIOR.md).
- **Mandatory TLS.** Every network path (webhook and Coach) enforces `https://` and verifies TLS certificates; there is no insecure fallback (verify: `git grep _create_unverified_context` returns nothing). If the Python interpreter ships with an empty default trust store, the bundled `certifi` CA bundle is used instead — never unverified mode. A request whose certificate cannot be checked fails safely and is not retried without verification.
- **External-only game interaction.** Screen capture and OS input synthesis only — no process memory access, no injection, no drivers. Evidence and verification commands: [docs/public-release/ROBLOX_SAFETY_BOUNDARY.md](docs/public-release/ROBLOX_SAFETY_BOUNDARY.md).
- **Untrusted-input handling.** Imported files (`.ppbuild`, `.ppscript`, calibration `.json`) are parsed as JSON, strictly validated and sanitized, and never evaluated as code. Studio scripts run in a data-walking interpreter with a hard-coded key whitelist and step/time budgets (see `docs/public-release/ARCHITECTURE.md`).
- **Secrets handling.** The Coach API key is written only to `prospecting_secrets.json`, which is gitignored, never bundled into builds, and was never committed (verified: `git log --all -- prospecting_secrets.json` is empty). The optional `WEBHOOK_SECRET` is sent only as an `x-macro-secret` header to the webhook URL you configured and is never logged.
- **Diagnostics are local-only.** The diagnostic/recommendation system added in 1.0.0-rc.6 (`lite_diagnostics.py`) is a pure local computation over data the app already holds — run statistics, engine safety events, calibration statuses, permission states. It is computed and stored locally (`diagnostics_state.json`: suppressions, apply/undo snapshots, bounded history), never transmitted, adds **no new network behavior** (the network inventory in [docs/public-release/NETWORK_BEHAVIOR.md](docs/public-release/NETWORK_BEHAVIOR.md) is unchanged), and captures no screenshots silently — the only image the related "Test capacity calibration" action produces comes from a button you click and is shown once in-app.

The full threat model, including what was removed before the public release, is in [docs/public-release/THREAT_MODEL.md](docs/public-release/THREAT_MODEL.md). The audit of secrets in the repository history is in [docs/public-release/SECRET_AUDIT.md](docs/public-release/SECRET_AUDIT.md).

## Known limitations (accepted, documented)

1. **Unsigned macOS build.** There is no Apple Developer certificate yet, so the DMG is unsigned and not notarized. Verify the SHA-256 checksum of anything you download against the published `SHA256SUMS.txt`, or build from source.
2. **Local files are plain JSON.** Your webhook URL, optional webhook secret, and Coach API key are stored unencrypted in your user data directory, protected only by ordinary file permissions. Anyone with access to your user account can read them.

> **Removed in 1.0.0-rc.2**: the SSL-unverified webhook/Coach fallback that earlier release candidates documented here. TLS certificate verification is now mandatory on every network path, with the bundled `certifi` trust store covering interpreters that ship without CA certificates. Delivery to a host with a broken certificate fails safely rather than sending unverified.

## How to verify claims yourself

```sh
# No process-memory / injection APIs anywhere in the Python tree:
grep -rn --include='*.py' -E 'ptrace|task_for_pid|ReadProcessMemory|WriteProcessMemory|CreateRemoteThread|VirtualAllocEx' .

# Every use of the network stack (should match only the webhook and Coach paths):
grep -rn --include='*.py' -E 'urllib|urlopen|http\.client|requests\.|socket' \
    prospecting_app.py prospector_engine/ prospecting_ui.py prospecting_assistant.py

# No unverified-TLS fallback anywhere:
git grep _create_unverified_context

# The secrets file was never committed:
git log --all -- prospecting_secrets.json
```

Two build-time artifacts help you tie a running app back to source:

- **Trust manifest** (`build/trust_manifest.json`, generated by `lite_trust.py --emit`): for every capability the app declares (screen detection, input control, stop hotkeys, notifications, Coach), the exact source files, symbols, and line ranges implementing it — resolved from the AST of the exact source being packaged; a stale reference fails the build. The Trust Center's "View code" buttons read this manifest.
- **Build identity** (`build/build_info.json`): the exact commit, date, version, package type, whether the source tree was dirty ("development build"), and the signing/notarization state. Shown in About, the welcome screen, and the Trust Center, so you can compare a build against the repository history.
