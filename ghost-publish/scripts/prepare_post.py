#!/usr/bin/env python3
"""Split a markdown post into the body Ghost should receive and the metadata
it should receive separately.

`ghst post create --markdown-file` transmits the file verbatim. Ghost has no
concept of front matter -- unlike Hugo or Jekyll, where the `---` block is
metadata, Ghost treats it as prose and renders it above the first paragraph
of the published post. The CLI reports success either way, so nothing in the
tooling catches it; a human reading the draft does.

So the front matter has to come off before upload, and what it contained has
to be passed as real Ghost fields. This prints those as `ghst` flags rather
than applying them, because sending the flags is the caller's job and this
script never talks to Ghost.

Standard library only. The YAML parsed here is deliberately a small subset --
see parse_front_matter for exactly which, and what it refuses to guess at.
"""

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", re.S)

# Keys that map onto a ghst flag. Anything else in the front matter is
# reported but not translated -- silently dropping a key the author wrote
# would be worse than saying it went unused.
FLAG_FOR_KEY = {
    "title": "--title",
    "slug": "--slug",
    "excerpt": "--excerpt",
    "tags": "--tags",
}


def split_front_matter(text: str) -> tuple[str, str]:
    """Return (front_matter_body, post_body). Front matter is only recognised
    at the very start of the file: a `---` fence elsewhere is a horizontal
    rule and must survive untouched."""
    match = FRONT_MATTER_RE.match(text)
    if not match:
        return "", text
    return match.group(1), text[match.end():]


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_front_matter(block: str) -> dict:
    """Parse the flat `key: value` subset that post front matter actually uses.

    Supported: scalars (quoted or bare) and inline lists (`tags: [a, b, c]`).
    Deliberately NOT supported, because guessing at them would produce a
    plausible-looking wrong answer rather than an error: nested mappings,
    block lists (`- item` on following lines), multi-line scalars (`|`, `>`),
    anchors, and any value containing an unescaped quote of its own kind.
    A line this cannot parse is returned under the "_unparsed" key so the
    caller can see it was skipped rather than silently mishandled.
    """
    out: dict = {}
    unparsed: list[str] = []
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] in (" ", "\t") or ":" not in line:
            unparsed.append(line)
            continue
        key, _, raw = line.partition(":")
        key, raw = key.strip(), raw.strip()
        if raw.startswith("[") and raw.endswith("]"):
            items = [_unquote(p) for p in raw[1:-1].split(",")]
            out[key] = [i for i in items if i]
        else:
            out[key] = _unquote(raw)
    if unparsed:
        out["_unparsed"] = unparsed
    return out


def ghst_flags(meta: dict) -> list[str]:
    """Front-matter keys rendered as ghst flags, in a stable order."""
    flags: list[str] = []
    for key, flag in FLAG_FOR_KEY.items():
        if key not in meta:
            continue
        value = meta[key]
        flags += [flag, ",".join(value) if isinstance(value, list) else value]
    return flags


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
    parser.add_argument("source", help="markdown file, front matter optional")
    parser.add_argument("--out", help="write the body here (default: stdout)")
    parser.add_argument("--json", action="store_true",
                        help="emit the parsed metadata as JSON instead of flags")
    args = parser.parse_args()

    text = Path(args.source).read_text(encoding="utf-8")
    front, body = split_front_matter(text)
    body = body.lstrip("\n")
    meta = parse_front_matter(front) if front else {}

    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
    else:
        sys.stdout.write(body)
        return 0

    if args.json:
        print(json.dumps(meta, indent=2))
        return 0

    print(f"body -> {args.out}  ({len(body.split())} words, front matter removed)"
          if front else
          f"body -> {args.out}  ({len(body.split())} words, no front matter found)")

    if flags := ghst_flags(meta):
        print("\nghst flags implied by the front matter:")
        print("  " + " ".join(shlex.quote(f) for f in flags))

    # `status` is a Ghost state transition, not a content field -- publishing
    # is a separate, deliberate command and must not be inferred from a file.
    if "status" in meta:
        print(f"\nnote: front matter says status={meta['status']!r}. Not translated:"
              "\n      use `ghst post publish` / `post schedule` deliberately.")

    if unused := sorted(set(meta) - set(FLAG_FOR_KEY) - {"status", "_unparsed"}):
        print(f"\nnote: no ghst flag for {', '.join(unused)} — set in Ghost if needed.")

    if "_unparsed" in meta:
        print("\nwarning: front-matter lines this parser does not handle "
              "(nested/block YAML is out of scope):")
        for line in meta["_unparsed"]:
            print(f"  {line.rstrip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
