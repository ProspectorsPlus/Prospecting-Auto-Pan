# Prospector Lite — Secret audit (repository history)

An audit of secrets in the working tree and the full git history, performed before public release. All findings are redacted — no secret values appear in this document.

## Summary

| # | Finding | Where | Status |
|---|---|---|---|
| 1 | A real Discord webhook URL was committed | History: commit `6842a47` ("analytics: set the webhook destination in the bundled config", 2026-06-30) — `prospecting_config.json`, `windows/prospecting_config.json`, and the pre-built Windows zip | Scrubbed from current files in commit `7ff6537`; **still present in history** → webhook must be revoked and the public repo must start from fresh history |
| 2 | The notify-bot `WEBHOOK_SECRET` value and its endpoint were committed as baked fallbacks | History: commit `e0f3d4f` ("Fix Discord notifications: … bake URL/secret as macro fallback") | Scrubbed from current files in commit `7ff6537`; **still present in history** → the bot secret must be rotated; covered by the same fresh-history requirement |
| 3 | Hashed access codes | History: `docs/codes.json` (the pre-1.0 invite gate) | Hashes only — no plaintext codes were ever committed; file is untracked/gitignored now; the gate itself was removed from the app |
| 4 | Personal API-key file | `prospecting_secrets.json` | **Never committed** (verified: `git log --all -- prospecting_secrets.json` returns nothing); gitignored; never bundled into builds |
| 5 | AI-provider API keys | Full history (`git log --all -S sk-proj` / `-S sk-ant`) | Only UI placeholder text, never a real key value |

## Finding 1 — committed webhook URL (the blocking one)

- **What happened:** on 2026-06-30, commit `6842a47` wrote a live Discord webhook URL into the two tracked config files and the bundled Windows zip, as the destination for the (since-removed) owner-analytics notifications.
- **Current state:** commit `7ff6537` scrubbed it from the tracked files. The currently tracked config (`windows/prospecting_config.json`) carries empty `WEBHOOK_URL`/`WEBHOOK_SECRET` and no legacy keys — re-verifiable at any time:

  ```sh
  python3 -c "import json; d=json.load(open('windows/prospecting_config.json')); \
      print({k: d.get(k) for k in ('WEBHOOK_URL','WEBHOOK_SECRET')})"
  ```

- **Why it still matters:** git history is part of a published repository. Anyone cloning the repo as-is could recover the URL and post to that Discord channel.
- **Required remediation (tracked in [RELEASING.md](../../RELEASING.md)):**
  1. **Revoke** the exposed webhook on the Discord side (delete/regenerate it) — do this regardless of anything else; treat it as compromised.
  2. **Publish from fresh history**: the public repository must start from a new initial commit, or from a history rewritten with the owner's explicit approval. Pushing the existing history is prohibited.

## Finding 2 — hashed access codes in history

`docs/codes.json` (added for the pre-1.0 invite gate, later untracked in commit `b531240`) contained **hashed** access codes only; no plaintext codes were committed. The access-code system no longer exists in the app, so the hashes gate nothing. Because the public repository starts from fresh history (Finding 1), this file's history disappears along with it. No further action needed.

## Finding 3 — the secrets file

`prospecting_secrets.json` (the user's Coach API key, and on developer machines optionally legacy webhook values used by the old build scripts) is:

- listed in `.gitignore` (root and `windows/` copies),
- absent from all history (`git log --all -- prospecting_secrets.json` → empty),
- excluded from all build outputs; the code writes the Coach key only to this file, never to the config (`_save_coach_key` in `prospecting_app.py`).

It must stay that way; [RELEASING.md](../../RELEASING.md) includes a pre-tag secret scan.

## Related pipeline note

The legacy build pipeline could inject webhook/analytics endpoints into the bundled config from CI secrets (`build-windows.yml`) or a local secrets file (`build_dmg.command`). Both scripts were rewritten during the public-release pass (commit `1026eac`): no secret is read, injected, or bundled anywhere in the current pipeline, and the build workflows scan the produced packages for endpoint/brand strings. A public build ships exactly the tracked, sanitized `windows/prospecting_config.json`.

## How this audit was performed

```sh
# History of the affected files:
git log --all --oneline -- prospecting_config.json windows/prospecting_config.json
git log --all --oneline -- docs/codes.json
git log --all --oneline -- prospecting_secrets.json     # -> empty

# Inspect the two key commits (metadata only; do not paste their contents anywhere):
git show --stat 6842a47
git show --stat 7ff6537

# Confirm current tracked files are clean:
git ls-files | grep config
```

Re-run this audit whenever the release process changes, and always before the first public push.

---

## Launch-day verification (2026-08-08, 5.0.0)

The revocation and history questions above were closed with live evidence
(values never printed or stored; probes report HTTP status only):

1. **Webhook revoked — verified.** A sweep of the *entire* history
   (`git log --all -p`) found exactly one unique Discord webhook URL
   (the finding-1/finding-2 credential). A live GET returns
   **HTTP 404 (Unknown Webhook)**: deleted on Discord's side, unusable.
2. **The "notify-bot secret" is inert.** `WEBHOOK_SECRET` was a payload
   field sent only to that webhook; with the endpoint dead there is
   nothing left to authenticate to.
3. **No other credential shapes in history.** Full-history sweep for
   GitHub PATs, AWS keys, Slack hooks, OpenAI/Anthropic-style keys and
   Discord bot tokens: zero hits. `prospectors-discord-bot/` was never
   tracked. `prospecting_secrets.json` was never committed.
4. **Fresh history is moot.** The history containing findings 1–2 has
   been public on `main` since June 2026 (the repository was already
   public with releases v1.0.0–v4.2.0). A history rewrite now would
   protect nothing and break existing clones; publication proceeds on
   the existing history with the credential verifiably dead.

Residual (accepted, documented): old public releases may embed the dead
webhook; some historical commits carry a personal author name/email.
Neither is reachable/actionable without destroying public history.
