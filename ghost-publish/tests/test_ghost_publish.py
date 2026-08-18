import json
import sys
from pathlib import Path

# No pyproject.toml pythonpath config in this skill, so the scripts dir goes
# on sys.path here -- keeps the test file self-contained, same as
# clear-and-human's suite.
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import preflight  # noqa: E402
from prepare_post import (  # noqa: E402
    ghst_flags,
    parse_front_matter,
    split_front_matter,
)
from verify_post import (  # noqa: E402
    compare,
    front_matter_leak,
    lexical_text,
    load_lexical,
    markdown_to_text,
)

FRONT_MATTER = """---
title: "Zero Contractions in 1,375 Words"
slug: measuring-register
excerpt: "A short excerpt."
tags: [Writing, Claude Skills, Python]
status: draft
---

My most recent post used no contractions.
"""


# ---------------------------------------------------------------------------
# prepare_post: the failure this whole skill exists to prevent is YAML
# arriving in the post body, so the stripper is the load-bearing part.
# ---------------------------------------------------------------------------

def test_front_matter_is_removed_from_the_body():
    front, body = split_front_matter(FRONT_MATTER)
    assert "title:" in front
    assert "title:" not in body
    assert body.lstrip().startswith("My most recent post")


def test_a_horizontal_rule_mid_document_is_not_mistaken_for_front_matter():
    """A `---` fence only counts at the very start of the file. Treating a
    thematic break as front matter would silently delete the first section
    of the post."""
    text = "First paragraph.\n\n---\n\nSecond paragraph.\n"
    front, body = split_front_matter(text)
    assert front == ""
    assert body == text


def test_file_without_front_matter_passes_through_unchanged():
    text = "Just prose, no metadata.\n"
    front, body = split_front_matter(text)
    assert front == ""
    assert body == text


def test_inline_list_and_quoted_scalars_parse():
    meta = parse_front_matter(split_front_matter(FRONT_MATTER)[0])
    assert meta["title"] == "Zero Contractions in 1,375 Words"  # quotes stripped
    assert meta["slug"] == "measuring-register"
    assert meta["tags"] == ["Writing", "Claude Skills", "Python"]


def test_flags_render_a_tag_list_as_one_comma_separated_value():
    """ghst takes --tags as a single comma-separated argument, and it
    REPLACES the tag set rather than adding to it, so the full list has to
    survive as one value."""
    flags = ghst_flags(parse_front_matter(split_front_matter(FRONT_MATTER)[0]))
    assert "--tags" in flags
    assert flags[flags.index("--tags") + 1] == "Writing,Claude Skills,Python"


def test_status_is_not_translated_into_a_flag():
    """Publishing is a state transition and must be a deliberate command --
    never inferred from a line in a file."""
    meta = parse_front_matter(split_front_matter(FRONT_MATTER)[0])
    assert meta["status"] == "draft"
    assert "--status" not in ghst_flags(meta)


def test_unsupported_yaml_is_reported_rather_than_guessed_at():
    """Block lists and nested maps are out of scope. Silently mis-parsing one
    would be worse than saying it was skipped."""
    meta = parse_front_matter("tags:\n  - Writing\n  - Python\n")
    assert "_unparsed" in meta
    assert any("Writing" in line for line in meta["_unparsed"])


# ---------------------------------------------------------------------------
# verify_post: the three ways of reading a Ghost post back, each of which
# produced a false "everything is missing" during development.
# ---------------------------------------------------------------------------

def test_text_is_recovered_from_a_markdown_card():
    doc = {"root": {"children": [
        {"type": "markdown", "markdown": "A sentence from a markdown card."}]}}
    assert "markdown card" in lexical_text(doc)


def test_text_is_recovered_from_native_paragraph_nodes():
    """post update --markdown-file converts to native Lexical, so the same
    post is a markdown card before the update and paragraph nodes after."""
    doc = {"root": {"children": [
        {"type": "paragraph", "children": [{"type": "text", "text": "Native node text."}]},
        {"type": "extended-heading", "children": [{"type": "text", "text": "A heading."}]},
    ]}}
    text = lexical_text(doc)
    assert "Native node text." in text
    assert "A heading." in text


def test_html_cards_are_stripped_to_their_text():
    doc = {"root": {"children": [{"type": "html", "html": "<p>Inside an <b>html</b> card.</p>"}]}}
    assert "Inside an html card." in " ".join(lexical_text(doc).split())


