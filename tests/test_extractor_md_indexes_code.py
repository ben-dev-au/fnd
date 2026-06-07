"""Markdown extractor indexes code-block content into the searchable ``body``.

Pre-fix, fenced (```` ``` ````) and indented code blocks arrived as
``fence`` / ``code_block`` tokens with no ``inline`` children, so their
text was flagged as "has content" (section not dropped) but never folded
into ``body``. The result: anything appearing only inside a code block
was silently unsearchable, even though it is plainly in the document.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from fnd.extract.markdown import extract


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "doc.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_fenced_code_content_is_in_body(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """\
        # Code Section

        ```python
        def unique_fence_func():
            return "secret_in_fence"
        ```
        """
    )
    chunks = list(extract(_write(tmp_path, body)))
    body_all = "\n".join(c.body for c in chunks)
    assert "unique_fence_func" in body_all
    assert "secret_in_fence" in body_all


def test_indented_code_content_is_in_body(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """\
        # Indented

        Some intro.

            indented_code_token = 42
        """
    )
    chunks = list(extract(_write(tmp_path, body)))
    body_all = "\n".join(c.body for c in chunks)
    assert "indented_code_token" in body_all


def test_code_only_section_is_searchable(tmp_path: Path) -> None:
    """A section whose only content is a code fence must still carry its
    code text in ``body`` (not just exist as an empty-bodied chunk)."""
    body = textwrap.dedent(
        """\
        # Top

        ## Only Code

        ```sh
        grep -r needle_in_code .
        ```
        """
    )
    chunks = list(extract(_write(tmp_path, body)))
    code_chunk = next(c for c in chunks if "Only Code" in c.heading_path)
    assert "needle_in_code" in code_chunk.body
