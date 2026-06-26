# Access codes — how it works

Prospectors Plus is invite-only. On first launch users see a splash, then a gate
asking for an **access code**. Once a code checks out, that PC is remembered and
never asks again (even offline). You control who gets in.

## How verification works
- Valid codes are stored as **SHA-256 hashes** in `docs/codes.json`, served from
  your GitHub Pages site.
- When someone types a code, the app hashes it and checks the list. Plaintext
  codes are never published — the hashes can't be reversed.
- First successful unlock is saved locally (`ACCESS_OK` in their config), so the
  gate only needs the internet that one time.

## Make new codes
```
python3 tools/gen_codes.py 10
```
This prints 10 new plaintext codes (hand them out) and adds their hashes to
`docs/codes.json`. Then publish:
```
git add docs/codes.json && git commit -m "add codes" && git push
```
GitHub Pages updates within a minute — the new codes work immediately.

> The very first batch is in `ACCESS_CODES_PRIVATE.txt` (gitignored — never
> committed). Keep that file safe; it's the only record of the plaintext.

## Revoke a code
Delete that code's hash line from `docs/codes.json` and push. The next time that
person opens the app **with internet**, they'd be blocked — but note: anyone
already unlocked stays unlocked locally (the "remember after first success"
choice). To force everyone to re-verify on every launch, tell me and I'll switch
the app to always-online checking.

## Notes
- `docs/codes.json` and `ACCESS_CODES_PRIVATE.txt` must never contain the same
  plaintext — only `codes.json` (hashes) is safe to publish.
- Codes are case-insensitive and ignore spaces (`pplus-ab12-cd34` works).