def test_double_encoded_lexical_is_decoded(tmp_path):
    """Ghost stores the document as a JSON string inside the post object, so
    --jq '.posts[0].lexical' returns a quoted blob needing two decodes."""
    inner = json.dumps({"root": {"children": [
        {"type": "paragraph", "children": [{"type": "text", "text": "Twice encoded."}]}]}})
    path = tmp_path / "lexical.json"
    path.write_text(json.dumps(inner))
    assert "Twice encoded." in lexical_text(load_lexical(path))


def test_null_lexical_explains_the_missing_html_field(tmp_path):
    """`--jq '.posts[0].html'` yields null. Five bytes of nothing must not
    read as total content loss."""
    path = tmp_path / "lexical.json"
    path.write_text("null")
    try:
        load_lexical(path)
    except SystemExit as err:
        assert "no html" in str(err)
    else:
        raise AssertionError("null lexical should have raised")


# ---------------------------------------------------------------------------
# The both-directions rule. A check that only asserts presence passed 16/16
# while front matter sat above the first paragraph; these are the assertions
# that would have caught it.
# ---------------------------------------------------------------------------

def test_front_matter_in_the_body_is_flagged():
    leaked = ("title: Zero Contractions slug: measuring-register "
              "tags: [Writing] status: draft My most recent post used none.")
    assert set(front_matter_leak(leaked)) >= {"title:", "slug:", "tags:", "status:"}


def test_leaked_text_is_reported_as_extra_not_ignored():
    source = "My most recent post used no contractions at all, not one."
    ghost = ('title: "Zero Contractions" slug: measuring-register. '
             "My most recent post used no contractions at all, not one.")
    result = compare(source, ghost)
    assert result["leak"], "front matter markers should be detected"
    assert result["extra"], "the leaked line should appear as IN GHOST, NOT IN SOURCE"


def test_dropped_content_is_reported_as_missing():
    source = ("The first sentence is long enough to survive the filter. "
              "The second sentence is also long enough to be counted here.")
    ghost = "The first sentence is long enough to survive the filter."
    result = compare(source, ghost)
    assert any("second sentence" in s for s in result["missing"])
    assert result["extra"] == []


def test_identical_content_reports_no_differences():
    text = ("The first sentence is long enough to survive the filter. "
            "The second sentence is also long enough to be counted here.")
    result = compare(text, text)
    assert result["missing"] == []
    assert result["extra"] == []
    assert result["leak"] == []


def test_markdown_syntax_alone_is_not_a_difference():
    """Ghost has already turned markdown into structure by the time we read
    it back. If syntax counted as content, every heading and bold span would
    report as a difference and the real findings would drown."""
    source = "## A heading here\n\nSome **bold** text with a [link](https://example.com) in it."
    ghost = "A heading here Some bold text with a link in it."
    result = compare(source, ghost)
    assert result["missing"] == []
    assert result["extra"] == []


def test_markdown_to_text_keeps_link_text_and_drops_the_target():
    out = markdown_to_text("See [the docs](https://example.com/page) for more.")
    assert "the docs" in out
    assert "example.com" not in out


def test_formatting_boundaries_do_not_read_as_differences():
    """Regression. Lexical stores a formatted span as its own text node, so
    an italic phrase rejoins with a space before the following comma. On a
    real 4,500-word post this reported 13 false 'missing' and 12 false
    'extra' lines -- the same sentences on both sides, differing only in
    whitespace."""
    source = "Biber's loading is on *demonstrative pronouns*, so the citation was misplaced."
    ghost = "Biber's loading is on demonstrative pronouns , so the citation was misplaced."
    result = compare(source, ghost)
    assert result["missing"] == []
    assert result["extra"] == []


def test_underscored_identifiers_survive_normalisation():
    """Regression. Stripping `_` as an emphasis marker turned
    `fidelity_check.py` into `fidelitycheck.py` on the source side only, so
    every sentence naming a script reported as changed."""
    source = "The second script, `fidelity_check.py`, does a different job entirely."
    ghost = "The second script, fidelity_check.py , does a different job entirely."
    result = compare(source, ghost)
    assert result["missing"] == []
    assert result["extra"] == []


def test_table_cell_text_is_kept_not_discarded():
    """Regression. Dropping table rows wholesale from the source made Ghost's
    rendered cells look like content Ghost had invented."""
    source = ("| document | contractions |\n|---|---|\n"
              "| the drift detection post | seventeen point six |\n")
    ghost = "document contractions the drift detection post seventeen point six"
    result = compare(source, ghost)
    assert result["extra"] == [], "rendered table cells must not read as invented content"


