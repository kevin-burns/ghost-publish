import json
import struct
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from enrich_cards import (  # noqa: E402
    GALLERY_MAX,
    bare_link_url,
    build_embed,
    embed_urls,
    enrich,
    image_size,
    is_spacer,
    resolve_local,
)

HERO = "https://blog.example.com/content/images/2026/08/a-hero.webp"


def image_node(src=HERO, **kw):
    node = {"type": "image", "version": 1, "src": src, "width": None, "height": None,
            "alt": "", "caption": "", "cardWidth": "regular", "href": "", "title": ""}
    node.update(kw)
    return node


def spacer():
    return {"type": "paragraph", "version": 1,
            "children": [{"type": "linebreak", "version": 1}]}


def link_para(url, text=None):
    return {"type": "paragraph", "version": 1, "children": [{
        "type": "link", "version": 1, "url": url,
        "children": [{"type": "extended-text", "version": 1, "text": text or url}]}]}


def doc(children):
    return {"root": {"type": "root", "version": 1, "children": children}}


# ---------------------------------------------------------------------------
# Reading dimensions. A gallery needs real numbers because Ghost lays the row
# out by aspect ratio, and markdown-created image cards carry width: null.
# ---------------------------------------------------------------------------

def test_png_dimensions(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
                  + struct.pack(">II", 2752, 1536) + b"\x08\x06\x00\x00\x00")
    assert image_size(p) == (2752, 1536)


def test_gif_dimensions(tmp_path):
    p = tmp_path / "a.gif"
    p.write_bytes(b"GIF89a" + struct.pack("<HH", 640, 480) + b"\x00" * 20)
    assert image_size(p) == (640, 480)


def test_webp_vp8x_dimensions(tmp_path):
    p = tmp_path / "a.webp"
    body = b"VP8X" + b"\x00" * 4 + b"\x00" * 4 \
        + (2751).to_bytes(3, "little") + (1535).to_bytes(3, "little")
    p.write_bytes(b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body + b"\x00" * 20)
    assert image_size(p) == (2752, 1536)  # VP8X stores width-1


def test_unreadable_format_returns_none_rather_than_guessing(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"not an image at all, just some bytes " * 4)
    assert image_size(p) is None


def test_local_file_is_found_through_ghosts_dedup_suffix(tmp_path):
    """Ghost appends -1, -2 ... to a stored URL when the name collides, but
    the gallery's fileName is the ORIGINAL name. Deriving one from the other
    naively would be wrong, so an exact match is tried first."""
    (tmp_path / "a-hero.webp").write_bytes(b"x")
    assert resolve_local("https://x/content/a-hero-1.webp", tmp_path).name == "a-hero.webp"


def test_exact_match_wins_over_suffix_stripping(tmp_path):
    """A file legitimately named `chart-1.webp` must not be mistaken for a
    deduplicated `chart.webp`."""
    (tmp_path / "chart-1.webp").write_bytes(b"x")
    (tmp_path / "chart.webp").write_bytes(b"y")
    assert resolve_local("https://x/content/chart-1.webp", tmp_path).name == "chart-1.webp"


# ---------------------------------------------------------------------------
# Gallery
# ---------------------------------------------------------------------------

def test_spacer_paragraph_between_images_is_recognised():
    """Ghost inserts a linebreak-only paragraph between consecutive markdown
    images, so a run is not contiguous and a naive scan finds runs of one."""
    assert is_spacer(spacer())
    assert not is_spacer(link_para("https://youtube.com/watch?v=x"))


def test_run_of_images_becomes_one_gallery(tmp_path):
    for name in ("a.webp", "b.webp", "c.webp"):
        (tmp_path / name).write_bytes(b"GIF89a" + struct.pack("<HH", 800, 600) + b"\x00" * 20)
    nodes = [image_node(src=f"https://x/content/{n}") for n in ("a.webp", "b.webp", "c.webp")]
    result, notes = enrich(doc([nodes[0], spacer(), nodes[1], spacer(), nodes[2]]), {}, tmp_path)
    kids = result["root"]["children"]
    assert len(kids) == 1 and kids[0]["type"] == "gallery"
    assert len(kids[0]["images"]) == 3
    assert any("gallery built" in n for n in notes)


def test_gallery_rows_are_three_per_row(tmp_path):
    """Confirmed against a gallery built by hand in Ghost's editor: images
    0,1,2 are row 0 and image 3 is row 1."""
    for i in range(4):
        (tmp_path / f"{i}.gif").write_bytes(b"GIF89a" + struct.pack("<HH", 800, 600) + b"\x00" * 20)
    nodes = [image_node(src=f"https://x/content/{i}.gif") for i in range(4)]
    result, _ = enrich(doc(nodes), {}, tmp_path)
    assert [img["row"] for img in result["root"]["children"][0]["images"]] == [0, 0, 0, 1]


def test_filename_is_the_local_name_not_the_deduplicated_url(tmp_path):
    (tmp_path / "hero.gif").write_bytes(b"GIF89a" + struct.pack("<HH", 10, 10) + b"\x00" * 20)
    (tmp_path / "other.gif").write_bytes(b"GIF89a" + struct.pack("<HH", 10, 10) + b"\x00" * 20)
    nodes = [image_node(src="https://x/content/hero-1.gif"),
             image_node(src="https://x/content/other.gif")]
    result, _ = enrich(doc(nodes), {}, tmp_path)
    assert [i["fileName"] for i in result["root"]["children"][0]["images"]] \
        == ["hero.gif", "other.gif"]


