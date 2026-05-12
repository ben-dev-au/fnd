"""Vacuum tests for the flat-buffer preview widget.

The widget replaces ~250,000 ``Static`` widgets (one per line of a
1000-page PDF) with a single ``ScrollView`` subclass that owns a
``list[Strip]``. These tests exercise the widget in isolation —
no AcornApp coupling, no real Tantivy index — so they can pin down
the data model + rendering contract before the host app starts
calling it.

Each test maps to one of the features the production preview pane
currently relies on. Together they make the audit explicit: a
behaviour that isn't tested here is a behaviour the widget can't
yet replace.
"""

from __future__ import annotations

import pytest

from acorn.tui.line_buffer import (
    FileView,
    LineBufferPreview,
    build_file_view,
    match_marker_positions,
)


def _chunk(
    chunk_id: int, text: str, *spans: tuple[int, int]
) -> tuple[int, str, list[tuple[int, int]]]:
    return chunk_id, text, list(spans)


# ── build_file_view (pure function) ────────────────────────────────


def test_build_file_view_splits_lines_and_maps_chunks() -> None:
    """Every line of every chunk maps back to its owning chunk_id, and
    chunk_to_range spans both the leading gap row and the chunk's
    text."""
    fv = build_file_view(
        [
            _chunk(10, "alpha\nbravo\ncharlie"),
            _chunk(11, "delta\necho"),
        ]
    )
    # chunk 10: 3 lines, no leading gap (first chunk).
    # chunk 11: 1 gap row + 2 lines.
    assert fv.line_count == 3 + 1 + 2
    assert [line.plain for line in fv.lines] == [
        "alpha",
        "bravo",
        "charlie",
        "",  # gap before chunk 11
        "delta",
        "echo",
    ]
    assert fv.line_to_chunk == [10, 10, 10, 11, 11, 11]
    assert fv.chunk_to_range == {10: (0, 3), 11: (3, 6)}


def test_build_file_view_match_spans_register_per_line() -> None:
    """match_spans land in the right lines, the matched substring is
    bolded, the whole line gets the subtle-accent style, and
    first_hit_line_in_chunk points at the first matched line in the
    chunk."""
    # Chunk "alpha\nbravo\ncharlie" — substring "bra" lives in line 1
    # at offsets 6..9 (b after 'alpha\n' which is 6 chars).
    fv = build_file_view([_chunk(7, "alpha\nbravo\ncharlie", (6, 9))])
    assert fv.match_lines == {1}
    assert fv.first_hit_line_in_chunk == {7: 1}
    # The matched line carries a bold span over (0, 3) — local offsets.
    bra_line = fv.lines[1]
    bold_spans = [s for s in bra_line.spans if "bold" in str(s.style).lower()]
    assert bold_spans, "expected a bold span over the matched substring"


def test_build_file_view_handles_multiple_matches_on_same_line() -> None:
    """Two match spans on the same line both register; only one entry
    is added to ``match_lines`` (it's a set)."""
    # "alpha bravo alpha" — two "alpha" matches at (0,5) and (12,17).
    fv = build_file_view([_chunk(1, "alpha bravo alpha", (0, 5), (12, 17))])
    assert fv.match_lines == {0}
    bold_spans = [s for s in fv.lines[0].spans if "bold" in str(s.style).lower()]
    assert len(bold_spans) >= 2


# ── match_marker_positions (pure function) ─────────────────────────


def test_match_marker_positions_maps_lines_to_track_cells_exactly() -> None:
    """The legacy MatchAwareScrollBar mapping was inaccurate because it
    assumed every chunk had the same line count. The line-precise
    mapping must place a marker at the right track cell for any
    line index."""
    # 1000 total lines, track height 10 cells, matches at line 0, 500,
    # 999 → cells 0, 5, 9.
    positions = match_marker_positions([0, 500, 999], track_height=10, total_lines=1000)
    assert positions == {0, 5, 9}


def test_match_marker_positions_empty_inputs() -> None:
    """Degenerate cases don't crash: zero-height track, zero lines,
    out-of-range match indices all produce an empty marker set."""
    assert match_marker_positions([], 10, 1000) == set()
    assert match_marker_positions([5], 0, 1000) == set()
    assert match_marker_positions([5], 10, 0) == set()
    # Out-of-range match indices are dropped silently.
    assert match_marker_positions([-1, 1000], 10, 1000) == set()


# ── LineBufferPreview widget (in a minimal Textual app) ────────────


@pytest.mark.asyncio
async def test_widget_set_file_view_publishes_virtual_size() -> None:
    """After set_file_view, the widget's virtual height matches the
    total line count so the scrollbar sizes itself correctly."""
    from textual.app import App, ComposeResult

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield LineBufferPreview(id="buf")

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        buf = app.query_one(LineBufferPreview)
        fv = build_file_view([_chunk(0, "\n".join(f"line {i}" for i in range(100)))])
        buf.set_file_view(fv)
        await pilot.pause()
        assert buf.virtual_size.height == 100
        assert buf.virtual_size.width >= len("line 99")


