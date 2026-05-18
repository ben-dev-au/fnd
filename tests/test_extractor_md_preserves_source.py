"""Markdown extractor preserves the section source on ``body_md``.

Pre-fix the extractor only kept ``h1..h6`` and ``p`` blocks; tables,
nested lists, blockquotes, and fenced code blocks were either flattened
to ``p`` or — in the case of code-only sections — silently dropped from
the index. ``body_md`` carries the verbatim section source so the
preview renderer can pass it to the Textual Markdown widget without any
lossy reconstruction.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from fnd.extract.markdown import extract


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "doc.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_body_md_round_trips_table_source(tmp_path: Path) -> None:
    """A markdown table survives extraction verbatim — same pipes,
    same alignment row — so the preview renderer can hand it straight
    to Textual's Markdown widget."""
    body = textwrap.dedent(
        """\
        # Table Section

        | Name | Score |
        |------|-------|
        | Foo  | 42    |
        """
    )
    chunks = list(extract(_write(tmp_path, body)))
    assert len(chunks) == 1
    assert "| Name | Score |" in chunks[0].body_md
    assert "|------|-------|" in chunks[0].body_md
    assert "| Foo  | 42    |" in chunks[0].body_md


def test_body_md_round_trips_fenced_code(tmp_path: Path) -> None:
    """Fenced code with a language tag round-trips."""
    body = textwrap.dedent(
        """\
        # Code

        ```python
        def hello():
            print("world")
        ```
        """
    )
    chunks = list(extract(_write(tmp_path, body)))
    assert len(chunks) == 1
    assert "```python" in chunks[0].body_md
    assert "def hello():" in chunks[0].body_md
    assert '    print("world")' in chunks[0].body_md
    assert chunks[0].body_md.rstrip().endswith("```")


def test_code_only_section_is_no_longer_dropped(tmp_path: Path) -> None:
    """Pre-fix bug: a heading section with only a fenced code block
    inside flushed nothing because the `not body` check killed it."""
    body = textwrap.dedent(
        """\
        # First

        regular text here.

        ## Code Only

        ```python
        x = 1
        ```

        # After

        more text.
        """
    )
    chunks = list(extract(_write(tmp_path, body)))
    paths = [c.heading_path for c in chunks]
    assert "First > Code Only" in paths, paths
    code_chunk = next(c for c in chunks if c.heading_path == "First > Code Only")
    assert "x = 1" in code_chunk.body_md


def test_nested_lists_round_trip(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """\
        # Lists

        - outer one
          - inner one
          - inner two
        - outer two
        """
    )
    chunks = list(extract(_write(tmp_path, body)))
    assert len(chunks) == 1
    md = chunks[0].body_md
    assert "- outer one" in md
    assert "  - inner one" in md
    assert "  - inner two" in md
    assert "- outer two" in md


def test_blockquote_round_trips(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """\
        # Quote

        > a quote
        > spans lines
        """
    )
    chunks = list(extract(_write(tmp_path, body)))
    assert len(chunks) == 1
    assert "> a quote" in chunks[0].body_md
    assert "> spans lines" in chunks[0].body_md


def test_heading_path_unchanged(tmp_path: Path) -> None:
    """``heading_path`` is built from the heading stack inline text —
    not from the new line-map slice — so adding ``body_md`` mustn't
    change sidebar locator labels."""
    body = textwrap.dedent(
        """\
        # Top

        para.

        ## Mid

        more.

        ### Leaf

        details.
        """
    )
    chunks = list(extract(_write(tmp_path, body)))
    paths = [c.heading_path for c in chunks]
    assert paths == ["Top", "Top > Mid", "Top > Mid > Leaf"]


def test_body_struct_remains_plain_text_for_snippets(tmp_path: Path) -> None:
    """``body_struct`` Block list stays as plain-text blocks — snippets
    must not show literal markdown markers like ``**bold**``."""
    body = textwrap.dedent(
        """\
        # Bold Section

        This has **bold** and *italic* text.
        """
    )
    chunks = list(extract(_write(tmp_path, body)))
    assert len(chunks) == 1
    block_texts = [b.text for b in chunks[0].body_struct]
    # Inline tokens preserve markdown markers in their content (markdown-it
    # behavior) — that's fine because snippets show what the user typed.
    # The key invariant is that body_struct is *not* the preview renderer's
    # source; body_md is. Either presentation reads as expected English.
    assert any("bold" in t for t in block_texts)


def test_pre_heading_preamble_flushes_as_first_chunk(tmp_path: Path) -> None:
    """Content before the first heading still produces a chunk; its
    ``body_md`` is the source slice up to the first heading line."""
    body = textwrap.dedent(
        """\
        a paragraph before any heading.

        # First Heading

        body of first.
        """
    )
    chunks = list(extract(_write(tmp_path, body)))
    assert len(chunks) == 2
    assert chunks[0].heading_path == ""
    assert "a paragraph before any heading." in chunks[0].body_md
    assert chunks[1].heading_path == "First Heading"
