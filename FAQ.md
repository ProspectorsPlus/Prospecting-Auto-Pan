# FAQ

**The FAQ lives inside the app.** Open it from the Help/Tutorial menu →
**"FAQ & troubleshooting"** — or from the warning drawer, Settings, the
Calibrate tab, the wizard's Readiness Check, or the Trust Center. It is
fully local (nothing is fetched), searchable across questions and
symptoms, and every entry ends with buttons that open the exact setting,
calibration, or permission it talks about.

The in-app version is the source of truth (`lite_diagnostics.py`,
`FAQ_ENTRIES` — validated against the real setting, calibration, and
permission registries by `diagnostics_tests.py`). This file is just the
index so you can see what is covered before opening the app.

## The 20 entries

1. I granted the permission, but after an update or reinstall macOS
   still says it is not granted.
2. Why does the app need Screen Recording, and why does detection
   return black frames?
3. What are Accessibility and Input Monitoring for, and is the app
   reading my keystrokes?
4. Windows says "Windows protected your PC" (SmartScreen) — is the
   download unsafe?
5. My Safe Stop hotkey does nothing while Roblox has focus.
6. The app cannot find the Roblox window.
7. Capacity calibration "succeeds" but runs misread the pan (or
   hard-stop). What is the right-end story?
8. What is Advanced Cue Matching (the masks), and why did it stop
   matching after I resized the window?
9. Auto Pan keeps turning off, or the tracker keeps kicking it.
10. The character keeps getting nudged back — why so many corrective
    nudges?
11. Shakes start too early, retry, or get marked as missed — how do I
    tune shake timing?
12. The macro keeps "recovering" over and over instead of panning.
13. The Pan / Collect Deposit / Shake prompt is on screen but the macro
    does not react.
14. I changed my display scaling / resolution / monitor arrangement and
    everything broke.
15. I moved or resized the Roblox window — do I have to recalibrate?
16. Earnings tracking shows nothing (money/shards stay empty or
    frozen).
17. The finds log misses finds, or shows ghost/duplicate entries.
18. Where are the logs and diagnostics, and how do I share them when
    asking for help?
19. How do I update the app safely, and what survives an update?
20. I imported or activated a Studio build/script and now Start refuses
    to launch.

If none of these covers your problem, see [SUPPORT.md](SUPPORT.md) for
how to ask for help.
