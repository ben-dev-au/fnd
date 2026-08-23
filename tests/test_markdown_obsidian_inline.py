"""Pin: Obsidian inline syntax renders, and indexed text is never hidden."""

from __future__ import annotations

from textual.content import Content, Span

from fnd.matching import MatchSpec
from fnd.tui.widgets.content_edits import apply_edits
from fnd.tui.widgets.md_inline import (
    MARK_STYLE,
    REVEAL_STYLE,
    TAG_STYLE,
    WIKILINK_STYLE,
    collect_edits,
)

EMPTY = MatchSpec()


def _render(plain: str, *, spec: MatchSpec = EMPTY, list_item: bool = False) -> str:
    edits = collect_edits(plain, protected=set(), spec=spec, list_item=list_item)
    return apply_edits(Content(plain), edits).plain


def test_plain_text_is_untouched() -> None:
    assert _render("nothing to do here") == "nothing to do here"


def test_highlight_markers_are_stripped() -> None:
    assert _render("a ==marked== b") == "a marked b"


def test_highlight_carries_the_mark_style() -> None:
    edits = collect_edits("a ==marked== b", protected=set(), spec=EMPTY, list_item=False)
    out = apply_edits(Content("a ==marked== b"), edits)
    assert [(s.start, s.end, str(s.style)) for s in out.spans] == [(2, 8, MARK_STYLE)]


def test_highlight_inside_inline_code_is_left_literal() -> None:
    # markdown-it strips the backticks: the built Content is "use ==x== here"
    # with a ``.code_inline`` span over positions 4-9.
    plain = "use ==x== here"
    edits = collect_edits(plain, protected=set(range(4, 9)), spec=EMPTY, list_item=False)
    assert edits == []


def test_unchecked_task_becomes_a_box() -> None:
    assert _render("[ ] buy milk", list_item=True) == "☐ buy milk"


def test_checked_task_becomes_a_ticked_box() -> None:
    assert _render("[x] buy milk", list_item=True) == "☑ buy milk"
    assert _render("[X] buy milk", list_item=True) == "☑ buy milk"


def test_checkbox_only_applies_at_the_start_of_a_list_item() -> None:
    assert _render("text [ ] mid", list_item=True) == "text [ ] mid"
    assert _render("[ ] not a list item", list_item=False) == "[ ] not a list item"


def test_bare_wikilink_loses_its_brackets() -> None:
    assert _render("see [[Alpha Note]] now") == "see Alpha Note now"


def test_bare_wikilink_is_link_coloured() -> None:
    edits = collect_edits("see [[Alpha]]", protected=set(), spec=EMPTY, list_item=False)
    out = apply_edits(Content("see [[Alpha]]"), edits)
    assert [(s.start, s.end, str(s.style)) for s in out.spans] == [(4, 9, WIKILINK_STYLE)]


def test_aliased_wikilink_shows_only_the_alias() -> None:
    assert _render("see [[Projects/Alpha|the Alpha note]]") == "see the Alpha note"


def test_aliased_target_is_revealed_when_it_matches() -> None:
    spec = MatchSpec.from_query("projects", auto_fuzzy=False)
    out = _render("see [[Projects/Alpha|the Alpha note]]", spec=spec)
    assert out == "see the Alpha note ⟨Projects/Alpha⟩"


def test_revealed_target_is_dimmed() -> None:
    plain = "see [[Projects/Alpha|the Alpha note]]"
    spec = MatchSpec.from_query("projects", auto_fuzzy=False)
    edits = collect_edits(plain, protected=set(), spec=spec, list_item=False)
    out = apply_edits(Content(plain), edits)
    styles = {str(s.style) for s in out.spans}
    assert REVEAL_STYLE in styles
    assert WIKILINK_STYLE in styles


def test_alias_matching_does_not_reveal_the_target() -> None:
    spec = MatchSpec.from_query("alpha", auto_fuzzy=False)
    # "alpha" is in the alias, so the alias alone already carries the match.
    assert _render("see [[Projects/Beta|the alpha note]]", spec=spec) == "see the alpha note"


def test_embed_renders_with_an_embed_glyph() -> None:
    assert _render("![[diagram.png]]") == "▣ diagram.png"


def test_wikilink_inside_inline_code_is_left_literal() -> None:
    plain = "use [[x]] here"
    edits = collect_edits(plain, protected=set(range(4, 9)), spec=EMPTY, list_item=False)
    assert edits == []


