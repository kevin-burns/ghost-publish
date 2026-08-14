#!/usr/bin/env python3
"""Turn the cards Ghost's markdown conversion cannot make into real ones.

Uploading markdown already gets you native cards for most things -- a single
`![alt](url)` becomes an image card, a blockquote becomes a quote card, a
fenced block keeps its language, a table becomes an HTML card. Two shapes it
never produces, because both are editor behaviours rather than markdown ones:

  GALLERY  Consecutive markdown images become separate image cards with a
           spacer paragraph between each. Ghost's gallery holds up to nine
           images (ghost.org/help/cards) laid out three to a row.
  EMBED    A bare video URL on its own line becomes a paragraph containing a
           link node. Clickable, not embedded. The editor embeds on paste;
           the markdown converter does not.

So this reads the Lexical document back out of Ghost, rewrites those two
shapes, and hands back a document to push with `ghst post update
--lexical-file`. Everything it does not recognise is passed through
untouched.

The node shapes here were taken from a post built by hand in Ghost's own
editor and read back, not inferred from Ghost's TypeScript definitions --
which give the field names but not the serialised form, and this stack fails
silently when a structure is wrong. Two details would have been guessed
incorrectly: `row` is three images per row, and a gallery image's `fileName`
is the ORIGINAL upload name while `src` is Ghost's stored URL, which may
carry a `-1` deduplication suffix the filename does not.

Standard library only. Reads files and writes a file; never calls Ghost.
"""

import argparse
import json
import re
import struct
import sys
from pathlib import Path

GALLERY_MAX = 9          # ghost.org/help/cards, verified 2026-08-14
GALLERY_PER_ROW = 3      # from a gallery built in the editor: 0,1,2 -> row 0

# Hosts whose bare URLs are worth turning into embeds. Deliberately a closed
# list: a link to someone's blog post should stay a link, and guessing which
# domains want an iframe is how you end up embedding a competitor's homepage.
EMBED_HOSTS = (
    "youtube.com", "youtu.be", "vimeo.com", "twitter.com", "x.com",
    "soundcloud.com", "spotify.com", "codepen.io", "github.com/gist",
)


# ---------------------------------------------------------------------------
# Image dimensions. A gallery needs real numbers -- Ghost lays the row out by
# aspect ratio -- and image cards created from markdown carry width: null,
# height: null. The files are local at upload time, so read the headers.
# ---------------------------------------------------------------------------

def image_size(path: Path) -> tuple[int, int] | None:
    """(width, height) for PNG, GIF, JPEG and WebP, or None if unreadable.

    Deliberately header-only and format-limited rather than pulling in
    Pillow: this skill is standard library, and these four cover every
    format Ghost accepts for a gallery. An unrecognised file returns None
    and the caller declines to build the gallery rather than inventing a
    dimension.
    """
    try:
        head = path.read_bytes()[:64]
    except OSError:
        return None

    if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
        return struct.unpack(">II", head[16:24])
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack("<HH", head[6:10])
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return _webp_size(path)
    if head[:2] == b"\xff\xd8":
        return _jpeg_size(path)
    return None


def _webp_size(path: Path) -> tuple[int, int] | None:
    data = path.read_bytes()
    chunk = data[12:16]
    if chunk == b"VP8X":
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return w, h
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 ":
        return struct.unpack("<HH", data[26:30])[0] & 0x3FFF, \
               struct.unpack("<HH", data[26:30])[1] & 0x3FFF
    return None


def _jpeg_size(path: Path) -> tuple[int, int] | None:
    data = path.read_bytes()
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
        # SOF0..SOF15, excluding the non-frame markers in that range.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + length
    return None


def resolve_local(src: str, images_dir: Path | None) -> Path | None:
    """Find the local file an uploaded image came from.

    Ghost deduplicates filenames by appending `-1`, `-2` and so on, so the
    stored URL often does not match the file on disk. An exact match is tried
    first; only then the suffix is stripped. A file that matches neither is
    reported rather than guessed at -- see build_gallery.
    """
    if images_dir is None:
        return None
    name = src.rsplit("/", 1)[-1]
    exact = images_dir / name
    if exact.is_file():
        return exact
    stem, dot, ext = name.rpartition(".")
    stripped = re.sub(r"-\d+$", "", stem)
    candidate = images_dir / f"{stripped}{dot}{ext}"
    return candidate if candidate.is_file() else None


# ---------------------------------------------------------------------------
# Recognising what to rewrite
# ---------------------------------------------------------------------------

def is_spacer(node: dict) -> bool:
    """A paragraph holding nothing but a linebreak. Ghost inserts one between
    consecutive markdown images, so a run of images is not contiguous in the
    child list and a naive scan finds runs of one."""
    if node.get("type") != "paragraph":
        return False
    kids = node.get("children") or []
    return all(k.get("type") == "linebreak" for k in kids) if kids else True


def bare_link_url(node: dict) -> str | None:
    """The URL of a paragraph that is nothing but one link whose text is that
    same URL -- i.e. a pasted address on its own line. A link with its own
    words is prose and must be left alone."""
    if node.get("type") != "paragraph":
        return None
    kids = [k for k in (node.get("children") or []) if k.get("type") != "linebreak"]
    if len(kids) != 1 or kids[0].get("type") != "link":
        return None
    link = kids[0]
    text = "".join(c.get("text", "") for c in (link.get("children") or [])).strip()
    url = (link.get("url") or "").strip()
    if not url or text.rstrip("/") != url.rstrip("/"):
        return None
    return url if any(host in url for host in EMBED_HOSTS) else None


# ---------------------------------------------------------------------------
# Building the replacement nodes
# ---------------------------------------------------------------------------

