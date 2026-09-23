"""The Results title stated a truncated count as a fact about the corpus.

At `result_limit = 50` over 290 matching files the title read `50 files`, with
nothing to distinguish it from a search that genuinely matched 50. Scrolling to
the end shows the list simply stopping, so a user who verifies by counting rows
gets 50 twice and grows more confident in a wrong number.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Tree

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp


def _indexed(
    tmp_path: Path,
    tmp_index_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    files: int,
    limit: int,
) -> Config:
    root = tmp_path / "notes"
    root.mkdir()
    for i in range(files):
        (root / f"n{i:03d}.md").write_text(f"# N{i}\n\nsaffron here.\n", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [defaults]
            result_limit = {limit}

            [[collections.notes.sources]]
            path = "{root.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    build_index(roots=[root], index_dir=tmp_index_dir, collection="notes")
    return load(cfg_path)


async def _title(cfg: Config, tmp_index_dir: Path) -> str:
    app = FNDApp(index_dir=tmp_index_dir, config=cfg, collection="notes", initial_query="saffron")
    async with app.run_test(size=(100, 30)) as pilot:
        for _ in range(30):
            await pilot.pause()
        return str(app.query_one("#results_pane", Tree).border_title)


@pytest.mark.asyncio
async def test_a_truncated_count_is_marked(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """12 files match, 5 are shown: the title must not claim the corpus has 5."""
    cfg = _indexed(tmp_path, tmp_index_dir, monkeypatch, files=12, limit=5)

    title = await _title(cfg, tmp_index_dir)

    assert "5+ files" in title, title


@pytest.mark.asyncio
async def test_a_complete_count_is_not_marked(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: under the cap the number is exact, so it carries no marker."""
    cfg = _indexed(tmp_path, tmp_index_dir, monkeypatch, files=3, limit=5)

    title = await _title(cfg, tmp_index_dir)

    assert "3 files" in title, title
    assert "+" not in title, title
