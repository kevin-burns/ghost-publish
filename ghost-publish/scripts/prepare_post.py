#!/usr/bin/env python3
"""Split a markdown post into the body Ghost should receive and the metadata
it should receive separately.

It also removes the drafting scaffolding directly beneath the front matter --
a leading H1 that would duplicate Ghost's own title field, and an author note
saying who the post is for. Neither is front matter, which is exactly why an
earlier version of this script left both on a public post. Every removal is
printed to stderr; `--keep-scaffolding` turns the step off.

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

# `slug` is the exception, and the asymmetry is ghst's rather than a choice
# here. Measured on 0.16.6: `page create` takes `--slug <slug>` and sets it,
# while `post create` has no such option at all and rejects it outright with
# `error: unknown option '--slug'`. `post update --slug` exists but is a
# LOOKUP -- it selects a post, it does not rename one. So a post's slug is
# reachable only through `--from-json`, which is what --payload writes.
SLUG_FLAG_TARGETS = frozenset({"page"})


def split_front_matter(text: str) -> tuple[str, str]:
    """Return (front_matter_body, post_body). Front matter is only recognised
    at the very start of the file: a `---` fence elsewhere is a horizontal
    rule and must survive untouched."""
    match = FRONT_MATTER_RE.match(text)
    if not match:
        return "", text
    return match.group(1), text[match.end():]


# --- drafting scaffolding -------------------------------------------------
#
# What sits between the front matter and the first real paragraph in a draft,
# written for the author rather than the reader. It is NOT front matter, which
# is why removing the front matter and stopping was not enough: on 2026-09-02 a
# leading H1 and an author note went up on a public post. Ghost renders the
# title from its own field, so the H1 duplicated it, and the note said who the
# post was for -- an internal aside, published.
#
# Both rules are deliberately narrow:
#   * only at the very top, before any prose. Scaffolding further down is prose.
#   * every removal is PRINTED, to stderr so it survives the stdout path too.
#     A silent strip is how you lose an H1 somebody meant to keep.
#
# An emphasis-only line that does NOT match the author-note shape is reported
# and LEFT ALONE. Stripping on suspicion would eat a legitimate epigraph, and a
# warning costs the author one glance.

H1_RE = re.compile(r"\A#[ \t]+\S")
HR_RE = re.compile(r"\A(?:-{3,}|\*{3,}|_{3,})\Z")
# The whole line is one emphasis span -- no delimiter inside, so "*a* and *b*"
# does not qualify. A heading wrapper is allowed because drafts have used one.
EMPHASIS_ONLY_RE = re.compile(r"\A(?:#{1,6}[ \t]+)?(?:\*([^*]+)\*|_([^_]+)_)\Z")
# Two unambiguous author-note markers, measured against every post in the
# corpus that carried one. A bare "For <something>" is NOT enough: an article
# may legitimately open on an italic line beginning "For years...".
AUTHOR_NOTE_RE = re.compile(r"audience:|\Adraft\b", re.I)


def strip_scaffolding(body: str) -> tuple[str, list[str], list[str]]:
    """Remove leading drafting scaffolding. Returns (body, removed, warnings).

    Order, each part optional and only at the very top: one H1, one author-note
    line, and a horizontal rule following either. The rule is removed only if
    something above it was, so a post that legitimately opens on one keeps it.
    """
    lines = body.split("\n")
    removed: list[str] = []
    warnings: list[str] = []

    def peek() -> tuple[int, str] | None:
        for i, line in enumerate(lines):
            if line.strip():
                return i, line.strip()
        return None

    def drop(index: int, label: str) -> None:
        text = lines[index].strip()
        removed.append(f"{label}: {text[:90]}{'...' if len(text) > 90 else ''}")
        del lines[:index + 1]

    if (found := peek()) and H1_RE.match(found[1]):
        drop(found[0], "leading H1 (Ghost renders the title from its own field)")

    if (found := peek()) and (match := EMPHASIS_ONLY_RE.match(found[1])):
        inner = match.group(1) or match.group(2) or ""
        if AUTHOR_NOTE_RE.search(inner):
            drop(found[0], "author note")
        else:
            warnings.append(
                "first line is entirely italic and may be an author note, but does not "
                f"match the known shape, so it was KEPT: {found[1][:90]}")

    if removed and (found := peek()) and HR_RE.match(found[1]):
        drop(found[0], "horizontal rule under the scaffolding")

    return "\n".join(lines).lstrip("\n"), removed, warnings


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


def ghst_flags(meta: dict, target: str = "post") -> list[str]:
    """Front-matter keys rendered as ghst flags, in a stable order.

    `target` decides whether slug is among them -- see SLUG_FLAG_TARGETS.
    Emitting `--slug` for a post would hand the caller a flag the CLI
    refuses, which is worse than omitting it and saying why.
    """
    flags: list[str] = []
    for key, flag in FLAG_FOR_KEY.items():
        if key not in meta:
            continue
        if key == "slug" and target not in SLUG_FLAG_TARGETS:
            continue
        value = meta[key]
        flags += [flag, ",".join(value) if isinstance(value, list) else value]
    return flags


def payload_for(meta: dict) -> dict:
    """The `--from-json` payload for a post, which is the only route that
    sets a slug on one.

    Field names here are Ghost's own, not the CLI's flag names: the excerpt
    is `custom_excerpt`, and tags are objects rather than a comma-separated
    string. `status` is deliberately absent for the same reason it is not a
    flag -- publishing is a state transition, not a content field.
    """
    payload: dict = {}
    for key in ("title", "slug"):
        if key in meta:
            payload[key] = meta[key]
    if "excerpt" in meta:
        payload["custom_excerpt"] = meta["excerpt"]
    if "tags" in meta:
        tags = meta["tags"]
        payload["tags"] = [{"name": t} for t in (tags if isinstance(tags, list) else [tags])]
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
    parser.add_argument("source", help="markdown file, front matter optional")
    parser.add_argument("--out", help="write the body here (default: stdout)")
    parser.add_argument("--json", action="store_true",
                        help="emit the parsed metadata as JSON instead of flags")
    parser.add_argument("--target", choices=("post", "page"), default="post",
                        help="post (default) or page; decides whether slug is a flag")
    parser.add_argument("--payload",
                        help="write a --from-json payload here; the only route "
                             "that sets a slug on a post")
    parser.add_argument("--keep-scaffolding", action="store_true",
                        help="keep a leading H1 and author note instead of removing "
                             "them; use when the H1 is genuinely part of the post")
    args = parser.parse_args()

    text = Path(args.source).read_text(encoding="utf-8")
    front, body = split_front_matter(text)
    body = body.lstrip("\n")
    meta = parse_front_matter(front) if front else {}

    # Reported on stderr rather than stdout: without --out the body IS stdout,
    # and a note printed into it would be published along with the post.
    removed: list[str] = []
    if args.keep_scaffolding:
        warnings: list[str] = []
    else:
        body, removed, warnings = strip_scaffolding(body)
    for line in removed:
        print(f"removed {line}", file=sys.stderr)
    for line in warnings:
        print(f"warning: {line}", file=sys.stderr)

    if args.payload:
        Path(args.payload).write_text(
            json.dumps(payload_for(meta), indent=2) + "\n", encoding="utf-8")

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

    if flags := ghst_flags(meta, args.target):
        print(f"\nghst flags implied by the front matter ({args.target}):")
        print("  " + " ".join(shlex.quote(f) for f in flags))

    # A slug on a post is not in those flags and must not be silently dropped.
    if args.target == "post" and "slug" in meta:
        if args.payload:
            print(f"\nslug -> {args.payload}. `post create` has no --slug flag, so pass:"
                  f"\n  ghst post create --from-json {args.payload} --markdown-file {args.out}")
        else:
            print("\nnote: `ghst post create` has no --slug flag, so the front matter's slug"
                  "\n      is NOT in the flags above. Re-run with --payload PATH and pass it"
                  "\n      as `ghst post create --from-json PATH`, or the slug is lost.")

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