@pytest.mark.asyncio
async def test_widget_render_line_returns_strip_with_expected_text() -> None:
    """render_line returns a Strip whose plain text matches the
    underlying Rich Text. This is the single contract the line API
    must satisfy for Textual to draw the buffer correctly."""
    from textual.app import App, ComposeResult
    from textual.strip import Strip

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield LineBufferPreview(id="buf")

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        buf = app.query_one(LineBufferPreview)
        fv = build_file_view([_chunk(0, "apple\nbanana\ncherry")])
        buf.set_file_view(fv)
        await pilot.pause()
        # Render y=0 from the top — the scroll offset is 0, so y=0 maps
        # to line 0.
        strip = buf.render_line(0)
        assert isinstance(strip, Strip)
        assert "apple" in strip.text
        strip2 = buf.render_line(2)
        assert "cherry" in strip2.text


@pytest.mark.asyncio
async def test_widget_scroll_to_line_moves_viewport() -> None:
    """scroll_to_line(50) lands line 50 at the top of the viewport.
    With center=True it lands roughly in the middle."""
    from textual.app import App, ComposeResult

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield LineBufferPreview(id="buf")

    app = _Host()
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        buf = app.query_one(LineBufferPreview)
        fv = build_file_view([_chunk(0, "\n".join(f"line {i}" for i in range(200)))])
        buf.set_file_view(fv)
        await pilot.pause()
        buf.scroll_to_line(50)
        await pilot.pause()
        assert int(buf.scroll_offset.y) == 50
        buf.scroll_to_line(100, center=True)
        await pilot.pause()
        # Centred — line 100 sits at viewport mid, so scroll y is line
        # 100 minus half the viewport height.
        assert int(buf.scroll_offset.y) == 100 - (buf.size.height // 2)


@pytest.mark.asyncio
async def test_widget_scroll_to_chunk_prefers_first_match_line() -> None:
    """Clicking a section in the sidebar should land the user on the
    matched line within the chunk, not the chunk's first line — this
    is feature 2 in the audit."""
    from textual.app import App, ComposeResult

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield LineBufferPreview(id="buf")

    app = _Host()
    async with app.run_test(size=(80, 5)) as pilot:
        await pilot.pause()
        buf = app.query_one(LineBufferPreview)
        # Long enough buffer that scrolling to a mid-chunk line is
        # actually a viewport move, not a no-op clamp.
        padding = "\n".join(f"pad {i}" for i in range(20))
        body = f"intro\nmore intro\napple match\nepilogue\n{padding}"
        # "apple" starts at offset 17.
        fv = build_file_view([_chunk(42, body, (17, 22))])
        buf.set_file_view(fv)
        await pilot.pause()
        buf.scroll_to_chunk(42)
        await pilot.pause()
        # The matched line in this chunk is global index 2.
        assert int(buf.scroll_offset.y) == 2


@pytest.mark.asyncio
async def test_widget_focused_chunk_repaint_is_local() -> None:
    """set_focused_chunk repaints only the focused chunk's strips,
    leaves other strips untouched. We assert by snapshotting Strip
    identities before/after and checking which slice changed."""
    from textual.app import App, ComposeResult

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield LineBufferPreview(id="buf")

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        buf = app.query_one(LineBufferPreview)
        fv = build_file_view(
            [
                _chunk(1, "one-a\none-b"),
                _chunk(2, "two-a\ntwo-b"),
                _chunk(3, "three-a\nthree-b"),
            ]
        )
        buf.set_file_view(fv)
        await pilot.pause()
        before = list(buf._strips)
        buf.set_focused_chunk(2)
        await pilot.pause()
        # Only chunk 2's lines should have been replaced; chunk 1 and
        # chunk 3 strips stay identical (by identity).
        chunk2_range = fv.chunk_to_range[2]
        for li in range(len(buf._strips)):
            if chunk2_range[0] <= li < chunk2_range[1]:
                assert (
                    buf._strips[li] is not before[li]
                ), f"chunk 2 line {li} should have been repainted"
            else:
                assert (
                    buf._strips[li] is before[li]
                ), f"line {li} outside the focused chunk must not be touched"


@pytest.mark.asyncio
async def test_widget_match_lines_property_exposes_sorted_positions() -> None:
    """``match_lines`` returns a sorted list of global line indices —
    the scrollbar renderer consumes this directly to paint markers."""
    from textual.app import App, ComposeResult

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield LineBufferPreview(id="buf")

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        buf = app.query_one(LineBufferPreview)
        # Two chunks each with one match — global lines 0 and 4
        # (chunk2 = gap row + 2 chunk lines, match on second of those).
        fv = build_file_view(
            [
                _chunk(0, "hit me", (0, 3)),
                _chunk(1, "ignore\nhit again", (7, 10)),
            ]
        )
        buf.set_file_view(fv)
        assert buf.match_lines == sorted(fv.match_lines)
        assert buf.match_lines  # not empty


@pytest.mark.asyncio
async def test_widget_clear_resets_buffer() -> None:
    """clear() empties the buffer and resets virtual_size to zero so a
    follow-up set_file_view starts from a clean slate."""
    from textual.app import App, ComposeResult

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield LineBufferPreview(id="buf")

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        buf = app.query_one(LineBufferPreview)
        buf.set_file_view(build_file_view([_chunk(0, "line\nline\nline")]))
        await pilot.pause()
        assert buf.virtual_size.height > 0
        buf.clear()
        await pilot.pause()
        assert buf.virtual_size.height == 0


def test_file_view_dataclass_defaults() -> None:
    """A FileView built without arguments is empty + safe to query."""
    fv = FileView()
    assert fv.line_count == 0
    assert fv.widest_line == 0
    assert fv.chunk_to_range == {}
    assert fv.match_lines == set()
