"""Query matches inside fenced code blocks are highlighted while the
lexer's syntax colouring is preserved underneath.

``_build_fence_widget`` renders the fence as a syntax-highlighted Rich
``Text`` with the match spans overlaid; the bare ``Syntax`` renderable
used previously discarded the computed spans.
"""

from __future__ import annotations

from rich.text import Text

from fnd.matching import MatchSpec
from fnd.render import HIGHLIGHT_STYLE
from fnd.tui._md_hybrid import _build_fence_widget


def _spec(query: str) -> MatchSpec:
    return MatchSpec.from_query(query)


def _rendered_text(md: str, query: str) -> tuple[Text, bool]:
    widget, has_match = _build_fence_widget(md, _spec(query))
    # Static stores its content under the name-mangled __content attr.
    rendered = widget._Static__content  # type: ignore[attr-defined]
    assert isinstance(rendered, Text)
    return rendered, has_match


def test_fence_match_highlighted_over_syntax_colours() -> None:
    rendered, has = _rendered_text("```python\ndef foo():\n    return needle\n```", "needle")
    assert has is True

    body = "def foo():\n    return needle"
    assert rendered.plain == body

    start = body.index("needle")
    hl = [s for s in rendered.spans if str(s.style) == HIGHLIGHT_STYLE]
    assert any(s.start == start and s.end == start + len("needle") for s in hl), (
        "expected a match highlight span over the fenced code"
    )
    # Lexer syntax spans must survive underneath the match overlay.
    assert any(str(s.style) != HIGHLIGHT_STYLE for s in rendered.spans), (
        "expected lexer syntax spans to be retained"
    )


def test_fence_without_match_keeps_syntax_only() -> None:
    rendered, has = _rendered_text("```python\ndef foo():\n    return bar\n```", "needle")
    assert has is False
    assert rendered.plain == "def foo():\n    return bar"
    assert not any(str(s.style) == HIGHLIGHT_STYLE for s in rendered.spans)
