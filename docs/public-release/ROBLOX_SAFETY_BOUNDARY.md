# Prospector Lite — Roblox safety boundary

This document states exactly how the app interacts with the game, what it categorically never does, and how to verify both from the source. It makes **no claim of endorsement** by Roblox or the *Prospecting* developers, and no claim that using it is free of account risk — automation may violate the Roblox Terms of Use.

## What it never does

| Never | Evidence |
|---|---|
| Attach to or read/write the Roblox process's memory | No `ptrace`, `task_for_pid`, `ReadProcessMemory`, `WriteProcessMemory`, `CreateRemoteThread`, `VirtualAllocEx`, or `OpenProcess` anywhere in the Python tree — verified by grep (command below) |
| Inject code (DLL/dylib) into any process | No injection APIs, no bundled native libraries beyond the ordinary Python dependency wheels |
| Modify Roblox files, settings, or install anything into the game | The code never opens paths under the Roblox installation; all file I/O targets the app's own data directory |
| Intercept, read, or modify network traffic | No packet capture, no proxying, no sockets at all in the game-facing code (the IPC layer is stdio; see `prospector_engine/ipc.py`) |
| Install drivers, kernel extensions, or services | Nothing of the kind exists in the tree or the installers; the app runs as a normal user process |
| Require admin/root | macOS needs only the user-granted Screen Recording + Accessibility permissions; the Windows app runs unelevated |

## What it actually does (the complete game-facing surface)

All game interaction lives in two files: `prospector_engine/platform_mac.py` and `prospector_engine/platform_win.py`.

**Reading (screen only):**

- `mss` screen capture on both platforms — pixels of the display, exactly what a screenshot sees.
- macOS: `Quartz.CGWindowListCopyWindowInfo` / `CGDisplayBounds` to find the Roblox *window geometry* (owner name and bounds — metadata the OS gives every app; not process internals).
- Windows: `EnumWindows` / `GetClientRect` / `ClientToScreen` via `ctypes` for the same window-geometry lookup, plus DPI-awareness calls so capture coordinates line up.

**Writing (ordinary OS input only):**

- macOS: Quartz `CGEventPost` at the HID event tap — the same event stream a keyboard/mouse produces.
- Windows: `SendInput` with hardware scancodes — the standard OS input-synthesis API.

That is the entire boundary: pixels in, synthetic keystrokes/clicks out. The detection logic (`sensing.py`, `vision.py`, `engine.py`) operates purely on the captured pixel arrays.

## Verify it yourself

```sh
# 1) No process-memory / injection APIs anywhere:
grep -rn --include='*.py' -E \
  'ptrace|task_for_pid|ReadProcessMemory|WriteProcessMemory|CreateRemoteThread|VirtualAllocEx|OpenProcess' .
# -> no matches

# 2) The complete list of OS-level calls the platform layers make:
grep -n -E 'Quartz\.|CGEvent|CGWindow|CGDisplay|mss' prospector_engine/platform_mac.py
grep -n -E 'SendInput|EnumWindows|GetClientRect|windll|mss' prospector_engine/platform_win.py

# 3) No sockets in the game-facing code:
grep -rn 'socket' prospector_engine/
# -> no matches (IPC is stdio)
```

## Honest framing

- "External" describes the *mechanism*, not a promise of invisibility. Synthesized input can still look automated to server-side systems.
- The permissions the app asks for (macOS Screen Recording + Accessibility) are exactly the two capabilities the mechanism needs — see [README.md](../../README.md).
- Nothing in this document is a safety guarantee for your account, and none of it implies Roblox's approval.
