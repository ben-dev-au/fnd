"""Mermaid fences render as diagrams on the live FNDMarkdown path.

A ```mermaid fence renders as termaid text-art when the parent markdown
carries ``render_mermaid=True``; it falls back to syntax-highlighted source
when the flag is off, when the diagram is unsupported/garbage, and is inert
for non-mermaid fences. A query match inside a rendered diagram still
registers the fence as the scroll anchor (no painted span on diagram art).
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from fnd.matching import MatchSpec
from fnd.tui.app import FNDMarkdown, FNDMarkdownFence

FLOW = "```mermaid\nflowchart TD\n    A[Start] --> B[End]\n```"


class _Host(App[None]):
    def __init__(self, md: FNDMarkdown) -> None:
        self.md = md
        super().__init__()

    def compose(self) -> ComposeResult:
        yield self.md


async def _fence_plain(source: str, **kw) -> str:
    """Rendered text of the first fence, captured while the app is live."""
    md = FNDMarkdown(source, **kw)
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
async def test_wide_diagram_falls_back_to_source_when_it_exceeds_pane() -> None:
    # A flowchart laid out left-to-right gets wide; with a narrow pane width
    # threaded in, it must fall back to source rather than render blank.
    wide = (
        "```mermaid\nflowchart LR\n"
        + "\n".join(f"    A{i} --> A{i + 1}" for i in range(12))
        + "\n```"
    )
    plain = await _fence_plain(wide, render_mermaid=True, mermaid_width=40)
    assert "flowchart" in plain  # source, not a diagram
    assert "┌" not in plain


@pytest.mark.asyncio
async def test_narrow_diagram_renders_within_pane_width() -> None:
    plain = await _fence_plain(FLOW, render_mermaid=True, mermaid_width=200)
    assert "┌" in plain
    assert "flowchart" not in plain


@pytest.mark.asyncio
async def test_match_inside_diagram_registers_anchor() -> None:
    src = "```mermaid\nflowchart TD\n    A[Needle] --> B[End]\n```"
    md = FNDMarkdown(src, render_mermaid=True, match_spec=MatchSpec.from_query("needle"))
    async with _Host(md).run_test():
        await md.build_done.wait()
        fence = next(iter(md.query(FNDMarkdownFence)))
        assert md.first_match_block is fence
