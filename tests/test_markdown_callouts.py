"""Pin: the callout token pass retags blockquotes and splits off the title."""

from __future__ import annotations

import pytest
from markdown_it import MarkdownIt
from markdown_it.token import Token

from fnd.tui.widgets.callouts import resolve_callout, rewrite_callouts


def _tokens(src: str) -> list[Token]:
    tokens = MarkdownIt("gfm-like").parse(src)
    rewrite_callouts(tokens)
    return tokens


def _inlines(tokens: list[Token]) -> list[str]:
    return [t.content for t in tokens if t.type == "inline"]


def test_plain_blockquote_is_left_alone() -> None:
    tokens = _tokens("> just a quote\n")
    assert "fnd_callout" not in tokens[0].meta
    assert _inlines(tokens) == ["just a quote"]


def test_callout_tags_the_blockquote_with_its_type() -> None:
    tokens = _tokens("> [!tip] Cap the rows\n> Body text.\n")
    assert tokens[0].type == "blockquote_open"
    assert tokens[0].meta["fnd_callout"] == "tip"


def test_title_and_body_become_separate_paragraphs() -> None:
    tokens = _tokens("> [!tip] Cap the rows\n> Body text.\n")
    assert _inlines(tokens) == ["Cap the rows", "Body text."]


def test_title_paragraph_carries_icon_metadata() -> None:
    tokens = _tokens("> [!warning] Careful\n> Body.\n")
    title_open = next(
        t for t in tokens if t.type == "paragraph_open" and "fnd_callout_title" in t.meta
    )
    assert title_open.meta["fnd_callout_title"] == ("warning", "▲", False)


def test_callout_without_a_title_uses_the_type_label() -> None:
    tokens = _tokens("> [!note]\n> Body only.\n")
    assert _inlines(tokens) == ["Note", "Body only."]


def test_callout_with_no_body_produces_only_a_title() -> None:
    tokens = _tokens("> [!tip] Just a title\n")
    assert _inlines(tokens) == ["Just a title"]


@pytest.mark.parametrize("fold", ["-", "+"])
def test_fold_marker_is_recorded_and_stripped(fold: str) -> None:
    tokens = _tokens(f"> [!tip]{fold} Foldable\n> Body.\n")
    title_open = next(
        t for t in tokens if t.type == "paragraph_open" and "fnd_callout_title" in t.meta
    )
    assert title_open.meta["fnd_callout_title"][2] is True
    assert _inlines(tokens)[0] == "Foldable"


def test_github_uppercase_alerts_resolve() -> None:
    tokens = _tokens("> [!WARNING] Heads up\n")
    assert tokens[0].meta["fnd_callout"] == "warning"


def test_unknown_type_keeps_its_word_and_falls_back_to_note_styling() -> None:
    style = resolve_callout("gibberish")
    assert style.key == "note"
    assert style.label == "gibberish"


def test_nested_callouts_are_each_retagged() -> None:
    tokens = _tokens("> [!info] Outer\n> > [!danger] Inner\n> > Boom.\n")
    keys = [t.meta.get("fnd_callout") for t in tokens if t.type == "blockquote_open"]
    assert keys == ["info", "danger"]


def test_title_keeps_inline_markup_children() -> None:
    # Token .content joins child text only — the ** markup is carried by the
    # strong_open/strong_close children, not by any child's content.
    tokens = _tokens("> [!tip] A **bold** title\n> Body.\n")
    assert _inlines(tokens)[0] == "A bold title"
    title_inline = next(t for t in tokens if t.type == "inline")
    assert any(c.type == "strong_open" for c in title_inline.children or [])


def test_marker_inside_a_fence_is_not_a_callout() -> None:
    tokens = _tokens("```\n> [!tip] not a callout\n```\n")
    assert not any(t.meta.get("fnd_callout") for t in tokens)