def test_gallery_is_declined_when_dimensions_are_unknown():
    """Ghost lays a gallery out by aspect ratio, so a null width renders
    wrongly. Separate image cards look ordinary; a broken gallery does not."""
    result, notes = enrich(doc([image_node(), spacer(), image_node()]), {}, None)
    assert [c["type"] for c in result["root"]["children"]] == ["image", "image"]
    assert any("no dimensions" in n for n in notes)


def test_gallery_caps_at_nine_and_says_so(tmp_path):
    for i in range(11):
        (tmp_path / f"{i}.gif").write_bytes(b"GIF89a" + struct.pack("<HH", 10, 10) + b"\x00" * 20)
    nodes = [image_node(src=f"https://x/content/{i}.gif") for i in range(11)]
    result, notes = enrich(doc(nodes), {}, tmp_path)
    kids = result["root"]["children"]
    assert kids[0]["type"] == "gallery" and len(kids[0]["images"]) == GALLERY_MAX
    assert [c["type"] for c in kids[1:]] == ["image", "image"]
    assert any("remaining 2 stay image cards" in n for n in notes)


def test_a_single_image_is_left_as_an_image_card(tmp_path):
    result, _ = enrich(doc([image_node()]), {}, tmp_path)
    assert [c["type"] for c in result["root"]["children"]] == ["image"]


# ---------------------------------------------------------------------------
# Embeds
# ---------------------------------------------------------------------------

def test_bare_video_url_is_detected():
    assert bare_link_url(link_para("https://youtube.com/watch?v=abc")) \
        == "https://youtube.com/watch?v=abc"


def test_a_link_with_its_own_words_is_prose_and_left_alone():
    assert bare_link_url(link_para("https://youtube.com/watch?v=abc", "this talk")) is None


def test_a_bare_url_on_a_non_embed_host_is_left_alone():
    assert bare_link_url(link_para("https://example.com/some/article")) is None


def test_embed_is_built_from_the_oembed_payload():
    """metadata is the whole payload verbatim, with html and embedType lifted
    to the top level -- how Ghost's own editor serialises it."""
    payload = {"type": "video", "html": "<iframe src='x'></iframe>",
               "title": "A talk", "provider_name": "YouTube"}
    node = build_embed("https://youtube.com/watch?v=abc", payload)
    assert node["type"] == "embed"
    assert node["embedType"] == "video"
    assert node["html"] == payload["html"]
    assert node["metadata"] == payload


def test_missing_oembed_payload_leaves_the_link_and_says_why():
    url = "https://youtube.com/watch?v=abc"
    result, notes = enrich(doc([link_para(url)]), {}, None)
    assert result["root"]["children"][0]["type"] == "paragraph"
    assert any("no oEmbed payload" in n for n in notes)


def test_list_embeds_reports_urls_needing_a_payload():
    d = doc([link_para("https://youtube.com/watch?v=a"),
             link_para("https://example.com/article"),
             link_para("https://vimeo.com/123")])
    assert embed_urls(d) == ["https://youtube.com/watch?v=a", "https://vimeo.com/123"]


# ---------------------------------------------------------------------------
# Passthrough. A provider's own iframe -- Bandcamp, and anything else pasted
# as raw HTML -- must survive untouched, including its width and styling.
# ---------------------------------------------------------------------------

def test_an_existing_embed_is_passed_through_byte_for_byte():
    """A pasted Bandcamp iframe becomes an embed node with embedType '' and
    empty metadata. Its markup is the provider's, and rewriting a width or
    injecting centering would break their player."""
    bandcamp = {
        "type": "embed", "version": 1,
        "url": "https://bandcamp.com/EmbeddedPlayer/album=123/size=large/",
        "embedType": "", "metadata": {}, "caption": "",
        "html": '<iframe style="border: 0; width: 100%; height: 120px;" '
                'src="https://bandcamp.com/EmbeddedPlayer/album=123/size=large/" '
                'seamless=""><a href="https://x.bandcamp.com/album/y">Y</a></iframe>',
    }
    result, _ = enrich(doc([dict(bandcamp)]), {}, None)
    assert result["root"]["children"][0] == bandcamp


def test_html_quote_and_code_cards_are_untouched():
    cards = [
        {"type": "html", "version": 1, "html": "<table><tr><td>1</td></tr></table>",
         "visibility": {"web": {"nonMember": True}}},
        {"type": "extended-quote", "version": 1, "children": []},
        {"type": "codeblock", "version": 1, "language": "python", "code": "print(1)"},
        {"type": "horizontalrule", "version": 1},
    ]
    result, _ = enrich(doc([dict(c) for c in cards]), {}, None)
    assert result["root"]["children"] == cards


def test_the_input_document_is_not_mutated():
    original = doc([image_node(), spacer(), image_node()])
    snapshot = json.dumps(original)
    enrich(original, {}, None)
    assert json.dumps(original) == snapshot
