#!/usr/bin/env python3
"""Generate Prospectors Plus access codes and add their hashes to docs/codes.json.

Usage:
    python3 tools/gen_codes.py 10          # make 10 new codes
    python3 tools/gen_codes.py 10 --label tournament

- Prints the new plaintext codes (hand these out; they are NOT stored anywhere).
- Appends their SHA-256 hashes to docs/codes.json (commit + push to publish).
- Only the hashes are public; the plaintext can't be recovered from them.

To REVOKE a code: delete its hash line from docs/codes.json and push. If you
didn't note which hash is which, you can recompute a code's hash with:
    python3 -c "import hashlib;print(hashlib.sha256('PPLUS-XXXX-XXXX'.encode()).hexdigest())"
"""
import sys, os, json, secrets, hashlib

ALPH = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no easily-confused chars
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODES = os.path.join(ROOT, "docs", "codes.json")


def make():
    g = lambda n: "".join(secrets.choice(ALPH) for _ in range(n))
    return f"PPLUS-{g(4)}-{g(4)}"


def h(code):
    return hashlib.sha256("".join(code.split()).upper().encode()).hexdigest()


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    data = {"hashes": []}
    if os.path.exists(CODES):
        data = json.load(open(CODES))
    have = set(data.get("hashes", []))
    new = []
    while len(new) < n:
        c = make()
        if h(c) in have:
            continue
        new.append(c)
        have.add(h(c))
    data["hashes"] = sorted(have)
    json.dump(data, open(CODES, "w"), indent=2)
    print(f"Added {len(new)} codes. docs/codes.json now has {len(have)} total.\n")
    print("NEW CODES (hand these out, keep private):")
    for c in new:
        print("  ", c)
    print("\nCommit + push docs/codes.json to publish.")


if __name__ == "__main__":
    main()
