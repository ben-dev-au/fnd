"""A search over 32 matching files reported 24, and the setting that should
govern it did nothing.

`search_controller` hardcoded `limit=50` while `defaults.result_limit` sat in
Preferences being written to config and read by nobody. The header stated the
truncated count as though it were the total, and ticking a tag facet surfaced
files the unfiltered query had hidden, because narrowing the query changed
which 50 survived.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp


def _corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, n: int, limit: int | None) -> Config:
    root = tmp_path / "notes"
    root.mkdir()
    for i in range(n):
        (root / f"note{i:03d}.md").write_text(
            f"# Note {i}\n\nsaffron appears here.\n", encoding="utf-8"
        )
    cfg_path = tmp_path / "config.toml"
    limit_line = f"result_limit = {limit}\n" if limit is not None else ""
    cfg_path.write_text(
        textwrap.dedent(f"""
            [defaults]
            {limit_line}
            [[collections.notes.sources]]
            path = "{root.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


async def _groups(app: FNDApp) -> int:
    async with app.run_test(size=(110, 30)) as pilot:
        for _ in range(40):
            await pilot.pause()
        return len(app._search.groups)


@pytest.mark.asyncio
async def test_more_than_fifty_files_all_come_back(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A limit above 50 has to reach the searcher, not a hardcoded cap."""
    cfg = _corpus(tmp_path, monkeypatch, 60, 100)
    build_index(roots=[tmp_path / "notes"], index_dir=tmp_index_dir, collection="notes")
    app = FNDApp(index_dir=tmp_index_dir, config=cfg, collection="notes", initial_query="saffron")

    found = await _groups(app)

    assert found == 60, found


@pytest.mark.asyncio
async def test_the_configured_limit_is_obeyed(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the setting has to DO something, in both directions."""
    cfg = _corpus(tmp_path, monkeypatch, 20, 3)
    build_index(roots=[tmp_path / "notes"], index_dir=tmp_index_dir, collection="notes")
    assert cfg.defaults.result_limit == 3
    app = FNDApp(index_dir=tmp_index_dir, config=cfg, collection="notes", initial_query="saffron")

    found = await _groups(app)

    assert found == 3, found
