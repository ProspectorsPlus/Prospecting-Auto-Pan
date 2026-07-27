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
- **External-only game interaction.** Screen capture and OS input synthesis only — no process memory access, no injection, no drivers. Evidence and verification commands: [docs/public-release/ROBLOX_SAFETY_BOUNDARY.md](docs/public-release/ROBLOX_SAFETY_BOUNDARY.md).
- **Untrusted-input handling.** Imported files (`.ppbuild`, `.ppscript`, calibration `.json`) are parsed as JSON, strictly validated and sanitized, and never evaluated as code. Studio scripts run in a data-walking interpreter with a hard-coded key whitelist and step/time budgets (see `docs/public-release/ARCHITECTURE.md`).
- **Secrets handling.** The Coach API key is written only to `prospecting_secrets.json`, which is gitignored, never bundled into builds, and was never committed (verified: `git log --all -- prospecting_secrets.json` is empty). The optional `WEBHOOK_SECRET` is sent only as an `x-macro-secret` header to the webhook URL you configured and is never logged.

The full threat model, including what was removed before the public release, is in [docs/public-release/THREAT_MODEL.md](docs/public-release/THREAT_MODEL.md). The audit of secrets in the repository history is in [docs/public-release/SECRET_AUDIT.md](docs/public-release/SECRET_AUDIT.md).

## Known limitations (accepted, documented)

1. **SSL-unverified webhook fallback.** Webhook delivery (`_webhook_send` in `prospector_engine/engine.py`, and the matching `test_webhook` in `prospecting_app.py`; the Coach API call has an equivalent fallback) retries once with certificate verification disabled if the first attempt fails for a non-HTTP reason. This exists because some older macOS Python installs ship without CA certificates. Consequence: on a hostile network, the retried request could be intercepted. It only affects the opt-in webhook/AI features, and only after a verified attempt has already failed.
2. **Unsigned macOS build.** There is no Apple Developer certificate yet, so the DMG is unsigned and not notarized. Verify the SHA-256 checksum of anything you download against the published `SHA256SUMS.txt`, or build from source.
3. **Local files are plain JSON.** Your webhook URL, optional webhook secret, and Coach API key are stored unencrypted in your user data directory, protected only by ordinary file permissions. Anyone with access to your user account can read them.

## How to verify claims yourself

```sh
# No process-memory / injection APIs anywhere in the Python tree:
grep -rn --include='*.py' -E 'ptrace|task_for_pid|ReadProcessMemory|WriteProcessMemory|CreateRemoteThread|VirtualAllocEx' .

# Every use of the network stack (should match only the webhook and Coach paths):
grep -rn --include='*.py' -E 'urllib|urlopen|http\.client|requests\.|socket' \
    prospecting_app.py prospector_engine/ prospecting_ui.py prospecting_assistant.py

# The secrets file was never committed:
git log --all -- prospecting_secrets.json
```