def test_tag_keeps_its_text_and_gains_a_style() -> None:
    plain = "filed under #uni/web today"
    edits = collect_edits(plain, protected=set(), spec=EMPTY, list_item=False)
    out = apply_edits(Content(plain), edits)
    assert out.plain == plain
    assert [(s.start, s.end, str(s.style)) for s in out.spans] == [(12, 20, TAG_STYLE)]


def test_heading_marker_prefix_is_not_a_tag() -> None:
    assert collect_edits("## Heading", protected=set(), spec=EMPTY, list_item=False) == []


def test_hash_mid_word_is_not_a_tag() -> None:
    assert collect_edits("C# and F#", protected=set(), spec=EMPTY, list_item=False) == []


def test_comment_is_hidden_when_it_holds_no_match() -> None:
    assert _render("keep %%secret note%% keep") == "keep  keep"


def test_comment_is_revealed_when_it_holds_a_match() -> None:
    spec = MatchSpec.from_query("secret", auto_fuzzy=False)
    assert _render("keep %%secret note%% keep", spec=spec) == "keep secret note keep"


def test_block_id_is_hidden_when_it_holds_no_match() -> None:
    assert _render("a sentence ^a1b2c3") == "a sentence"


def test_block_id_is_revealed_when_it_matches() -> None:
    spec = MatchSpec.from_query("a1b2c3", auto_fuzzy=False)
    assert _render("a sentence ^a1b2c3", spec=spec) == "a sentence ^a1b2c3"


def test_caret_mid_sentence_is_not_a_block_id() -> None:
    assert collect_edits("2^10 is 1024", protected=set(), spec=EMPTY, list_item=False) == []


def test_ordinary_prose_and_code_punctuation_survives() -> None:
    """Negative guard: the rules must not fire on everyday text."""
    for src in (
        "C# and F# are languages",
        "colour #fff and #ff0000",
        "2^10 is 1024",
        "a == b and c == d",
        "see https://x.com/a#frag now",
    ):
        assert _render(src) == src, src


def test_known_false_positives_match_obsidian() -> None:
    """Accepted: bracket/equals pairs in prose render as Obsidian renders them."""
    # R/Julia list indexing in prose is indistinguishable from a wikilink, and
    # Obsidian resolves it the same way. Inline code (protected) is the escape.
    assert _render("array[[0]][1]") == "array0[1]"
    assert _render("x ==2 and y== 3") == "x 2 and y 3"


def test_multiline_comment_inside_one_paragraph_is_hidden() -> None:
    """A real newline, so the ``re.S`` flag on _COMMENT is actually exercised."""
    assert _render("before %%a\nb\nc%% after") == "before  after"


def test_comment_containing_nested_syntax_is_still_hidden() -> None:
    """A comment claims its range before wikilink/tag/mark rules can take it."""
    assert _render("%%a [[b]] c%%") == ""
    assert _render("%%a #tag b%%") == ""
    assert _render("%%a ==m== b%%") == ""


def test_revealed_comment_keeps_its_nested_syntax_literal() -> None:
    spec = MatchSpec.from_query("secret", auto_fuzzy=False)
    assert _render("%%a [[b]] secret%%", spec=spec) == "a [[b]] secret"


def test_inline_formatting_inside_a_mark_survives() -> None:
    """Stripping ``==`` must shift inner spans, not collapse them."""
    plain = "==a b c=="
    content = Content(plain, spans=[Span(4, 5, ".strong")])
    out = apply_edits(content, collect_edits(plain, protected=set(), spec=EMPTY, list_item=False))
    strong = [s for s in out.spans if str(s.style) == ".strong"]
    assert strong, out.spans
    assert out.plain[strong[0].start : strong[0].end] == "b"


def test_inline_formatting_inside_a_wikilink_survives() -> None:
    for plain, span in (("[[a b c]]", (4, 5)), ("[[t|a b c]]", (6, 7))):
        content = Content(plain, spans=[Span(*span, ".strong")])
        out = apply_edits(
            content, collect_edits(plain, protected=set(), spec=EMPTY, list_item=False)
        )
        strong = [s for s in out.spans if str(s.style) == ".strong"]
        assert strong, (plain, out.spans)
        assert out.plain[strong[0].start : strong[0].end] == "b"
