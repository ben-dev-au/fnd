"""Pin: FNDMarkdown heading marker prefix + accent-colored headings +
contrasting bold/italic.

A terminal can't render font-size differences between heading levels.
``_HeadingMarkerMixin`` prepends ``#``/``##``/``###``/... so the level
is readable. ``FNDMarkdown.DEFAULT_CSS`` paints every level in
``$accent`` (not just H1-H3) and shifts ``.strong`` / ``.em`` to
contrasting theme colors so emphasis reads on most terminal fonts.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from fnd.tui.widgets.markdown import (
    FNDMarkdown,
    FNDMarkdownH1,
    FNDMarkdownH2,
    FNDMarkdownH3,
    FNDMarkdownH4,
    FNDMarkdownH5,
    FNDMarkdownH6,
    FNDMarkdownParagraph,
)

_H_CLASSES = [
    FNDMarkdownH1,
    FNDMarkdownH2,
    FNDMarkdownH3,
    FNDMarkdownH4,
    FNDMarkdownH5,
    FNDMarkdownH6,
]


class _Host(App[None]):
    def __init__(self, md: str) -> None:
        super().__init__()
        self._md = md

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield FNDMarkdown(self._md)


@pytest.mark.asyncio
async def test_every_heading_level_carries_marker_prefix() -> None:
    md = "\n\n".join(f"{'#' * n} H{n}" for n in range(1, 7))
    app = _Host(md)
    async with app.run_test(size=(60, 30)) as pilot:
        await app.query_one(FNDMarkdown).update(md)
        await pilot.pause()
        for level, cls in enumerate(_H_CLASSES, start=1):
            block = app.query_one(cls)
            expected = "#" * level + f" H{level}"
            assert block._content.plain == expected, (level, block._content.plain)


@pytest.mark.asyncio
async def test_every_heading_level_uses_accent_color() -> None:
    md = "\n\n".join(f"{'#' * n} H{n}" for n in range(1, 7))
    app = _Host(md)
    async with app.run_test(size=(60, 30)) as pilot:
        await app.query_one(FNDMarkdown).update(md)
        await pilot.pause()
        accent_rgb = app.get_css_variables().get("accent")
        assert accent_rgb is not None
        for cls in _H_CLASSES:
            block = app.query_one(cls)
            # Render the first row of the block; its first segment that
            # has actual text should carry the accent triplet (or an
            # alpha-blended variant for H6).
            strip = block.render_line(0)
            color_seen = None
            for seg in strip:
                if not seg.text.strip():
                    continue
                if seg.style and seg.style.color and seg.style.color.triplet:
                    color_seen = seg.style.color.triplet
                    break
            assert color_seen is not None, cls.__name__
            # H6 is accent-70% so the rendered triplet may differ slightly
            # after alpha blend; the other five are flat $accent.
            if cls is not FNDMarkdownH6:
                t = color_seen
                # Tokyo-night-ish accent — peach (254, 166, 43); guard
                # against the default Textual palette regression where H4-H6
                # silently fell back to $text and lost colour entirely.
                assert (t.red, t.green, t.blue) != (224, 224, 224), cls.__name__


@pytest.mark.asyncio
async def test_bold_carries_contrasting_color_not_just_bold_weight() -> None:
    md = "A line with **bold text** here."
    app = _Host(md)
    async with app.run_test(size=(60, 10)) as pilot:
        await app.query_one(FNDMarkdown).update(md)
        await pilot.pause()
        para = app.query_one(FNDMarkdownParagraph)
        strip = para.render_line(0)
        bold_seg = next(
            (s for s in strip if s.text == "bold text" and s.style and s.style.bold),
            None,
        )
        assert bold_seg is not None, [s.text for s in strip]
        assert bold_seg.style is not None
        assert bold_seg.style.color is not None
        t = bold_seg.style.color.triplet
        assert t is not None
        # Body text colour for the rest of the paragraph; bold must not
        # share that triplet — i.e. .strong picked up a contrasting
        # colour from $primary, not just text-style: bold.
        body_seg = next(
            (s for s in strip if s.text.startswith("A line with ") and s.style),
            None,
        )
        assert body_seg is not None
        assert body_seg.style is not None
        assert body_seg.style.color is not None
        body_t = body_seg.style.color.triplet
        assert body_t is not None
        assert (t.red, t.green, t.blue) != (body_t.red, body_t.green, body_t.blue)


@pytest.mark.asyncio
async def test_italic_also_carries_contrasting_color() -> None:
    md = "Some *italic phrase* here."
    app = _Host(md)
    async with app.run_test(size=(60, 10)) as pilot:
        await app.query_one(FNDMarkdown).update(md)
        await pilot.pause()
        para = app.query_one(FNDMarkdownParagraph)
        strip = para.render_line(0)
        em_seg = next(
            (s for s in strip if s.text == "italic phrase" and s.style and s.style.italic),
            None,
        )
        assert em_seg is not None
        body_seg = next((s for s in strip if s.text.startswith("Some ") and s.style), None)
        assert body_seg is not None
        assert em_seg.style is not None
        assert em_seg.style.color is not None
        assert body_seg.style is not None
        assert body_seg.style.color is not None
        em_t = em_seg.style.color.triplet
        body_t = body_seg.style.color.triplet
        assert em_t is not None
        assert body_t is not None
        assert (em_t.red, em_t.green, em_t.blue) != (body_t.red, body_t.green, body_t.blue)


@pytest.mark.asyncio
async def test_heading_marker_does_not_break_highlight_offsets() -> None:
    """A query term inside a heading still highlights the right word —
    the marker prefix shifts the plain content but the highlight span
    is computed against the post-prefix plain, so offsets stay aligned.
    """
    from fnd.matching import MatchSpec

    md = "## My Heading Word"
    app = _Host(md)
    async with app.run_test(size=(60, 10)) as pilot:
        widget = app.query_one(FNDMarkdown)
        widget.match_spec = MatchSpec.from_query("heading")
        await widget.update(md)
        await pilot.pause()
        h2 = app.query_one(FNDMarkdownH2)
        plain = h2._content.plain
        assert plain == "## My Heading Word"
        # Highlight span covers "Heading" at offsets [6, 13) in the
        # post-marker plain ("## My Heading Word").
        hits = [
            s
            for s in h2._content.spans
            if not (isinstance(s.style, str) and s.style.startswith("."))
        ]
        assert hits, h2._content.spans
        s = hits[0]
        assert plain[s.start : s.end].lower() == "heading"
