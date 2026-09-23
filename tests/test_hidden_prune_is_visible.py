"""The always-on hidden prune is named where the filters are named.

The walk prunes every dot-prefixed name whatever the filters say. That rule
appeared on no screen and in no doc: the README's filter table listed thirteen
and not this one, and the "Hidden / system" excludes preset implied unticking
it would index hidden files, which it cannot.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fnd.config import EXCLUDES_PRESETS
from fnd.filters import FilterSpec
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterBrowserScreen

_README = Path(__file__).resolve().parent.parent / "README.md"


def test_the_readme_filter_table_lists_it() -> None:
    table = _README.read_text(encoding="utf-8").split("| Filter | What it does |")[1]
    row = table.splitlines()[2]
    assert "Hidden files" in row, row
    assert "always on" in row.lower(), row


def test_the_preset_does_not_promise_to_bring_hidden_files_back() -> None:
    label = str(EXCLUDES_PRESETS["hidden"]["label"])
    assert "always" in label.lower(), label
    # The globs stay: after a dotted include glob admits a hidden path, they
    # are the only thing that can take it out again.
    assert "**/.*" in EXCLUDES_PRESETS["hidden"]["globs"]


@pytest.mark.asyncio
async def test_the_browser_summary_says_it(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.push_screen(
            FilterBrowserScreen(
                title="Index filters",
                spec=FilterSpec(),
                gitignore=True,
                fndignore=True,
                on_save=lambda *_a: None,
            )
        )
        for _ in range(15):
            await pilot.pause()
        strips = app.screen._compositor.render_strips()
        painted = "\n".join(
            re.sub(r"\s+", " ", "".join(seg.text for seg in strip)) for strip in strips
        )

    assert "skipping hidden files" in painted, painted[:600]
