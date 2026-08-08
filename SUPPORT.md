# Getting help with Prospector Lite

## Self-help first (fastest)

- **The warning drawer** — if something is wrong, a red or yellow count badge usually already sits on the Calibrate, Cycle, or Trust tab. Click it: the drawer explains what was observed (with the actual numbers), the most likely cause with an honest confidence label, and the exact setting, calibration, or permission to fix — with an Open button that jumps to the precise control, and a bounded Apply/Undo for suggested values. Start there before anything else. See [DIAGNOSTICS.md](DIAGNOSTICS.md).
- **The in-app FAQ** — Help/Tutorial menu → "FAQ & troubleshooting" (also reachable from the drawer, Settings, Calibrate, the wizard's Readiness Check, and the Trust Center). Twenty searchable entries covering permissions, calibration, detection, and tuning, each ending with a deep link to the exact surface to fix. See [FAQ.md](FAQ.md) for the entry list.
- **In-app tutorial** — the Tutorial menu walks through setup, calibration, and every page. It also reopens the welcome/privacy/version screen ("Welcome, privacy & version").
- **Re-run the setup wizard** — Tutorial menu → "Re-run setup wizard" (also available from the Trust Center). It re-checks permissions, calibration, and readiness step by step; re-running deletes nothing.
- **Trust Center** — the permanent Trust Center tab shows live permission status with one-click in-app tests, your build identity, network behavior, and your local data files. Most "nothing is detected" or "hotkeys don't work" problems are visible there as a missing permission.
- **Calibration problems** — most detection issues are calibration issues. Re-run the Calibrate tab after any game UI update, resolution change, or monitor swap.
- **README troubleshooting section** — [README.md](README.md) covers the common cases: macOS permissions, unsigned-app first launch, webhook test failures.
- **Run logs** — each run writes a log under `run_logs/` in your data directory (see README for the location). Attach the relevant log when you ask for help; it contains detection events, not personal data — but skim it before sharing, like anything you upload.

## Asking for help

Use **GitHub Issues** for bugs and questions: <https://github.com/ProspectorsPlus/Prospecting-Auto-Pan/issues> (and **GitHub Discussions** if enabled).

A good report includes:

1. Prospector Lite version (welcome screen, About, or the Trust Center shows it — currently 5.0.0) and whether you run from source or a packaged build.
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
