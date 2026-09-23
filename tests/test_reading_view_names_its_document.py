"""Reading View names its document in the pane's top edge.

In a corpus of documents that read alike, the claim being checked turns on
which one says what.

The rest of the border is dropped so the frame is not copied with the text. A
top edge is not inside a text selection, and pane names live at the top
everywhere else in this app, so that edge carries the title.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import wait_until


@pytest.fixture
def indexed(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "governance-handbook.md").write_text(
        "# Handbook\n\n" + "\n".join(f"Line {i} about the quorum." for i in range(40)),
        encoding="utf-8",
    )
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


def _painted(app: FNDApp) -> list[str]:
    return ["".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()]


@pytest.mark.asyncio
async def test_reading_view_names_its_file_at_the_top(indexed: Config, tmp_index_dir: Path) -> None:
    """Painted, not merely set: the border was `none` here, so a title that
    existed on the widget appeared nowhere on screen."""
    app = FNDApp(
        index_dir=tmp_index_dir, config=indexed, collection="notes", initial_query="quorum"
    )
    async with app.run_test(size=(100, 20)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups),
            timeout=30.0,
            message="the search never produced a result",
        )
        app.action_toggle_reading_mode()
        await wait_until(
            pilot,
            lambda: any("governance-handbook.md" in r for r in _painted(app)),
            timeout=30.0,
            message="reading view never painted its file name",
        )
        rows = _painted(app)

    assert app._reading_mode is True
    named = [i for i, r in enumerate(rows) if "governance-handbook.md" in r]
    assert named, rows[:4]
    # At the TOP, where every other pane names itself, not buried in the body.
    assert min(named) <= 2, [rows[i] for i in named]


@pytest.mark.asyncio
async def test_leaving_reading_view_restores_the_full_border(
    indexed: Config, tmp_index_dir: Path
) -> None:
    """The control: the top-only rule must not leak into split view, which
    needs its whole frame."""
    app = FNDApp(
        index_dir=tmp_index_dir, config=indexed, collection="notes", initial_query="quorum"
    )
    async with app.run_test(size=(100, 20)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups),
            timeout=30.0,
            message="the search never produced a result",
        )
        app.action_toggle_reading_mode()
        for _ in range(10):
            await pilot.pause()
        app.action_toggle_reading_mode()
        await wait_until(
            pilot,
            lambda: not app._reading_mode and any("Results" in r for r in _painted(app)),
            timeout=30.0,
            message="split view never came back",
        )
        rows = _painted(app)

    assert any("╭─" in r and "Preview" in r for r in rows), rows[:4]
