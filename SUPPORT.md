# Getting help with Prospector Lite

## Self-help first (fastest)

- **In-app tutorial** — the Tutorial menu walks through setup, calibration, and every page. It also reopens the welcome/privacy/version screen ("Welcome, privacy & version").
- **Re-run the setup wizard** — Tutorial menu → "Re-run setup wizard" (also available from the Trust Center). It re-checks permissions, calibration, and readiness step by step; re-running deletes nothing.
- **Trust Center** — the permanent Trust Center tab shows live permission status with one-click in-app tests, your build identity, network behavior, and your local data files. Most "nothing is detected" or "hotkeys don't work" problems are visible there as a missing permission.
- **Calibration problems** — most detection issues are calibration issues. Re-run the Calibrate tab after any game UI update, resolution change, or monitor swap.
- **README troubleshooting section** — [README.md](README.md) covers the common cases: macOS permissions, unsigned-app first launch, webhook test failures.
- **Run logs** — each run writes a log under `run_logs/` in your data directory (see README for the location). Attach the relevant log when you ask for help; it contains detection events, not personal data — but skim it before sharing, like anything you upload.

## Asking for help

The project is not published yet, so there is no public issue tracker at this time: `<repository URL — to be filled in when published>`. Once it is public, use **GitHub Issues** for bugs and questions, and **GitHub Discussions** if enabled.

A good report includes:

1. Prospector Lite version (welcome screen, About, or the Trust Center shows it — currently 1.0.0-rc.4) and whether you run from source or a packaged build.
2. OS and version (macOS/Windows).
3. What you did, what you expected, what happened instead.
4. The tail of the newest file in `run_logs/`, if the problem happened during a run.
5. A screenshot if the problem is visual (calibration, detection, UI).

## What NOT to post

- Your Discord webhook URL or `WEBHOOK_SECRET` — anyone who has the URL can post to your channel.
- Your Coach API key (or any content of `prospecting_secrets.json`).
- Full config files without checking them first (the config contains your webhook URL if you set one).

## Security problems

Do **not** report vulnerabilities in public issues — see [SECURITY.md](SECURITY.md) for the private reporting path.

## Scope of support

This is a free project whose source is available for inspection, without a support team. Best-effort answers only. Unsupported: the old private "Prospectors Plus" builds, forks, and anything about circumventing Roblox moderation — the project makes no promises about account safety (see the disclaimer in [README.md](README.md)).
