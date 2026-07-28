# Build Identity & Trust Manifest — Reviewer Reference

Internal reviewer documentation for the code-reference system: how a build knows exactly what
it is (`build_info.json`), how every capability card's "View code" button is guaranteed to
point at the exact source it was built from (`trust_manifest.json`), and why a wrong link is
a build failure rather than a shipped bug.

> Line citations below were resolved against the 1.0.0-rc.2 baseline (commit `eb9bbba`) and
> are historical — the rc.3 stabilization pass shifted several of them. The mechanism
> descriptions remain accurate; for exact current line numbers use the shipped
> `trust_manifest.json`, which is regenerated from source at every build.

## build_info.json — fields and who stamps them

Schema (all producers write the same eight fields):

```json
{"commit": "<full sha>", "date": "YYYY-MM-DD", "version": "<VERSION>",
 "dirty": false, "package": "dmg|windows", "project_url": "",
 "signed": false, "notarized": false}
```

| Producer | Where | Notes |
|---|---|---|
| macOS local/CI build | `build_dmg.command:50-68` | commit + dirty from git; version regex-extracted from `prospecting_app.py`; `package: "dmg"`; `project_url` from the `PP_PROJECT_URL` env; `signed` true only when `CODESIGN_ID` is set; `notarized` always false (notarization is a manual owner step, `RELEASING.md`). CI macOS builds run this same script (`.github/workflows/build-macos.yml:32`). |
| Windows local build | `windows/build.bat:18` | same fields; `package: "windows"`; `signed: false` (local). |
| Windows CI build | `.github/workflows/build-windows.yml:37` | commit from `GITHUB_SHA`; `dirty: false` (CI trees are clean checkouts); optional Authenticode signing runs afterwards only when the `WINDOWS_CERT_PFX_B64` / `WINDOWS_CERT_PASSWORD` secrets exist (`build-windows.yml:71-83`, signtool + timestamp server); no certificate lives in the repo. |

Consumption: `lite_trust._read_build_info` (`lite_trust.py:965-976`) reads the bundled copy
(`sys._MEIPASS`, falling back to `build/build_info.json`);
`build_identity(version, project_url)` (`lite_trust.py:988-1017`) merges it with the app's
constants — frozen builds trust the stamp, **source runs ask git directly** (`_git`,
`:979-985`) including a live dirty check. A dirty tree sets `development_build: True`
(`:1015-1016`) and the Trust Center prints "(built from modified source)"
(`prospecting_app.py:10152`). The identity also carries the honest licence line:
"source-available; no open-source licence chosen yet" (`:1012-1013`) — the product is never
described as open source. About, the welcome screen and the Trust Center all render this
identity (`Api.app_info`; welcome fill `prospecting_app.py:9862-9869`; Trust Center
`:10151-10155`, including `Signed: no (unsigned build)` when applicable).

## The trust manifest and its ast resolver

`generate_manifest(version, project_url)` (`lite_trust.py:1087-1118`) resolves every
capability's `source_references` (module, symbol, why) into
`{file, symbol, line_start, line_end, why, url}` and wraps them with
`{schema: 1, generated_from: <commit>, version, project_url, note}`.

`_resolve_ref(module_name, symbol)` (`lite_trust.py:1040-1084`) is the heart, and its design
constraints matter:

- **Static, no imports.** It `ast.parse`s the module source on disk. Nothing is imported and
  no side effects run — which also means platform-specific modules resolve identically
  everywhere (`platform_win` resolves on a Mac build machine and vice versa).
