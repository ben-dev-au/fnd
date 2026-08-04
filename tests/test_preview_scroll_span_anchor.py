"""The scroll anchors on the matching LINE inside a tall block, not its top.

``scroll_to_region`` pins a widget's first row, so a match a hundred lines into
a long code fence landed off the bottom of the viewport: the scroll reported
success while the user saw no highlight at all. Counting newlines is exact for
blocks that render one source line per row (fences), which is where the problem
actually bites.
"""

from __future__ import annotations

from typing import Any, cast

from textual.widget import Widget

from fnd.matching import MatchSpec
from fnd.tui.preview_scroll import StructuralScrollStrategy


class _Host:
    """Just the slice of StructuralHost that ``_match_line_offset`` reads."""

    def __init__(self, spec: MatchSpec) -> None:
        self._spec = spec

    def effective_match_spec(self) -> MatchSpec:
        return self._spec


class _Region:
    def __init__(self, height: int) -> None:
        self.height = height


class _Fence:
    """A MarkdownFence keeps its text on ``.code``, not ``._content``."""

    def __init__(self, code: str, height: int) -> None:
        self.code = code
        self.region = _Region(height)


class _Block:
    def __init__(self, plain: str, height: int) -> None:
        class _Content:
            def __init__(self, p: str) -> None:
                self.plain = p

        self._content = _Content(plain)
        self.region = _Region(height)


def _strategy(query: str) -> Any:
    return StructuralScrollStrategy(_Host(MatchSpec.from_query(query)))  # type: ignore[arg-type]


def _long_fence(match_line: int, total: int = 120) -> Any:
    lines = [f"filler line {i}" for i in range(total)]
    lines[match_line] = "    result = glimmer(value)"
    return cast(Widget, _Fence("\n".join(lines), total))


def test_offset_lands_on_the_matching_line_deep_in_a_fence() -> None:
    assert _strategy("glimmer")._match_line_offset(_long_fence(87)) == 87


def test_first_match_wins_when_a_fence_has_several() -> None:
    fence = _long_fence(87)
    lines = fence.code.splitlines()
    lines[12] = "    glimmer_setup()"
    fence.code = "\n".join(lines)

    assert _strategy("glimmer")._match_line_offset(fence) == 12


def test_no_offset_for_a_match_on_the_first_line() -> None:
    assert _strategy("glimmer")._match_line_offset(_long_fence(0)) == 0


def test_no_offset_when_nothing_matches() -> None:
    assert _strategy("kubernetes")._match_line_offset(_long_fence(87)) == 0


def test_no_offset_for_wrapped_prose_without_newlines() -> None:
    """A paragraph is wrapped, not newline-delimited, so a newline count would
    be meaningless — and the block is short enough that its top is the match."""
    block = cast(Widget, _Block("a paragraph mentioning glimmer part way through the line", 3))

    assert _strategy("glimmer")._match_line_offset(block) == 0


def test_offset_never_exceeds_the_widget() -> None:
    """If newline count and rendered rows have diverged (wrapping), the widget
    top is the safer answer than an anchor past its own bottom."""
    fence = _long_fence(87)
    fence.region.height = 40  # rendered shorter than its source lines

    assert _strategy("glimmer")._match_line_offset(fence) == 0


def test_empty_spec_never_offsets() -> None:
    strategy = StructuralScrollStrategy(_Host(MatchSpec()))  # type: ignore[arg-type]

    assert strategy._match_line_offset(_long_fence(87)) == 0
