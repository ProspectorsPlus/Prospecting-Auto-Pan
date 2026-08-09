#!/usr/bin/env python3
"""Lint the public website content under docs/.

Scans site files only (*.html, *.css, *.js, *.json, *.svg), skipping the
historical engineering-report folders and all Markdown. Four checks:

  1. HARD FAIL  no em dash character (U+2014) anywhere
  2. HARD FAIL  no banned marketing phrases in HTML visible text/attributes
  3. HARD FAIL  every relative href/src in HTML resolves to a real file
                inside docs/ (directories count if they hold index.html)
  4. WARNING    absolute links to the published site whose path does not
                exist locally under docs/ (informational only)

Usage:
    python3 tools/site_lint.py            # full report
    python3 tools/site_lint.py --quiet    # failures only

Exit codes: 0 when clean (warnings allowed), 1 on any hard failure.
"""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")

SITE_EXTS = {".html", ".css", ".js", ".json", ".svg"}
# Historical engineering reports live under these folders; they are not
# rendered site pages, so the lint leaves them alone.
SKIP_DIRS = {"public-release", "public-launch", "final-prepublish",
             "final-visible-pass", "stabilization", "trust-and-onboarding"}

EM_DASH = "—"

# Curated ban list, matched case-insensitively. Straight and curly
# apostrophes both count for "whether you're".
BANNED_PHRASES = ["seamless", "cutting-edge", "revolutionary",
                  "game-changing", "whether you're", "empower your"]
BANNED_RES = [re.compile(re.escape(p).replace("'", "['’]"), re.I)
              for p in BANNED_PHRASES]

# href="..." / src='...' in HTML, either quote style.
LINK_RE = re.compile(r"""(?:href|src)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
# Prefixes that make a link non-relative (check 3 skips these).
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "#", "data:",
                     "//", "javascript:", "tel:")
SITE_URL = "https://prospectorsplus.github.io/Prospecting-Auto-Pan/"


def site_files():
    """All lintable site files under docs/, deterministic order."""
    found = []
    for dirpath, dirnames, filenames in os.walk(DOCS):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in SITE_EXTS:
                found.append(os.path.join(dirpath, name))
    return found


def rel(path):
    return os.path.relpath(path, ROOT)


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def strip_hidden_html(text):
    """Drop comments, <script> and <style> bodies so the phrase check sees
    only visible text and tag attributes. Blanks are substituted (not
    removed) so line numbers stay accurate."""
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    text = re.sub(r"<!--.*?-->", blank, text, flags=re.S)
    text = re.sub(r"<script\b.*?</script>", blank, text, flags=re.S | re.I)
    text = re.sub(r"<style\b.*?</style>", blank, text, flags=re.S | re.I)
    return text


def excerpt(line, limit=90):
    line = line.strip()
    return line if len(line) <= limit else line[:limit] + "..."


def target_exists(path):
    """True when `path` is a real file, or a directory with an index.html."""
    if os.path.isfile(path):
        return True
    return os.path.isdir(path) and \
        os.path.isfile(os.path.join(path, "index.html"))


def inside_docs(path):
    return os.path.commonpath([DOCS, os.path.abspath(path)]) == DOCS


def check_links(path, text, failures, warnings):
    """Checks 3 and 4 for one HTML file."""
    base = os.path.dirname(path)
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in LINK_RE.finditer(line):
            raw = (m.group(1) or m.group(2) or "").strip()
            if not raw:
                continue
            low = raw.lower()
            # Check 4: absolute links into the published site.
            if low.startswith(SITE_URL.lower()):
                tail = raw[len(SITE_URL):].split("#")[0].split("?")[0]
                local = os.path.normpath(os.path.join(DOCS, tail)) \
                    if tail else DOCS
                if not target_exists(local):
                    warnings.append(
                        "%s:%d absolute site link has no local target: %s"
                        % (rel(path), lineno, raw))
                continue
            if low.startswith(EXTERNAL_PREFIXES):
                continue
            # Check 3: relative link -> must exist inside docs/.
            tail = raw.split("#")[0].split("?")[0]
            if not tail:
                continue    # pure-fragment or pure-query link
            if tail.startswith("/"):
                # Root-relative: resolve against the docs root.
                resolved = os.path.normpath(
                    os.path.join(DOCS, tail.lstrip("/")))
            else:
                resolved = os.path.normpath(os.path.join(base, tail))
            if not inside_docs(resolved):
                failures.append(
                    "%s:%d link escapes docs/: %s" % (rel(path), lineno, raw))
            elif not target_exists(resolved):
                failures.append(
                    "%s:%d broken link: %s" % (rel(path), lineno, raw))


def main():
    ap = argparse.ArgumentParser(description="Lint the public site in docs/.")
    ap.add_argument("--quiet", action="store_true",
                    help="print only failures")
    args = ap.parse_args()

    if not os.path.isdir(DOCS):
        print("ERROR: docs/ not found at %s" % DOCS, file=sys.stderr)
        return 1

    files = site_files()
    failures = []   # hard failures (checks 1-3)
    warnings = []   # check 4 only

    for path in files:
        text = read_text(path)
        is_html = path.lower().endswith(".html")
        visible = strip_hidden_html(text) if is_html else text

        for lineno, line in enumerate(text.splitlines(), 1):
            # Check 1: em dash, in any site file.
            if EM_DASH in line:
                failures.append("%s:%d em dash (U+2014): %s"
                                % (rel(path), lineno, excerpt(line)))
        if is_html:
            # Check 2: banned phrases in visible text / attributes.
            for lineno, line in enumerate(visible.splitlines(), 1):
                for phrase, pat in zip(BANNED_PHRASES, BANNED_RES):
                    if pat.search(line):
                        failures.append(
                            '%s:%d banned phrase "%s": %s'
                            % (rel(path), lineno, phrase, excerpt(line)))
            # Checks 3 and 4.
            check_links(path, text, failures, warnings)

    if not args.quiet:
        print("site_lint: scanned %d site file(s) under docs/" % len(files))
    if failures:
        print("FAILURES (%d):" % len(failures))
        for f in failures:
            print("  " + f)
    if warnings and not args.quiet:
        print("warnings (%d, non-fatal):" % len(warnings))
        for w in warnings:
            print("  " + w)
    if not args.quiet:
        print("result: %s" % ("FAIL" if failures else "clean"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
