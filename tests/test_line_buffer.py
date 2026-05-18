"""Vacuum tests for the flat-buffer preview widget.

The widget replaces ~250,000 ``Static`` widgets (one per line of a
1000-page PDF) with a single ``ScrollView`` subclass that owns a
``list[Strip]``. These tests exercise the widget in isolation —
no FNDApp coupling, no real Tantivy index — so they can pin down
the data model + rendering contract before the host app starts
calling it.

Each test maps to one of the features the production preview pane
currently relies on. Together they make the audit explicit: a
behaviour that isn't tested here is a behaviour the widget can't
yet replace.
"""

from __future__ import annotations

import pytest

from fnd.tui.line_buffer import (
    FileView,
    LineBufferPreview,
    RenderedDocument,
    build_file_view,
    build_rendered_document,
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
async def test_widget_focused_chunk_overlay_is_per_render() -> None:
    """``set_focused_chunk`` doesn't rebuild Strips — focused-chunk
    accent is applied at render time via the component class. The
    cached Strip array stays identical across focus changes (cheap to
    flip back and forth), and the row overlay style is non-None only
    for visual rows inside the focused chunk's range."""
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
        # No rebuild: every Strip identity is preserved.
        for li in range(len(buf._strips)):
            assert (
                buf._strips[li] is before[li]
            ), f"focus change must not rebuild Strip at line {li}"
        # Row-overlay resolution flags the focused chunk's visual rows.
        chunk2_range = fv.chunk_to_range[2]
        for vy in range(len(buf._strips)):
            logical = buf._visual_to_logical[vy]
            overlay = buf._row_overlay_style(vy)
            if chunk2_range[0] <= logical < chunk2_range[1]:
                assert (
                    overlay is not None
                ), f"focused chunk row vy={vy} (logical={logical}) should have overlay"
            elif logical not in fv.match_lines:
                assert (
                    overlay is None
                ), f"non-focused, non-match row vy={vy} should not have overlay"


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
    assert fv.structural_map == []


# ── Stage 1 (RenderedDocument / structural_map) ────────────────────


def test_build_file_view_emits_structural_map_one_chunk() -> None:
    """One block per chunk, ordered by line_start; ranges match chunk_to_range."""
    fv = build_file_view(
        [
            _chunk(10, "alpha\nbravo\ncharlie"),
            _chunk(11, "delta\necho"),
        ]
    )
    assert fv.structural_map == [
        (0, 3, "chunk", 10),
        (3, 6, "chunk", 11),
    ]
    for start, end, _, payload in fv.structural_map:
        assert isinstance(payload, int)
        assert fv.chunk_to_range[payload] == (start, end)


def test_build_md_file_view_emits_structural_map() -> None:
    """Markdown flat-renderer agrees with the structural_map contract."""
    from fnd.extract.base import Block
    from fnd.matching import MatchSpec
    from fnd.query import FileChunk
    from fnd.tui._md_flat import build_md_file_view

    def _md_chunk(seq: int, md: str) -> FileChunk:
        return FileChunk(
            parent_id="doc",
            path="/doc.md",
            kind="md",
            page=0,
            slide=0,
            heading_path="",
            chunk_seq=seq,
            blocks=[Block(kind="p", text=md)],
            body_md=md,
        )

    fv = build_md_file_view(
        [_md_chunk(0, "# Heading\n\nbody"), _md_chunk(1, "second")],
        spec=MatchSpec(),
        wrap_width=40,
    )
    assert [payload for *_, payload in fv.structural_map] == [0, 1]
    for start, end, kind, payload in fv.structural_map:
        assert kind == "chunk"
        assert isinstance(payload, int)
        assert fv.chunk_to_range[payload] == (start, end)


def test_rendered_document_packs_strips_and_indexes() -> None:
    """build_rendered_document is the prefetch-bundle constructor —
    strips and per-row indexes must agree with what set_prebuilt_view
    consumes."""
    fv = build_file_view([_chunk(0, "alpha\nbravo\ncharlie")])
    doc = build_rendered_document(fv, wrap_width=0)
    assert doc.fv is fv
    assert len(doc.strips) == len(doc.visual_to_logical) == fv.line_count
    assert len(doc.logical_to_visual_start) == fv.line_count
    assert doc.wrap_width == 0
    assert doc.base_width == max(fv.widest_line, 1)
    assert doc.match_lines is fv.match_lines
    assert doc.structural_map is fv.structural_map


def test_rendered_document_wraps_long_lines() -> None:
    """Wrap > 0 produces more visual rows than logical lines for a
    line wider than the wrap width."""
    fv = build_file_view([_chunk(0, "x" * 100 + "\nshort")])
    doc = build_rendered_document(fv, wrap_width=20)
    assert len(doc.strips) > fv.line_count
    assert doc.wrap_width == 20
    assert doc.base_width == 1
    # logical -> visual start is monotonic and covers every logical line.
    assert doc.logical_to_visual_start == sorted(doc.logical_to_visual_start)


@pytest.mark.asyncio
async def test_set_prebuilt_view_consumes_rendered_document() -> None:
    """A widget installed with the RenderedDocument's fields paints the
    expected line count — the value type is a faithful prebuilt bundle."""
    from textual.app import App, ComposeResult

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield LineBufferPreview(id="buf", wrap=False)

    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        buf = app.query_one(LineBufferPreview)
        fv = build_file_view([_chunk(0, "\n".join(f"row-{i}" for i in range(40)))])
        doc = build_rendered_document(fv, wrap_width=0)
        buf.set_prebuilt_view(
            doc.fv,
            doc.strips,
            doc.visual_to_logical,
            doc.logical_to_visual_start,
            wrap_width=doc.wrap_width,
            base_width=doc.base_width,
        )
        await pilot.pause()
        assert buf.virtual_size.height == len(doc.strips)


def test_rendered_document_defaults_empty() -> None:
    """An empty RenderedDocument is safe to construct + has consistent indexes."""
    doc = RenderedDocument(fv=FileView())
    assert doc.strips == []
    assert doc.visual_to_logical == []
    assert doc.logical_to_visual_start == []
    assert doc.base_width == 1
    assert doc.match_lines == set()
    assert doc.structural_map == []


# ── Wrap mode (PDF long-line story) ─────────────────────────────────


@pytest.mark.asyncio
async def test_widget_wrap_mode_splits_long_lines_to_visual_rows() -> None:
    """When wrap is enabled and a logical line is wider than the
    viewport, the widget reports more visual rows than logical lines
    and projects each logical row onto its first visual row."""
    from textual.app import App, ComposeResult

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield LineBufferPreview(id="buf", wrap=True)

    app = _Host()
    async with app.run_test(size=(20, 5)) as pilot:
        await pilot.pause()
        buf = app.query_one(LineBufferPreview)
        # Tall enough that there's content past the 5-row viewport so
        # scroll_to_line can actually move (otherwise the scroll clamps
        # to 0 because everything fits).
        long = "x" * 200
        padding = "\n".join(f"pad-{i}" for i in range(20))
        fv = build_file_view([_chunk(0, long + "\nshort\n" + padding)])
        buf.set_file_view(fv)
        await pilot.pause()
        # Visual count must exceed logical (long line wraps into several
        # visual rows; "short" + padding stay one row each).
        assert buf.visual_line_count > fv.line_count
        # The logical "short" line still lives at its own visual y;
        # scrolling to it lands the viewport at that visual row.
        buf.scroll_to_line(1)
        await pilot.pause()
        assert int(buf.scroll_offset.y) == buf._logical_to_visual_y(1)


@pytest.mark.asyncio
async def test_widget_wrap_mode_match_lines_project_to_visual_rows() -> None:
    """In wrap mode ``match_lines`` returns the visual row indices that
    contain a match — not the logical line indices. The scrollbar's
    line-precise math expects visual offsets so this is the right
    interpretation."""
    from textual.app import App, ComposeResult

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield LineBufferPreview(id="buf", wrap=True)

    app = _Host()
    async with app.run_test(size=(20, 10)) as pilot:
        await pilot.pause()
        buf = app.query_one(LineBufferPreview)
        # Make a chunk whose first logical line is long enough to wrap
        # AND a later logical line contains the match.
        long_prefix = "x" * 100
        body = long_prefix + "\napple match here"
        # "apple" starts at offset len(long_prefix) + 1.
        match_start = len(long_prefix) + 1
        fv = build_file_view([_chunk(5, body, (match_start, match_start + 5))])
        buf.set_file_view(fv)
        await pilot.pause()
        # The logical match line is 1 — but in wrap mode the visual
        # row index is past the wrap of line 0.
        assert buf.match_lines == [buf._logical_to_visual_y(1)]


@pytest.mark.asyncio
async def test_widget_set_wrap_toggles_layout() -> None:
    """``set_wrap`` flips between modes without losing the FileView,
    and re-renders the cached Strips so the viewport reflects the new
    layout on the next paint."""
    from textual.app import App, ComposeResult

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield LineBufferPreview(id="buf", wrap=False)

    app = _Host()
    async with app.run_test(size=(20, 10)) as pilot:
        await pilot.pause()
        buf = app.query_one(LineBufferPreview)
        long = "x" * 100
        fv = build_file_view([_chunk(0, long + "\nshort")])
        buf.set_file_view(fv)
        await pilot.pause()
        unwrapped_visual_count = buf.visual_line_count
        assert unwrapped_visual_count == fv.line_count
        buf.set_wrap(True)
        await pilot.pause()
        assert buf.visual_line_count > unwrapped_visual_count
        buf.set_wrap(False)
        await pilot.pause()
        assert buf.visual_line_count == fv.line_count


@pytest.mark.asyncio
async def test_widget_wrap_mode_get_selection_uses_visual_rows() -> None:
    """Drag-select across wrapped visual rows extracts the wrapped
    plain text — not the logical-line-indexed text the no-wrap path
    used to assume."""
    from textual.app import App, ComposeResult

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield LineBufferPreview(id="buf", wrap=True)

    app = _Host()
    async with app.run_test(size=(20, 10)) as pilot:
        await pilot.pause()
        buf = app.query_one(LineBufferPreview)
        long = "abc" * 20  # 60 chars; wraps at ~18-19 cells in a 20-wide pane.
        fv = build_file_view([_chunk(0, long)])
        buf.set_file_view(fv)
        await pilot.pause()
        # A selection spanning two visual rows should produce text whose
        # first row's tail concatenates with the next visual row.
        from types import SimpleNamespace

        sel = SimpleNamespace(start=(0, 0), end=(1, 5))
        result = buf.get_selection(sel)  # type: ignore[arg-type]
        assert result is not None
        text, ending = result
        # The first visual row's full text is the first chunk of the
        # wrap; the second row contributes its first 5 chars. The exact
        # split depends on Rich's wrap algorithm, so we assert the
        # extracted text starts at the buffer start and is longer than
        # one row alone.
        assert text.startswith("abc")
        assert ending == "\n"
        first_row_text = buf._strips[0].text
        assert len(text) > len(first_row_text)
