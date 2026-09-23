"""fnd opens with the query box focused, under a footer naming four keys that
all type into it.

`/`, `:`, `?` and `q` reach a focused text box as characters. Measured: the
box ends up holding `/:?q`. The settings screens already drop the anchors while
a box has focus; the screen the app opens on did not.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Input

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp


@pytest.fixture
def indexed(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.md").write_text("saffron\n", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.notes.sources]]
            path = "{root.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    build_index(roots=[root], index_dir=tmp_index_dir, collection="notes")
    return load(cfg_path)


def _footer(app: FNDApp) -> str:
    strips = app.screen._compositor.render_strips()
    return "".join(s.text for s in strips[-1])


@pytest.mark.asyncio
async def test_the_keys_it_names_at_launch_are_the_ones_that_work(
    indexed: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=indexed, collection="notes")
    async with app.run_test(size=(110, 30)) as pilot:
        for _ in range(10):
            await pilot.pause()
        focused = type(app.focused).__name__
        footer = _footer(app)

        for key in ("slash", "colon", "question_mark", "q"):
            await pilot.press(key)
            for _ in range(3):
                await pilot.pause()
        typed = app.query_one("#query_bar", Input).value

    assert focused == "Input", "the app opens on the query box"
    assert typed == "/:?q", "every one of them is a character here"
    for dead in ("Search", "Menu", "Keys", "Quit"):
        assert dead not in footer, f"{dead} types instead: {footer}"
    assert "Run" in footer, footer
    assert "Results" in footer, footer


@pytest.mark.asyncio
async def test_they_come_back_once_a_key_would_reach_them(
    indexed: Config, tmp_index_dir: Path
) -> None:
    """The control: the anchors are dropped where they are inert, not removed."""
    # Wide enough that nothing is elided: this is about which anchors are
    # OFFERED, not about how a narrow bar drops them.
    app = FNDApp(index_dir=tmp_index_dir, config=indexed, collection="notes")
    async with app.run_test(size=(170, 30)) as pilot:
        for _ in range(10):
            await pilot.pause()
        await pilot.press("escape")
        for _ in range(6):
            await pilot.pause()
        footer = _footer(app)
        focused = type(app.focused).__name__

    assert focused != "Input", "Esc leaves the box"
    for live in ("Search", "Menu", "Keys", "Quit"):
        assert live in footer, footer
