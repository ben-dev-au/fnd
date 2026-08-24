"""The scroll anchors on the matching ROW inside a tall block, not its top.

``scroll_to_region`` pins a widget's first row, so a match a hundred rows into a
long block landed off the bottom of the viewport: the scroll reported success
while the user saw no highlight at all. The row count itself lives in
:mod:`fnd.tui.preview.match_row`; these are the strategy's own edges.
"""

from __future__ import annotations

from typing import Any, cast

from textual.geometry import Region
from textual.widget import Widget

from fnd.matching import MatchSpec
from fnd.tui.preview_scroll import StructuralScrollStrategy


class _Host:
    """Just the slice of StructuralHost that ``_match_line_offset`` reads."""

    def __init__(self, spec: MatchSpec) -> None:
        self._spec = spec

    def effective_match_spec(self) -> MatchSpec:
        return self._spec


class _Fence:
    """A MarkdownFence keeps its text on ``.code``, not ``._content``."""

    def __init__(self, code: str, height: int, width: int = 200) -> None:
        self.code = code
        self.region = Region(0, 0, width, height)
        self.content_region = Region(0, 0, width, height)


class _Block:
    def __init__(self, plain: str, height: int, width: int = 200) -> None:
        class _Content:
            def __init__(self, p: str) -> None:
                self.plain = p

        self._content = _Content(plain)
        self.region = Region(0, 0, width, height)
        self.content_region = Region(0, 0, width, height)


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


def test_wrapped_prose_anchors_on_the_row_it_wraps_onto() -> None:
    """One source line, many rendered rows — the case a newline count misses."""
    words = [f"word{i:03d}" for i in range(300)]
    words[120] = "glimmer"
    block = cast(Widget, _Block(" ".join(words), 100, width=30))

    assert _strategy("glimmer")._match_line_offset(block) == 40


def test_no_offset_for_short_wrapped_prose() -> None:
    block = cast(Widget, _Block("a paragraph mentioning glimmer part way through", 1))

    assert _strategy("glimmer")._match_line_offset(block) == 0


def test_offset_never_exceeds_the_widget() -> None:
    """A height no model reproduces falls back to the block's top."""
    fence = _long_fence(87)
    fence.region = Region(0, 0, 200, 40)
    fence.content_region = Region(0, 0, 200, 40)

    assert _strategy("glimmer")._match_line_offset(fence) == 0


def test_empty_spec_never_offsets() -> None:
    strategy = StructuralScrollStrategy(_Host(MatchSpec()))  # type: ignore[arg-type]

    assert strategy._match_line_offset(_long_fence(87)) == 0


def test_a_frozen_chunk_answers_from_its_captured_row() -> None:
    view = _Block("", 84)
    view.fnd_first_match_row = 43  # type: ignore[attr-defined]

    assert _strategy("glimmer")._match_line_offset(cast(Widget, view)) == 43