- **Dotted symbols** walk class bodies (`Sensing.save_pixels` → the method's exact range);
  the start line includes decorators (`:1082-1083`).
- **Nested-symbol fallback:** a `def` inside a `def` is found by walking the whole tree for
  the final name — but only a **unique** match is accepted. Ambiguity raises
  (`"symbol %r ambiguous"`), and a missing symbol raises (`"symbol %r not found"`)
  (`:1067-1081`).
- **Dead or ambiguous reference ⇒ build failure.** Both build scripts run
  `lite_trust.py --emit` (`build_dmg.command:69`, `windows/build.bat:19`); `generate_manifest`
  propagates the resolver's exception, `--emit` exits non-zero, and the build stops. A
  refactor that moves or renames referenced code cannot ship a stale link — it cannot ship
  at all until the registry is updated.

`--emit` (`_main`, `lite_trust.py:1151-1183`) regex-extracts `VERSION` and (when
`PP_PROJECT_URL` is unset) `PROJECT_URL` from `prospecting_app.py`, writes atomically
(tmp + `os.replace`), and prints the capability count + source commit.

## Exact-commit URL policy

`source_url_for(ident, path, line_start, line_end)` (`lite_trust.py:1020-1033`):

- Emits `<project_url>/blob/<full commit>/<path>#L<a>-L<b>` — **always the exact commit,
  never a branch name**, so a link a user opens in six months still shows the code their
  build runs.
- Returns `""` when `project_url` is empty **or** the commit is unknown. This is the honesty
  rule: `PROJECT_URL` in `prospecting_app.py:46` is currently `""` pending the owner's
  publication (with the fresh-history procedure of
  `docs/public-release/SECRET_AUDIT.md` — a hard precondition in `RELEASING.md`), so today
  every URL is empty by design.
- The UI's fallback is explicit, not silent: `Api.trust_view_code`
  (`prospecting_app.py:2768-2794`) opens the URL when present, otherwise returns
  `file :: symbol (lines a-b) @ commit <short>` with the note "No public repository URL is
  configured in this build, so the exact local reference is shown instead." The Trust Center
  states the same policy in prose (`prospecting_app.py:10156-10158`). There is **no**
  fallback to a moving branch anywhere.

## Frozen vs dev manifest loading

`load_manifest(version, project_url)` (`lite_trust.py:1121-1148`):

| Mode | Behaviour |
|---|---|
| Frozen (packaged) | Read the bundled `trust_manifest.json` (`sys._MEIPASS`, then `build/`). If missing (a packaging bug), return an honest error dict (`"trust manifest missing from this package"`) — never a fabricated manifest. |
| Dev (source run) | Generate live from the working tree, so line numbers always match the files on disk. If generation fails (e.g. running from an incomplete checkout), fall back to a previously emitted `build/trust_manifest.json`, else return the error dict. |

`Api.trust_manifest` (`prospecting_app.py:2761-2766`) exposes this to the UI; the Trust
Center renders the full manifest JSON in a collapsible section with its source commit
(`prospecting_app.py:10159`).

## Why this design is trustworthy

The chain is: build script stamps commit → same script generates the manifest **from that
exact checkout** (`build_dmg.command:45-49` comment states this intent) → resolver refuses
to guess → URLs pin the stamped commit or honestly decline. The only trusted inputs are the
repo itself and git; there is no hand-maintained line-number table to rot.

Current resolution snapshot (dev tree, for orientation — regenerate with
`python3 lite_trust.py --emit`): 11 capabilities, e.g. `screen_detection` →
`prospector_engine/sensing.py :: Sensing._grab_full` L135-141, `input_control` →
`prospector_engine/engine.py :: release_all` L3118-3140, `discord_notifications` →
`prospector_engine/engine.py :: _webhook_tls_context` L3034-3048. NOT_REQUIRED entries
resolve to empty reference lists by design.

## Tests that verify link integrity

- **Build-time:** the `--emit` step in both build scripts is itself the hard gate (dead ref
  = failed build); the packaged smoke test then proves the frozen binary boots and answers
  `--capabilities` (`build_dmg.command:106-109`, `.github/workflows/build-macos.yml:47-49`).
- **The onboarding/trust suite** (`onboarding_trust_tests.py`, being written in parallel with
  this document; CI at `.github/workflows/ci.yml:56-57`): every registry reference resolves;
  resolved ranges are sane (start ≤ end, file exists and is tracked); with an empty
  `project_url` every manifest `url` is empty (no accidental branch links); with a configured
  URL every link embeds the full commit; `build_identity` fields present on all platforms.
- **Windows stamping** is prepared in `windows/build.bat` / `build-windows.yml` but — like
  the rest of the Windows runtime this pass — has **not been executed**; see TEST_MATRIX.md.
