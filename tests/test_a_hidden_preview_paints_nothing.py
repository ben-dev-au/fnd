"""A preview held behind ``-pre-reveal`` (opacity 0) paints nothing, not even its
matches: Textual's opacity blend skips ANSI palette colours, so a highlight
drawn in ANSI black showed through a table on every cold launch."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.color import ColorType
from rich.style import Style

from fnd.index import build_index
from fnd.render import DIM_STYLES, MATCH_STYLES, MISMATCH_STYLE
from fnd.tui import FNDApp
from tests._pilot_wait import safe_pause

TERM = "scaffold"


@pytest.mark.parametrize("style", [*MATCH_STYLES, MISMATCH_STYLE, *sorted(DIM_STYLES)])
def test_every_match_style_is_drawn_in_rgb(style: str) -> None:
    parsed = Style.parse(style)
    for colour in (parsed.color, parsed.bgcolor):
        assert colour is not None
        assert colour.type == ColorType.TRUECOLOR, style


def _corpus(tmp_path: Path, tmp_index_dir: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    filler = "".join(f"Filler line {i}.\n\n" for i in range(12))
    body = (
        "# Cheatsheet\n\n" + filler + "## Terminal\n\n"
        f"### Scaffold a controller\n\nRun the {TERM} step.\n\n"
        "| Task | Command |\n|---|---|\n"
        f"| Scaffold a controller | #Terminal - {TERM} controller |\n\n" + filler
    )
    (docs / "sheet.md").write_text(body, encoding="utf-8")
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_a_preview_still_behind_pre_reveal_shows_no_text(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fnd.tui.preview.presenter import PreviewPresenter

    # Hold the cold launch's preview hidden, as a slow landing does.
    monkeypatch.setattr(PreviewPresenter, "reveal", lambda self, c: None)
    monkeypatch.setattr(PreviewPresenter, "reveal_active", lambda self: None)
    app = FNDApp(index_dir=_corpus(tmp_path, tmp_index_dir), initial_query=TERM)
    async with app.run_test(size=(116, 45)) as pilot:
        for _ in range(200):
            await safe_pause(pilot)
            active = app._preview.active
            if active is not None and active.chunk_widgets and not app._preview.pipeline_busy():
                break
        active = app._preview.active
        assert active is not None
        assert active.has_class("-pre-reveal"), "the preview was revealed: nothing held it"
        pane = app.query_one("#preview_pane")
        left, right = pane.region.x + 1, pane.region.right - 2
        shown: list[str] = []
        for strip in app.screen._compositor.render_strips()[
            pane.region.y + 1 : pane.region.bottom - 1
        ]:
            x = 0
            for seg in strip:
                start, x = x, x + len(seg.text)
                style = seg.style
                if x <= left or start >= right or not seg.text.strip() or style is None:
                    continue
                if style.color is not None and style.color != style.bgcolor:
                    shown.append(seg.text.strip())
        assert shown == []
