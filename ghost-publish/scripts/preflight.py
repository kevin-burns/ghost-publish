#!/usr/bin/env python3
"""Check the installed `ghst` against the version this skill's traps were verified on.

WHY THIS EXISTS. SKILL.md and README.md both say "verified against 0.16.5 -- re-check the
traps above after an upgrade". That is a pin written in prose, which nothing enforces. The
same shape cost an afternoon elsewhere on 2026-08-17: a toolchain pinned in a comment and
resolved from PATH in the code turned out to be forty-two versions stale, and it reported
itself as an authorization failure rather than a version problem.

`ghst` is pre-1.0 and moves fast -- 27 tags and 30 commits in the thirty days to 2026-08-17.
Several of the behaviours this skill depends on are UNDOCUMENTED, so they are not covered by
anyone's semver promise:

  1. `--markdown-file` sends YAML front matter into the post as visible text
  2. `post update` converts markdown to Lexical, so the source is not recoverable afterwards
  3. `post get` returns no `html` field
  4. `auth login` fails behind an authenticating proxy

A patch release can change any of those without it being a breaking change to anyone but us.

WHAT IT DELIBERATELY DOES NOT DO.

  IT DOES NOT BLOCK ON DRIFT. A check that refuses to run the day `ghst` ships a patch is a
  check that gets commented out, and then the real drift arrives unannounced. Drift warns,
  loudly and specifically, and names what to re-verify. `--strict` is there for CI, where
  stopping is the right answer.

  IT DOES NOT CALL GHOST. `ghst --version` runs the local binary and touches no network, which
  keeps this in line with the other scripts here: they read and compare, they never reach the
  site. So this cannot tell you your credentials work -- only that the tool is the one the
  traps were written against.

Usage:
    python3 scripts/preflight.py
    python3 scripts/preflight.py --strict          # any drift is a failure
    python3 scripts/preflight.py --verified 0.17.0 # after re-verifying against a new release
"""

import argparse
import re
import shutil
import subprocess
import sys

# THE VERSION THE TRAPS WERE VERIFIED AGAINST. Bump it only after actually re-running the
# four behaviours above against the new release -- not after reading its changelog. The
# changelog will not mention them, because to Ghost they are not features.
VERIFIED = "0.16.5"

TRAPS = (
    "--markdown-file sending YAML front matter into the post body",
    "post update converting markdown to Lexical (source not recoverable)",
    "post get returning no html field",
    "auth login behind an authenticating proxy",
)

VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse(text: str) -> tuple[int, int, int] | None:
    """First x.y.z in whatever the CLI printed. Returns None rather than guessing."""
    m = VERSION_RE.search(text or "")
    return tuple(int(g) for g in m.groups()) if m else None


def read_version(run=None) -> tuple[str, str | None]:
    """(raw output, error). `run` is injectable so the tests never shell out."""
    if run is None:
        if not shutil.which("ghst"):
            return "", "ghst is not on PATH -- npm i -g @tryghost/ghst"
        run = lambda: subprocess.run(  # noqa: E731
            ["ghst", "--version"], capture_output=True, text=True, timeout=20, check=False
        )
    try:
        proc = run()
    except (OSError, subprocess.SubprocessError) as e:
        return "", f"could not run ghst --version: {type(e).__name__}"
    if proc.returncode != 0:
        return proc.stdout or "", f"ghst --version exited {proc.returncode}"
    return (proc.stdout or "").strip(), None


def classify(installed: tuple[int, int, int], verified: tuple[int, int, int]) -> str:
    """match | patch | minor | major | behind -- what KIND of drift, not how much."""
    if installed == verified:
        return "match"
    if installed < verified:
        # Older than the verified version is its own case: the traps were written against
        # behaviour this build may not have yet, which is not the same risk as a newer one.
        return "behind"
    if installed[:2] == verified[:2]:
        return "patch"
    return "minor" if installed[0] == verified[0] else "major"


def report(raw: str, err: str | None, verified: str = VERIFIED) -> tuple[int, list[str]]:
    """(exit code, lines). Exit 1 only when the check CANNOT be made."""
    if err:
        return 1, [f"FAIL {err}"]
    got, want = parse(raw), parse(verified)
    if not got:
        return 1, [f"FAIL could not parse a version out of {raw!r}"]
    if not want:
        return 1, [f"FAIL --verified {verified!r} is not an x.y.z version"]

    kind = classify(got, want)
    shown = ".".join(str(n) for n in got)
    if kind == "match":
        return 0, [f"ok   ghst {shown} is the version these traps were verified against"]

    head = {
        "patch": f"WARN ghst {shown} is a patch ahead of the verified {verified}",
        "minor": f"WARN ghst {shown} is a MINOR release ahead of the verified {verified}",
        "major": f"WARN ghst {shown} is a MAJOR release ahead of the verified {verified}",
        "behind": f"WARN ghst {shown} is OLDER than the verified {verified}",
    }[kind]
    lines = [head, "     re-verify these before trusting a publish, then bump VERIFIED:"]
    lines += [f"       - {t}" for t in TRAPS]
    if kind == "patch":
        lines.append(
            "     a patch bump is usually harmless here, but the four above are "
            "undocumented behaviours and no semver promise covers them."
        )
    return 0, lines


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
    p.add_argument("--verified", default=VERIFIED, help=f"version to compare against (default {VERIFIED})")
    p.add_argument("--strict", action="store_true", help="treat any drift as a failure (for CI)")
    args = p.parse_args()

    raw, err = read_version()
    code, lines = report(raw, err, args.verified)
    for line in lines:
        print(line)
    if args.strict and any(line.startswith("WARN") for line in lines):
        print("     --strict: drift is a failure")
        return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
