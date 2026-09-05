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
    payload_for,
    split_front_matter,
    strip_scaffolding,
)
from verify_post import (  # noqa: E402
    compare,
    front_matter_leak,
    lexical_text,
    load_lexical,
    markdown_to_text,
    scaffolding_leak,
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


def test_slug_is_not_rendered_as_a_flag_on_a_post():
    """ghst 0.16.6 `post create` has no --slug option and rejects it with
    `error: unknown option '--slug'`. Emitting it handed the caller a command
    that cannot run, and nothing here covered it -- which is the same failure
    this skill exists to catch, one level up: the tests asserted what was
    expected and never asked what was absent."""
    meta = parse_front_matter(split_front_matter(FRONT_MATTER)[0])
    assert meta["slug"] == "measuring-register"
    assert "--slug" not in ghst_flags(meta)
    assert "--slug" not in ghst_flags(meta, "post")


def test_slug_is_rendered_as_a_flag_on_a_page():
    """The asymmetry is ghst's, not ours: `page create --slug <slug>` exists
    and sets the slug, verified against 0.16.6."""
    meta = parse_front_matter(split_front_matter(FRONT_MATTER)[0])
    flags = ghst_flags(meta, "page")
    assert "--slug" in flags
    assert flags[flags.index("--slug") + 1] == "measuring-register"


def test_payload_carries_the_slug_under_ghosts_own_field_names():
    """--from-json is the only route that sets a post's slug. It takes Ghost's
    API field names rather than the CLI's flag names, so `excerpt` becomes
    `custom_excerpt` and tags become objects."""
    payload = payload_for(parse_front_matter(split_front_matter(FRONT_MATTER)[0]))
    assert payload["slug"] == "measuring-register"
    assert payload["custom_excerpt"] == "A short excerpt."
    assert payload["tags"] == [{"name": "Writing"}, {"name": "Claude Skills"},
                               {"name": "Python"}]
    assert "excerpt" not in payload


def test_payload_does_not_carry_status():
    """Same rule as the flags: publishing is a state transition and must be a
    deliberate command, never a field smuggled in from a file."""
    payload = payload_for(parse_front_matter(split_front_matter(FRONT_MATTER)[0]))
    assert "status" not in payload


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
# SKILL.md and README.md both say "verified against the pinned release -- re-check the traps after an
# upgrade". That is a pin written in prose. `ghst` is pre-1.0 with 27 tags and 30 commits in
# the thirty days to 2026-08-17, and the four behaviours this skill leans on are UNDOCUMENTED,
# so no semver promise covers them.
#
# The design decision these tests protect is that DRIFT WARNS RATHER THAN BLOCKS. A check that
# refuses to run the day ghst ships a patch gets commented out, and then real drift arrives
# unannounced. Exit 1 is reserved for "the check could not be made at all".


def _proc(out="", code=0):
    return type("P", (), {"stdout": out, "returncode": code, "stderr": ""})()


def _rel(patch=0, minor=0, major=0) -> str:
    """A version string relative to whatever preflight currently pins.

    These tests are about how `report` CLASSIFIES a version, not about any
    particular release. Hardcoding the pin made every legitimate bump look
    like three test failures, which trains people to edit the tests rather
    than re-verify the traps -- exactly backwards.
    """
    a, b, c = preflight.parse(preflight.VERIFIED)
    return f"{a + major}.{b + minor if not major else 0}.{c + patch if not (minor or major) else 0}"


def test_the_verified_version_passes():
    code, lines = preflight.report(_rel(), None)
    assert code == 0
    assert lines[0].startswith("ok")


def test_a_patch_ahead_warns_and_does_not_block():
    """THE DESIGN DECISION. ghst ships patches weekly; blocking here would get this deleted."""
    code, lines = preflight.report(_rel(patch=1), None)
    assert code == 0, "a patch bump must not block a publish"
    assert lines[0].startswith("WARN") and "patch ahead" in lines[0]


def test_a_minor_release_is_called_out_differently_from_a_patch():
    """A minor bump is where an undocumented behaviour plausibly moves. Same exit code, louder
    words -- the reader decides, but they are not told it is routine."""
    code, lines = preflight.report(_rel(minor=1), None)
    assert code == 0
    assert "MINOR" in lines[0]
    assert not any("usually harmless" in line for line in lines)


def test_a_major_release_says_major():
    _, lines = preflight.report(_rel(major=1), None)
    assert "MAJOR" in lines[0]


def test_an_older_ghst_is_its_own_case():
    """Older is not the same risk as newer: the traps were written against behaviour this
    build may not have yet. Calling it 'behind' rather than folding it into drift keeps the
    two apart."""
    _, lines = preflight.report(_rel(patch=-1), None)
    assert "OLDER" in lines[0]


def test_every_drift_warning_names_the_traps_to_recheck():
    """A warning that says 'version changed' and stops is noise. The value is naming the four
    undocumented behaviours, because those are what a release note will never mention."""
    for version in (_rel(patch=1), _rel(minor=1), _rel(major=1), _rel(patch=-1)):
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
    code, lines = preflight.report(_rel(), None, verified="latest")
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


# ---------------------------------------------------------------------------
# prepare_post: drafting scaffolding. On 2026-09-02 a leading H1 and an author
# note reached a PUBLIC post, because removing the front matter and stopping
# left them in place. Both directions are asserted here -- a clean post must
# come through untouched, or the fix is worse than the bug it replaces.
# ---------------------------------------------------------------------------

SCAFFOLDED = """# Four Millimetres From Unsearchable: CI Gates for a CV

*For techblog.kevinburns.de. Audience: Claude Code / agent builders.*

---

A colleague rebuilt his CV as code.
"""


def test_the_three_pieces_of_scaffolding_are_all_removed():
    body, removed, _ = strip_scaffolding(SCAFFOLDED)

    assert body.startswith("A colleague rebuilt his CV as code.")
    assert len(removed) == 3


def test_every_removal_is_reported_rather_than_silent():
    # The whole reason a silent strip is unacceptable: an H1 somebody meant to
    # keep would vanish with nothing to notice.
    _, removed, _ = strip_scaffolding(SCAFFOLDED)

    assert any("H1" in line for line in removed)
    assert any("author note" in line for line in removed)
    assert any("horizontal rule" in line for line in removed)


def test_a_post_that_opens_on_prose_is_untouched():
    # The regression that matters. Three published posts open straight into
    # prose and must come through byte-identical.
    clean = "My most recent post used no contractions. Not one.\n\n# A real heading\n"

    body, removed, warnings = strip_scaffolding(clean)

    assert body == clean
    assert removed == []
    assert warnings == []


def test_a_draft_marker_author_note_is_recognised():
    text = "# Title\n\n*Draft 2 — for the blog. Post 2 of a series.*\n\nReal prose.\n"

    body, removed, _ = strip_scaffolding(text)

    assert body.startswith("Real prose.")
    assert len(removed) == 2


def test_an_italic_epigraph_is_kept_and_warned_about_rather_than_stripped():
    # Stripping on suspicion would eat a legitimate opening line. A warning
    # costs the author one glance; a wrong strip is invisible.
    text = "*For years I believed the tests were the hard part.*\n\nThey were not.\n"

    body, removed, warnings = strip_scaffolding(text)

    assert body == text
    assert removed == []
    assert len(warnings) == 1


def test_a_line_with_two_emphasis_spans_is_not_treated_as_an_author_note():
    text = "Some *emphasis* and more *emphasis* about the audience: here.\n"

    body, removed, warnings = strip_scaffolding(text)

    assert body == text
    assert (removed, warnings) == ([], [])


def test_a_horizontal_rule_is_kept_when_nothing_above_it_was_removed():
    # A post may legitimately open on a rule. It is only scaffolding when it
    # was sitting under scaffolding.
    text = "---\n\nProse follows.\n"

    body, removed, _ = strip_scaffolding(text)

    assert body == text
    assert removed == []


def test_scaffolding_further_down_the_document_survives():
    # Only the very top is scaffolding. An H1 in the body is the author's.
    text = "Opening prose.\n\n# A heading they wrote\n\n*Audience: nobody*\n"

    body, removed, _ = strip_scaffolding(text)

    assert body == text
    assert removed == []


def test_an_author_note_wrapped_in_a_heading_is_still_an_author_note():
    text = "# Title\n\n## *For the blog. Audience: agent builders.*\n\nProse.\n"

    body, removed, _ = strip_scaffolding(text)

    assert body.startswith("Prose.")
    assert len(removed) == 2


# ---------------------------------------------------------------------------
# verify_post: the scaffolding was invisible to "did my content arrive" -- all
# of it had. These ask the other question, in the two places the answer lives.
# ---------------------------------------------------------------------------

def test_an_author_note_in_the_published_text_is_reported():
    ghost = "For techblog.kevinburns.de. Audience: agent builders. A colleague rebuilt his CV."

    assert scaffolding_leak(ghost)


def test_a_leading_h1_node_is_reported_even_when_the_text_looks_clean():
    # The case sentence comparison cannot see: the words are legitimately in
    # the source too, so only the node type gives it away.
    doc = {"root": {"children": [{"type": "extended-heading", "tag": "h1"},
                                 {"type": "paragraph"}]}}

    found = scaffolding_leak("A colleague rebuilt his CV as code.", doc)

    assert any("h1" in f for f in found)


def test_a_clean_post_reports_no_scaffolding():
    doc = {"root": {"children": [{"type": "paragraph"}, {"type": "paragraph"}]}}

    assert scaffolding_leak("My most recent post used no contractions.", doc) == []


def test_an_h2_is_not_mistaken_for_a_duplicated_title():
    # Body headings are ordinary. Only h1 duplicates Ghost's own title field.
    doc = {"root": {"children": [{"type": "extended-heading", "tag": "h2"}]}}

    assert scaffolding_leak("Prose.", doc) == []


def test_compare_surfaces_scaffolding_alongside_the_sentence_diff():
    doc = {"root": {"children": [{"type": "extended-heading", "tag": "h1"}]}}

    result = compare("Prose.", "Prose.", doc)

    assert result["missing"] == [] and result["extra"] == []
    assert result["scaffolding"], "identical content must not mask scaffolding"
