#!/usr/bin/env python3
"""Compare what Ghost actually holds against the markdown that was sent.

A `ghst post update` returns a clean JSON object whether or not the content
arrived intact, so the CLI's own output is not evidence. This reads the post
back out of Ghost and diffs it against the source.

**It compares in both directions, and that is the whole design.** The check
this replaces asserted a list of expected strings and passed sixteen out of
sixteen while YAML front matter sat visible above the first paragraph of the
post. A verification that only asks "is what I wanted present?" cannot see
what should not be there. So this reports sentences in the source but missing
from Ghost (dropped content) *and* sentences in Ghost with no source
(leaked front matter, editor artifacts, an older draft's text).

Reading a Ghost post back has three traps, each of which produces a
convincing false negative, and all three are handled here:

  - `post get` returns no `html` field at all, only `lexical` and
    `mobiledoc`. Asking for `.posts[0].html` yields `null`.
  - `post update --markdown-file` converts markdown into native Lexical
    nodes, so a reader that looks only for markdown cards finds zero words.
    Both shapes are walked below.
  - `ghst api /posts/<id>/?formats=html` is rejected outright.

Usage:
    ghst post get <id> --json --jq '.posts[0].lexical' > lexical.json
    verify_post.py body.md lexical.json

Standard library only. Reads and compares; never calls Ghost, never writes.
"""

import argparse
import html as html_mod
import json
import re
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------------------
# Pulling text out of Lexical. The document is a tree whose leaves carry
# `text`; cards carry `markdown` or raw `html` instead, and which of the
# three you get depends on how the post was created -- the same post can be
# one markdown card before an update and seventy paragraph nodes after it.
# ---------------------------------------------------------------------------

def lexical_text(node, out: list[str] | None = None) -> str:
    """Every string a reader would see, in document order."""
    out = [] if out is None else out
    if isinstance(node, dict):
        for key in ("text", "markdown"):
            if isinstance(node.get(key), str):
                out.append(node[key])
        if isinstance(node.get("html"), str):
            out.append(strip_html(node["html"]))
        for value in node.values():
            lexical_text(value, out)
    elif isinstance(node, list):
        for value in node:
            lexical_text(value, out)
    return " ".join(out)


def strip_html(raw: str) -> str:
    return html_mod.unescape(re.sub(r"<[^>]+>", " ", raw))


def load_lexical(path: Path):
    """Ghost stores the Lexical document as a JSON *string* inside the post
    object, so `--jq '.posts[0].lexical'` hands back a quoted blob that has
    to be decoded twice. Accept either form rather than making the caller
    care which they captured."""
    raw = path.read_text(encoding="utf-8").strip()
    if not raw or raw == "null":
        raise SystemExit(
            "lexical file is empty or null.\n"
            "`post get --jq '.posts[0].html'` returns null -- Ghost exposes no html\n"
            "field. Capture '.posts[0].lexical' instead."
        )
    doc = json.loads(raw)
    return json.loads(doc) if isinstance(doc, str) else doc


# ---------------------------------------------------------------------------
# Normalising the two sides so they are comparable. Ghost has already turned
# markdown into structure by the time we read it back, so the source has to
# lose its syntax too -- otherwise every heading and bold span reports as a
# difference and the real findings drown.
# ---------------------------------------------------------------------------

MARKDOWN_STRIPPERS = (
    (re.compile(r"```.*?```", re.S), " "),          # fenced code
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),     # images
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),  # links -> their text
    (re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M), ""),   # heading markers
    (re.compile(r"^[ \t]{0,3}>[ \t]?", re.M), ""),        # block quotes
    (re.compile(r"^[ \t]{0,3}([-*+]|\d+\.)[ \t]+", re.M), ""),  # list markers
    # Tables: drop the separator row and the pipes, but KEEP the cell text --
    # Ghost renders those cells as prose, so discarding them here reports a
    # whole table as content Ghost invented.
    (re.compile(r"^[ \t]{0,3}\|[ \t:|-]+\|[ \t]*$", re.M), " "),
    (re.compile(r"\|"), " "),
    (re.compile(r"[*`~]+"), ""),                    # emphasis and code spans
)


def markdown_to_text(md: str) -> str:
    for pattern, repl in MARKDOWN_STRIPPERS:
        md = pattern.sub(repl, md)
    return md


