# License choice required — publication is blocked on this

**Status: this repository has NO license file.** Under copyright law, "no license" means all rights reserved: nobody may legally copy, modify, or redistribute the code, and calling the project "open source" is not accurate until a license exists. The owner must pick one before the repository is published.

This file deliberately does not choose. It gives a short, neutral comparison and the exact steps to finish.

## The three standard candidates

| | MIT | Apache-2.0 | GPL-3.0 |
|---|---|---|---|
| Style | Short permissive | Longer permissive | Strong copyleft |
| Others may use it in closed-source products | Yes | Yes | No — derivatives must also be GPL-3.0 |
| Explicit patent grant | No (implied at best) | Yes | Yes |
| Requires forks to state their changes | No | Yes (NOTICE/changed-files clauses) | Yes (plus full source disclosure) |
| Practical effect for this project | Maximum adoption, minimal friction; forks may go closed | Like MIT plus patent protection and clearer contribution terms | Guarantees every public fork stays open source |
| Common concern | Closed forks possible | Slightly more ceremony | Some users/companies avoid GPL code entirely |

Rules of thumb:

- Want the most users and contributors with the least friction → **MIT**.
- Same, but with a patent grant and more formal terms → **Apache-2.0**.
- Care most that improved forks must stay open → **GPL-3.0**.

All three are compatible with this project's dependencies (mostly BSD/MIT-licensed, plus pynput under LGPL-3.0, Pillow under MIT-CMU, and certifi under MPL-2.0 — see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)). pynput is used as an ordinary, unmodified dynamically-imported dependency, which the LGPL permits alongside any of these licenses, and MPL-2.0's file-level copyleft applies only to certifi's own files; none of the dependencies force a particular choice.

## Exact next steps (owner)

1. Pick one of the three above (or another OSI-approved license, deliberately).
2. Add the full license text as `LICENSE` at the repository root:
   - MIT: https://opensource.org/license/mit — fill in year + copyright holder.
   - Apache-2.0: https://www.apache.org/licenses/LICENSE-2.0.txt — verbatim; optionally add a `NOTICE` file.
   - GPL-3.0: https://www.gnu.org/licenses/gpl-3.0.txt — verbatim.
3. Update [README.md](README.md): replace the "does not have a license yet" section with the chosen license name and a link to `LICENSE`.
4. Update [CONTRIBUTING.md](CONTRIBUTING.md): remove the "contributions cannot be formally accepted" caveat.
5. Delete this file.
6. Only then proceed with the publication checklist in [RELEASING.md](RELEASING.md).

Until step 2 is done, do not publish the repository or distribute release builds.
