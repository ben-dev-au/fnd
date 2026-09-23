"""``body_struct`` → highlighted Markdown render."""

from __future__ import annotations

from fnd.extract.base import Block
from fnd.render import render


def test_headings_render_with_correct_level() -> None:
    md = render([Block(kind="h1", text="Top"), Block(kind="h2", text="Sub")])
    assert "# Top" in md
    assert "## Sub" in md


def test_paragraphs_have_blank_line() -> None:
    md = render([Block(kind="p", text="A"), Block(kind="p", text="B")])
    assert "A\n\nB" in md


def test_query_term_is_bolded() -> None:
    md = render([Block(kind="p", text="The blue penguin sandwich is here.")], query="penguin")
    assert "**penguin**" in md


def test_highlighter_is_case_insensitive() -> None:
    md = render([Block(kind="p", text="QUARK gluon Quark")], query="quark")
    assert md.count("**QUARK**") == 1
    assert md.count("**Quark**") == 1


def test_highlighter_strips_field_qualifiers() -> None:
    """A query like `kind:pdf supersymmetry` should only highlight 'supersymmetry'."""
    md = render(
        [Block(kind="p", text="kind pdf and supersymmetry")],
        query="kind:pdf supersymmetry",
    )
    # Bare "kind" / "pdf" should not be bolded; "supersymmetry" should.
    assert "**supersymmetry**" in md
    assert "**kind**" not in md
    assert "**pdf**" not in md


def test_highlighter_strips_proximity_braces() -> None:
    md = render([Block(kind="p", text="foo bar baz")], query="{5} foo bar baz")
    assert "**foo**" in md
    assert "**bar**" in md
    assert "**baz**" in md
    assert "**5**" not in md


def test_quote_block_renders_with_caret() -> None:
    md = render([Block(kind="quote", text="speaker note")])
    assert md.startswith("> ")


def test_code_block_does_not_bold_inside() -> None:
    md = render([Block(kind="code", text="grep penguin /tmp")], query="penguin")
    # Code block is fenced; no bold inside.
    assert "```" in md
    # The literal "penguin" is inside the fence and must NOT be bolded.
    assert "**penguin**" not in md