def build_gallery(images: list[dict], images_dir: Path | None,
                  notes: list[str]) -> dict | None:
    """A gallery node, or None if it cannot be built honestly.

    Ghost lays a gallery out by aspect ratio, so an image without real
    dimensions would render wrongly. Rather than emit a null width the way
    the markdown importer does, this declines the whole merge and says which
    file it could not measure -- leaving separate image cards, which look
    ordinary rather than broken.
    """
    entries = []
    for index, node in enumerate(images):
        src = node.get("src", "")
        width, height = node.get("width"), node.get("height")
        local = resolve_local(src, images_dir)
        if (not width or not height) and local is not None:
            if size := image_size(local):
                width, height = size
        if not width or not height:
            notes.append(
                f"gallery skipped: no dimensions for {src.rsplit('/', 1)[-1]}"
                f"{'' if images_dir else ' (pass --images-dir)'}")
            return None
        entries.append({
            "row": index // GALLERY_PER_ROW,
            "src": src,
            "width": width,
            "height": height,
            # The original upload name, which is NOT always the basename of
            # src -- Ghost's dedup suffix lives in the URL only.
            "fileName": local.name if local else src.rsplit("/", 1)[-1],
        })
    return {"type": "gallery", "version": 1, "images": entries, "caption": ""}


def build_embed(url: str, oembed: dict) -> dict:
    """An embed node from a Ghost `/oembed/` payload.

    `metadata` is the whole payload verbatim, and `html`/`embedType` are just
    two of its fields lifted to the top level -- that is how Ghost's own
    editor serialises it, checked against one built by hand.
    """
    return {
        "type": "embed",
        "version": 1,
        "url": url,
        "embedType": oembed.get("type", ""),
        "html": oembed.get("html", ""),
        "metadata": oembed,
        "caption": "",
    }


# ---------------------------------------------------------------------------
# The pass itself
# ---------------------------------------------------------------------------

def enrich(doc: dict, oembeds: dict[str, dict], images_dir: Path | None) -> tuple[dict, list[str]]:
    children = doc.get("root", {}).get("children", [])
    out: list[dict] = []
    notes: list[str] = []
    i = 0

    while i < len(children):
        node = children[i]

        if node.get("type") == "image":
            run, j = [node], i + 1
            while j < len(children):
                if children[j].get("type") == "image":
                    run.append(children[j])
                    j += 1
                elif is_spacer(children[j]) and j + 1 < len(children) \
                        and children[j + 1].get("type") == "image":
                    j += 1          # step over the spacer, keep collecting
                else:
                    break
            if len(run) > 1:
                if len(run) > GALLERY_MAX:
                    notes.append(
                        f"{len(run)} consecutive images: first {GALLERY_MAX} become a "
                        f"gallery, the remaining {len(run) - GALLERY_MAX} stay image "
                        "cards (Ghost's limit is nine)")
                if gallery := build_gallery(run[:GALLERY_MAX], images_dir, notes):
                    out.append(gallery)
                    out.extend(run[GALLERY_MAX:])
                    notes.append(f"gallery built from {min(len(run), GALLERY_MAX)} images")
                    i = j
                    continue
                out.extend(run)     # declined -- leave the image cards alone
                i = j
                continue

        if url := bare_link_url(node):
            if payload := oembeds.get(url):
                out.append(build_embed(url, payload))
                notes.append(f"embed built for {url}")
                i += 1
                continue
            notes.append(f"embed skipped: no oEmbed payload for {url}")

        out.append(node)
        i += 1

    doc = json.loads(json.dumps(doc))       # do not mutate the caller's object
    doc["root"]["children"] = out
    return doc, notes


def embed_urls(doc: dict) -> list[str]:
    """Every bare URL that would become an embed, so a caller can fetch the
    oEmbed payloads before running the transform."""
    seen: list[str] = []
    for node in doc.get("root", {}).get("children", []):
        if (url := bare_link_url(node)) and url not in seen:
            seen.append(url)
    return seen


def load_lexical(path: Path):
    """Ghost stores the document as a JSON string inside the post object, so
    `--jq '.posts[0].lexical'` yields a quoted blob needing two decodes."""
    raw = path.read_text(encoding="utf-8").strip()
    if not raw or raw == "null":
        raise SystemExit("lexical file is empty or null -- capture "
                         "'.posts[0].lexical', not '.posts[0].html'")
    doc = json.loads(raw)
    return json.loads(doc) if isinstance(doc, str) else doc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
    parser.add_argument("lexical", help="file holding .posts[0].lexical from `ghst post get`")
    parser.add_argument("--out", help="write the rewritten document here")
    parser.add_argument("--images-dir", help="directory of the local image files, for dimensions")
    parser.add_argument("--oembed", help="JSON file mapping URL -> Ghost /oembed/ payload")
    parser.add_argument("--list-embeds", action="store_true",
                        help="print the URLs needing an oEmbed payload, and stop")
    args = parser.parse_args()

    doc = load_lexical(Path(args.lexical))

    if args.list_embeds:
        for url in embed_urls(doc):
            print(url)
        return 0

    oembeds = json.loads(Path(args.oembed).read_text()) if args.oembed else {}
    images_dir = Path(args.images_dir) if args.images_dir else None
    result, notes = enrich(doc, oembeds, images_dir)

    for note in notes:
        print(note, file=sys.stderr)
    if not notes:
        print("nothing to enrich: no image runs and no bare embeddable URLs",
              file=sys.stderr)

    payload = json.dumps(result, separators=(",", ":"))
    if args.out:
        # Ghost takes the document as a JSON string, which is the same shape
        # `post get` handed back -- so what is written here can be passed
        # straight to `ghst post update --lexical-file`.
        Path(args.out).write_text(payload, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