def sentences(text: str) -> list[str]:
    """Crude split, on purpose: a real segmenter needs an abbreviation list
    this skill has no business carrying. Good enough to localise a finding,
    which is all the report needs it for."""
    flat = re.sub(r"\s+", " ", text)
    parts = re.split(r"(?<=[.!?:])\s+(?=[A-Z0-9\"'(])", flat)
    return [p.strip() for p in parts if len(p.strip()) > 25]


def key(sentence: str) -> str:
    """Compare on letters and digits alone -- no punctuation, no whitespace.

    Whitespace has to go, and this is not fussiness. Lexical stores a
    formatted span as its own text node, so `*demonstrative pronouns*,` comes
    back as two nodes that rejoin with a space before the comma. Keeping
    spaces significant reports every italic, bold and code span in the
    document as a difference -- on a real 4,500-word post that was 13 false
    "missing" lines and 12 false "extra" lines, all of them the same
    sentences. Ghost also normalises quotes and dashes on import, and those
    are not content either.
    """
    return re.sub(r"[^a-z0-9]", "", sentence.lower())


# ---------------------------------------------------------------------------
# Front matter is checked explicitly as well as by the diff, because it is
# the failure this script was written for and deserves to be unmissable
# rather than one line among many.
# ---------------------------------------------------------------------------

FRONT_MATTER_MARKERS = ("title:", "slug:", "excerpt:", "tags:", "status:",
                        "date:", "author:", "description:", "draft:")


def front_matter_leak(ghost_text: str) -> list[str]:
    head = ghost_text[:600].lower()
    return [m for m in FRONT_MATTER_MARKERS if m in head]


def compare(source_md: str, ghost_text: str) -> dict:
    src = Counter(key(s) for s in sentences(markdown_to_text(source_md)))
    dst = Counter(key(s) for s in sentences(ghost_text))
    lookup = {key(s): s for s in sentences(ghost_text)}
    lookup.update({key(s): s for s in sentences(markdown_to_text(source_md))})
    missing = sorted((src - dst).elements())
    extra = sorted((dst - src).elements())
    return {
        "missing": [lookup.get(k, k) for k in missing],
        "extra": [lookup.get(k, k) for k in extra],
        "source_words": len(markdown_to_text(source_md).split()),
        "ghost_words": len(ghost_text.split()),
        "leak": front_matter_leak(ghost_text),
    }


def report(result: dict, limit: int) -> str:
    lines: list[str] = []
    if result["leak"]:
        lines += ["!" * 70,
                  "FRONT MATTER IN THE POST BODY: " + ", ".join(result["leak"]),
                  "Ghost renders a leading --- block as prose. Strip it with",
                  "prepare_post.py and re-upload; do not fix it in the Ghost editor,",
                  "or the source file and the post will diverge.",
                  "!" * 70, ""]

    lines.append(f"source {result['source_words']} words  |  "
                 f"ghost {result['ghost_words']} words")

    for label, items, note in (
        ("IN SOURCE, NOT IN GHOST", result["missing"],
         "content that did not arrive, or arrived reworded"),
        ("IN GHOST, NOT IN SOURCE", result["extra"],
         "leaked front matter, editor edits, or an older draft still in place"),
    ):
        if not items:
            continue
        lines += ["", f"-- {label} ({len(items)}) --", f"   {note}"]
        for item in items[:limit]:
            lines.append(f"   * {item[:110]}{'...' if len(item) > 110 else ''}")
        if len(items) > limit:
            lines.append(f"   ... and {len(items) - limit} more (raise --limit)")

    if not result["missing"] and not result["extra"] and not result["leak"]:
        lines.append("\nNo differences. Every sentence in the source is in Ghost and "
                     "vice versa.\nMetadata is NOT checked here -- verify status, slug, "
                     "tags, excerpt and\nfeature image separately with `ghst post get`.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
    parser.add_argument("source", help="the markdown body that was uploaded (front matter removed)")
    parser.add_argument("lexical", help="file holding .posts[0].lexical from `ghst post get`")
    parser.add_argument("--limit", type=int, default=10, help="differences shown per section")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    source = Path(args.source).read_text(encoding="utf-8")
    ghost = lexical_text(load_lexical(Path(args.lexical)))
    result = compare(source, ghost)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(report(result, args.limit))

    # Advisory, like the rest of this repo's checkers: exit 0 means the
    # comparison ran. A difference is for a person to judge -- a reworded
    # sentence and a dropped paragraph look identical to a multiset.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
