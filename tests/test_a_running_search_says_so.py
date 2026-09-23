"""A query in flight changes the screen.

Search is the one operation whose work happens entirely off the loop, so
every pane could stay byte-identical for its whole duration: the results
border read "Results" and the preview read "Type a query and press Enter",
over the query the user had just pressed Enter on.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import run_search


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.papers.sources]]
            path = "{(tmp_path / "papers").as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def indexed(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "papers"
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.md").write_text("saffron risotto\n", encoding="utf-8")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="papers")
    return tmp_index_dir


def _painted(app: FNDApp) -> str:
    return "\n".join(
        "".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()
    )


@pytest.mark.asyncio
async def test_the_screen_says_a_search_is_running(
    cfg: Config, indexed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Held open deliberately: a one-file index finishes inside a single
    frame, so a race decides what the assertion measures."""
    import threading

    from fnd.tui.search_controller import SearchController

    gate = threading.Event()
    real = SearchController._execute

    def _held(self: SearchController, request: object) -> object:
        gate.wait(timeout=10.0)
        return real(self, request)  # type: ignore[arg-type]

    monkeypatch.setattr(SearchController, "_execute", _held)

    app = FNDApp(index_dir=indexed, config=cfg)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        before = _painted(app)
        app._search.run("saffron")
        for _ in range(4):
            await pilot.pause()
        during = _painted(app)
        title = str(app.query_one("#results_pane", Tree).border_title)
        gate.set()
        for _ in range(20):
            await pilot.pause()

    assert during != before, "the paint was byte-identical to before Enter"
    assert "searching" in title.lower(), title
    assert "Searching for 'saffron'" in during, during[:400]


@pytest.mark.asyncio
async def test_it_gives_the_line_up_when_the_results_land(cfg: Config, indexed: Path) -> None:
    """The control: a state that outlives its operation is worse than none."""
    app = FNDApp(index_dir=indexed, config=cfg)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await run_search(pilot, app, "saffron")
        for _ in range(20):
            await pilot.pause()
        after = _painted(app)
        title = str(app.query_one("#results_pane", Tree).border_title)

    assert "searching" not in title.lower(), title
    assert "Searching for" not in after, after[:400]


@pytest.mark.asyncio
async def test_it_does_not_cover_a_preview_already_on_screen(cfg: Config, indexed: Path) -> None:
    """A preview is the last thing the user chose; announcing a query over it
    would stack the line above it rather than replacing anything."""
    app = FNDApp(index_dir=indexed, config=cfg)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await run_search(pilot, app, "saffron")
        for _ in range(25):
            await pilot.pause()
        app._search.run("risotto")
        await pilot.pause()
        during = _painted(app)

    assert "Searching for 'risotto'" not in during, "it mounted over the live preview"
