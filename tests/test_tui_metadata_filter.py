"""Phase 5.5e-2: TUI extracts inline [filter] from query bar."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Input

from fnd.config import CollectionConfig, SourceConfig
from fnd.index import build_index_from_config
from fnd.tui import FNDApp


def _touch(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def tui_corpus(tmp_path: Path, tmp_index_dir: Path) -> Path:
    notes = tmp_path / "notes"
    _touch(notes / "in.md", "---\nCourse: DPwC\n---\n# A\nblue penguin\n")
    _touch(notes / "out.md", "---\nCourse: Other\n---\n# B\nblue penguin\n")
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])
    build_index_from_config(config=cc, collection="notes", index_dir=tmp_index_dir)
    return tmp_index_dir


@pytest.mark.asyncio
async def test_tui_inline_filter_narrows_results(tui_corpus: Path) -> None:
    app = FNDApp(index_dir=tui_corpus, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#query_bar", Input)
        inp.value = "[Course == 'DPwC'] blue penguin"
        await pilot.press("enter")
        await pilot.pause()
        paths = {Path(g.path).name for g in app._groups}  # type: ignore[attr-defined]
        assert "in.md" in paths
        assert "out.md" not in paths


@pytest.mark.asyncio
async def test_tui_invalid_filter_does_not_run_search(tui_corpus: Path) -> None:
    app = FNDApp(index_dir=tui_corpus, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#query_bar", Input)
        inp.value = "[Course ==] foo"
        await pilot.press("enter")
        await pilot.pause()
        # Filter has invalid DSL syntax — should clear groups, not crash.
        assert app._groups == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_tui_unclosed_bracket_does_not_run_search(tui_corpus: Path) -> None:
    app = FNDApp(index_dir=tui_corpus, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#query_bar", Input)
        inp.value = "[Course == 'DPwC' foo"  # unclosed [
        await pilot.press("enter")
        await pilot.pause()
        # ValueError from split_metadata_filter — should not run search,
        # leave groups empty (or as previous, depending on implementation).
        assert app._groups == []  # type: ignore[attr-defined]
