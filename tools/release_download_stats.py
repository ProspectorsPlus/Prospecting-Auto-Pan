#!/usr/bin/env python3
"""Owner-side report of PUBLIC GitHub release download counts.

Reads release-asset metadata from the public GitHub REST API (the same
numbers anyone can see on the releases page). No app telemetry is involved
and nothing is uploaded: this script only performs GET requests.

Usage:
    python3 tools/release_download_stats.py               # human tables
    python3 tools/release_download_stats.py --json        # raw aggregate
    python3 tools/release_download_stats.py --repo O/N    # another repo
    python3 tools/release_download_stats.py --timeout 30

Auth is optional (the repo is public). If GITHUB_TOKEN or GH_TOKEN is set
in the environment it is sent as a Bearer token, which raises the API rate
limit. The token is never printed or written anywhere.

Exit codes: 0 on success, 1 on any failure (network, rate limit, bad JSON).
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

DEFAULT_REPO = "ProspectorsPlus/Prospecting-Auto-Pan"
API_BASE = "https://api.github.com"

# Link header pagination: <https://api.github.com/...&page=2>; rel="next"
NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
def build_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "prospector-lite-release-stats",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = "Bearer %s" % token
    return headers


def fetch_all_releases(repo, timeout):
    """Every release for `repo`, following Link-header pagination."""
    url = "%s/repos/%s/releases?per_page=100" % (API_BASE, repo)
    headers = build_headers()
    releases = []
    while url:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                page = json.loads(resp.read().decode("utf-8"))
                link = resp.headers.get("Link", "")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                die("GitHub API refused the request (HTTP %d) -- likely "
                    "rate limiting. Setting GITHUB_TOKEN in the environment "
                    "raises the limit substantially." % e.code)
            die("GitHub API error: HTTP %d for %s" % (e.code, url))
        except urllib.error.URLError as e:
            die("Network error talking to the GitHub API: %s" % e.reason)
        except (TimeoutError, OSError) as e:
            die("Network error talking to the GitHub API: %s" % e)
        if not isinstance(page, list):
            die("Unexpected API response shape (expected a list of releases).")
        releases.extend(page)
        m = NEXT_LINK.search(link)
        url = m.group(1) if m else None
    return releases


def die(msg):
    print("ERROR: %s" % msg, file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------
# Classification: platform + kind from the asset filename alone.
# Order matters -- metadata names (checksums, sbom, ...) win over extension.
# --------------------------------------------------------------------------
def classify(name):
    """(platform, kind) for one asset filename."""
    low = name.lower()
    # Metadata artifacts first: they may carry .txt/.json/.zip extensions.
    if "sha256" in low or "checksum" in low:
        return "meta", "checksums"
    if "sbom" in low or "spdx" in low or "cyclonedx" in low:
        return "meta", "sbom"
    if "notes" in low or "changelog" in low:
        return "meta", "notes"
    if "manifest" in low:
        return "meta", "manifest"
    # Source bundles.
    if "source" in low or low.endswith((".tar.gz", ".tgz")) or "-src" in low:
        return "source", "source"
    # macOS.
    if low.endswith(".dmg"):
        return "macos", "dmg"
    if "mac" in low or "darwin" in low or "osx" in low:
        return "macos", "portable" if "portable" in low else "dmg"
    # Windows.
    if low.endswith(".exe") or "setup" in low or "installer" in low:
        return "windows", "installer"
    if "portable" in low or ("win" in low and low.endswith(".zip")):
        return "windows", "portable"
    if "win" in low:
        return "windows", "installer"
    return "meta", "manifest" if low.endswith(".json") else "notes"


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------
def aggregate(repo, releases):
    """A deterministic, JSON-friendly summary of every release."""
    # Newest first, by publish date (fall back to created date for drafts).
    releases = sorted(
        releases,
        key=lambda r: r.get("published_at") or r.get("created_at") or "",
        reverse=True)
    # "Latest" the way GitHub means it: newest non-draft, non-prerelease.
    latest_tag = next(
        (r.get("tag_name") for r in releases
         if not r.get("draft") and not r.get("prerelease")), None)

    out = {
        "repo": repo,
        "source": "public GitHub release-asset metadata (no app telemetry)",
        "latest_tag": latest_tag,
        "releases": [],
        "totals": {"windows": 0, "macos": 0, "source": 0, "meta": 0,
                   "installer": 0, "portable": 0, "all_time": 0},
    }
    t = out["totals"]
    for r in releases:
        rel = {
            "tag": r.get("tag_name"),
            "name": r.get("name") or r.get("tag_name"),
            "published_at": r.get("published_at"),
            "prerelease": bool(r.get("prerelease")),
            "draft": bool(r.get("draft")),
            "latest": r.get("tag_name") == latest_tag,
            "total_downloads": 0,
            "assets": [],
        }
        for a in r.get("assets", []):
            platform, kind = classify(a.get("name", ""))
            count = int(a.get("download_count", 0))
            rel["assets"].append({
                "name": a.get("name", ""),
                "size": int(a.get("size", 0)),
                "download_count": count,
                "platform": platform,
                "kind": kind,
            })
            rel["total_downloads"] += count
            t["all_time"] += count
            if platform in t:
                t[platform] += count
            if kind in ("installer", "portable"):
                t[kind] += count
        out["releases"].append(rel)
    return out


# --------------------------------------------------------------------------
# Human report
# --------------------------------------------------------------------------
def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%d %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


def print_table(rows, header):
    """Aligned fixed-width columns; numeric-ish columns right-aligned."""
    rows = [header] + rows
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(header))]
    right = {2, 3}   # size, downloads
    for idx, r in enumerate(rows):
        cells = [str(c).rjust(widths[i]) if i in right else
                 str(c).ljust(widths[i]) for i, c in enumerate(r)]
        print("  " + "  ".join(cells).rstrip())
        if idx == 0:
            print("  " + "  ".join("-" * w for w in widths))


def report(agg):
    print("Release download report for %s" % agg["repo"])
    print("(%s)" % agg["source"])
    print()
    for rel in agg["releases"]:
        marks = []
        if rel["latest"]:
            marks.append("LATEST")
        if rel["prerelease"]:
            marks.append("prerelease")
        if rel["draft"]:
            marks.append("draft")
        suffix = ("  [%s]" % ", ".join(marks)) if marks else ""
        print("%s -- %s (published %s)%s"
              % (rel["tag"], rel["name"], rel["published_at"] or "n/a", suffix))
        if not rel["assets"]:
            print("  (no assets)")
        else:
            rows = [[a["name"], "%s/%s" % (a["platform"], a["kind"]),
                     fmt_size(a["size"]), a["download_count"]]
                    for a in rel["assets"]]
            print_table(rows, ["asset", "class", "size", "downloads"])
        print("  release total: %d download(s)" % rel["total_downloads"])
        print()
    t = agg["totals"]
    print("Grand totals across %d release(s):" % len(agg["releases"]))
    print("  Windows:              %d" % t["windows"])
    print("  macOS:                %d" % t["macos"])
    print("  Source bundles:       %d" % t["source"])
    print("  Metadata files:       %d" % t["meta"])
    print("  Installer vs portable: %d vs %d" % (t["installer"], t["portable"]))
    print("  All-time total:       %d" % t["all_time"])
    if agg["latest_tag"]:
        print("Latest release: %s" % agg["latest_tag"])


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Report public GitHub release download counts.")
    ap.add_argument("--json", action="store_true",
                    help="dump the aggregated structure as JSON")
    ap.add_argument("--repo", default=DEFAULT_REPO, metavar="OWNER/NAME",
                    help="repository to query (default: %(default)s)")
    ap.add_argument("--timeout", type=float, default=20, metavar="SECONDS",
                    help="per-request timeout (default: %(default)s)")
    args = ap.parse_args()
    if "/" not in args.repo:
        die("--repo must look like OWNER/NAME")

    agg = aggregate(args.repo, fetch_all_releases(args.repo, args.timeout))
    if args.json:
        json.dump(agg, sys.stdout, indent=2)
        print()
    else:
        report(agg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
