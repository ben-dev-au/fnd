"""Mermaid fences render as diagrams on the live FNDMarkdown path.

A ```mermaid fence renders as termaid text-art when the parent markdown
carries ``render_mermaid=True``; it falls back to syntax-highlighted source
when the flag is off, when the diagram is unsupported/garbage, and is inert
for non-mermaid fences. A query match inside a rendered diagram still
registers the fence as the scroll anchor (no painted span on diagram art).
"""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App, ComposeResult

from fnd.matching import MatchSpec
from fnd.tui import mermaid_render
from fnd.tui.widgets.markdown import _MERMAID_RENDERER, FNDMarkdown, FNDMarkdownFence

FLOW = "```mermaid\nflowchart TD\n    A[Start] --> B[End]\n```"


class _Host(App[None]):
    def __init__(self, md: FNDMarkdown) -> None:
        self.md = md
        super().__init__()

    def compose(self) -> ComposeResult:
        yield self.md


async def _fence_plain(
    source: str,
    *,
    render_mermaid: bool = False,
    match_spec: MatchSpec | None = None,
) -> str:
    """Rendered text of the first fence, captured while the app is live."""
    md = FNDMarkdown(source, render_mermaid=render_mermaid, match_spec=match_spec)
    async with _Host(md).run_test():
        await md.build_done.wait()
        fences = list(md.query(FNDMarkdownFence))
        assert fences, "expected a code fence"
        return fences[0]._highlighted_code.plain


@pytest.mark.asyncio
async def test_flag_on_renders_diagram() -> None:
    plain = await _fence_plain(FLOW, render_mermaid=True)
    # Box-drawing art present; the 'flowchart' keyword from source is gone.
    assert "┌" in plain
    assert "flowchart" not in plain
    assert "Start" in plain


@pytest.mark.asyncio
async def test_flag_off_renders_source() -> None:
    plain = await _fence_plain(FLOW, render_mermaid=False)
    assert "flowchart" in plain
    assert "┌" not in plain


@pytest.mark.asyncio
async def test_unsupported_diagram_falls_back_to_source() -> None:
    src = "```mermaid\nthis is not a real diagram\n```"
    plain = await _fence_plain(src, render_mermaid=True)
    assert "this is not a real diagram" in plain


@pytest.mark.asyncio
async def test_non_mermaid_fence_unchanged_when_flag_on() -> None:
    src = "```python\ndef hit():\n    return needle\n```"
    plain = await _fence_plain(src, render_mermaid=True)
    assert "def hit" in plain


@pytest.mark.asyncio
async def test_wide_diagram_renders_with_horizontal_scroll_not_source() -> None:
    # A wide diagram keeps its width (no wrap), tagged for the thin
    # horizontal scrollbar, instead of clipping to blank or showing source.
    wide = (
        "```mermaid\nflowchart LR\n"
        + "\n".join(f"    A{i} --> A{i + 1}" for i in range(12))
        + "\n```"
    )
    md = FNDMarkdown(wide, render_mermaid=True)
    async with _Host(md).run_test(size=(60, 24)) as pilot:
        await md.build_done.wait()
        await pilot.pause()
        fence = next(iter(md.query(FNDMarkdownFence)))
        assert "┌" in fence._highlighted_code.plain  # diagram, not source
        assert fence.has_class("mermaid-diagram")
        # wider than the 60-col pane => horizontally scrollable
        assert fence.virtual_size.width > fence.size.width


@pytest.mark.asyncio
async def test_match_inside_diagram_registers_anchor() -> None:
    src = "```mermaid\nflowchart TD\n    A[Needle] --> B[End]\n```"
    md = FNDMarkdown(src, render_mermaid=True, match_spec=MatchSpec.from_query("needle"))
    async with _Host(md).run_test():
        await md.build_done.wait()
        fence = next(iter(md.query(FNDMarkdownFence)))
        assert md.first_match_block is fence


@pytest.mark.asyncio
async def test_a_runaway_diagram_leaves_the_app_responsive_and_shows_source() -> None:
    """termaid never returns on this label; the loop keeps ticking, then source shows."""
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    ticker = asyncio.create_task(tick())
    src = '```mermaid\nflowchart TD\n  A["`a*`"] --> B\n```'
    md = FNDMarkdown(src, render_mermaid=True)
    try:
        async with _Host(md).run_test():
            await md.build_done.wait()
            fence = next(iter(md.query(FNDMarkdownFence)))
            assert "flowchart" in fence._highlighted_code.plain
            assert not fence.has_class("mermaid-diagram")
    finally:
        ticker.cancel()
    # The build spans the time limit; a loop blocked by the render would not tick.
    assert ticks > mermaid_render.RENDER_TIMEOUT_S / 0.05 / 2


@pytest.mark.asyncio
async def test_a_newer_update_wins_over_a_pending_diagram() -> None:
    """A re-update while a diagram renders must not let the older body land."""
    md = FNDMarkdown(FLOW, render_mermaid=True)
    async with _Host(md).run_test() as pilot:
        md.update('```mermaid\nflowchart TD\n  %% superseded\n  A["`a*`"] --> B\n```')
        await md.update("plain *text* only")
        await pilot.pause(0.05)
        await md.build_done.wait()
        superseded = '```mermaid\nflowchart TD\n  %% superseded\n  A["`a*`"] --> B\n```'
        code = superseded.removeprefix("```mermaid\n").removesuffix("\n```")
        while not _MERMAID_RENDERER.is_prepared(code):
            await asyncio.sleep(0.05)
        await pilot.pause()
        assert not list(md.query(FNDMarkdownFence))


@pytest.mark.asyncio
async def test_a_shown_diagram_survives_eviction_and_restyle() -> None:
    """A restyle after the render cache dropped the entry keeps the diagram."""
    md = FNDMarkdown(FLOW, render_mermaid=True)
    async with _Host(md).run_test() as pilot:
        await md.build_done.wait()
        _MERMAID_RENDERER._results.clear()
        md.add_class("restyled")
        await pilot.pause()
        fence = next(iter(md.query(FNDMarkdownFence)))
        assert "┌" in fence._highlighted_code.plain
        assert fence.has_class("mermaid-diagram")


@pytest.mark.asyncio
async def test_a_section_cut_short_by_the_budget_is_never_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its leftover diagrams show as source for now; a capture would keep that for good."""
    from fnd.tui.preview.frozen import freeze

    monkeypatch.setattr(mermaid_render, "PREPARE_BUDGET_S", 0.05)
    src = '```mermaid\nflowchart TD\n  %% budget\n  A["`a*`"] --> B\n```\n\n' + FLOW
    md = FNDMarkdown(src, render_mermaid=True)
    async with _Host(md).run_test() as pilot:
        await md.build_done.wait()
        await pilot.pause()
        assert md.diagrams_pending
        assert freeze(md, 0) is None