# --- preflight: the pin that nothing enforced ----------------------------------------
#
# SKILL.md and README.md both say "verified against 0.16.5 -- re-check the traps after an
# upgrade". That is a pin written in prose. `ghst` is pre-1.0 with 27 tags and 30 commits in
# the thirty days to 2026-08-17, and the four behaviours this skill leans on are UNDOCUMENTED,
# so no semver promise covers them.
#
# The design decision these tests protect is that DRIFT WARNS RATHER THAN BLOCKS. A check that
# refuses to run the day ghst ships a patch gets commented out, and then real drift arrives
# unannounced. Exit 1 is reserved for "the check could not be made at all".


def _proc(out="", code=0):
    return type("P", (), {"stdout": out, "returncode": code, "stderr": ""})()


def test_the_verified_version_passes():
    code, lines = preflight.report("0.16.5", None)
    assert code == 0
    assert lines[0].startswith("ok")


def test_a_patch_ahead_warns_and_does_not_block():
    """THE DESIGN DECISION. ghst ships patches weekly; blocking here would get this deleted."""
    code, lines = preflight.report("0.16.6", None)
    assert code == 0, "a patch bump must not block a publish"
    assert lines[0].startswith("WARN") and "patch ahead" in lines[0]


def test_a_minor_release_is_called_out_differently_from_a_patch():
    """A minor bump is where an undocumented behaviour plausibly moves. Same exit code, louder
    words -- the reader decides, but they are not told it is routine."""
    code, lines = preflight.report("0.17.0", None)
    assert code == 0
    assert "MINOR" in lines[0]
    assert not any("usually harmless" in line for line in lines)


def test_a_major_release_says_major():
    _, lines = preflight.report("1.0.0", None)
    assert "MAJOR" in lines[0]


def test_an_older_ghst_is_its_own_case():
    """Older is not the same risk as newer: the traps were written against behaviour this
    build may not have yet. Calling it 'behind' rather than folding it into drift keeps the
    two apart."""
    _, lines = preflight.report("0.16.4", None)
    assert "OLDER" in lines[0]


def test_every_drift_warning_names_the_traps_to_recheck():
    """A warning that says 'version changed' and stops is noise. The value is naming the four
    undocumented behaviours, because those are what a release note will never mention."""
    for version in ("0.16.6", "0.17.0", "1.0.0", "0.16.4"):
        _, lines = preflight.report(version, None)
        body = "\n".join(lines)
        for trap in preflight.TRAPS:
            assert trap in body, (version, trap)


def test_a_missing_ghst_is_a_failure_not_a_warning():
    """This one IS blocking: with no ghst there is nothing to check and nothing to publish."""
    code, lines = preflight.report("", "ghst is not on PATH -- npm i -g @tryghost/ghst")
    assert code == 1
    assert lines[0].startswith("FAIL") and "npm i -g" in lines[0]


def test_an_unparseable_version_fails_rather_than_passing_quietly():
    """The dangerous middle: ghst runs, prints something unexpected, and a check that shrugged
    would report success while verifying nothing."""
    code, lines = preflight.report("some banner with no digits", None)
    assert code == 1
    assert "could not parse" in lines[0]


def test_a_nonstandard_verified_argument_is_refused():
    code, lines = preflight.report("0.16.5", None, verified="latest")
    assert code == 1
    assert "not an x.y.z" in lines[0]


def test_the_version_is_found_inside_a_noisy_banner():
    """`ghst --version` prints a bare version today, but CLIs grow banners."""
    assert preflight.parse("@tryghost/ghst v0.16.5 (node 22)") == (0, 16, 5)
    assert preflight.parse("") is None


def test_a_nonzero_exit_from_ghst_is_reported_not_parsed():
    raw, err = preflight.read_version(run=lambda: _proc("", 127))
    assert raw == "" and "exited 127" in err


def test_a_version_read_that_raises_is_caught():
    def boom():
        raise OSError("no such file")

    raw, err = preflight.read_version(run=boom)
    assert "could not run" in err and "OSError" in err


def test_the_verified_constant_matches_what_the_docs_claim():
    """THE POINT OF THE WHOLE FILE. If SKILL.md says one version and the code checks another,
    the enforcement is decorative."""
    root = Path(__file__).parent.parent
    for name in ("SKILL.md", "README.md"):
        text = (root / name).read_text()
        assert preflight.VERIFIED in text, f"{name} does not mention {preflight.VERIFIED}"
