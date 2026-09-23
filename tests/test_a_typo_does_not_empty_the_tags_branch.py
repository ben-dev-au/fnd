"""The tags branch describes the results on screen, not a stricter query.

`_facet_query` parses the lexical text exactly, so a query only the fuzzy pass
can satisfy matched nothing and the branch reported "none indexed" while its
own tagged files sat in the results tree.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

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
def tagged_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "papers"
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.md").write_text(
        "---\ntags: [physics, lecture]\n---\n\nquantum entanglement\n", encoding="utf-8"
    )
    (root / "b.md").write_text(
        "---\ntags: [physics, seminar]\n---\n\nquantum fields\n", encoding="utf-8"
    )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="papers")
    return tmp_index_dir


async def _tags_after(app: FNDApp, pilot: Any, query: str) -> tuple[int, dict[str, list[Any]]]:
    await run_search(pilot, app, query)
    for _ in range(10):
        await pilot.pause()
    rows = len(app.query_one("#results_pane", Tree).root.children)
    return rows, app._scope.tag_catalogue_for_scope()


@pytest.mark.asyncio
async def test_a_typo_that_still_finds_files_still_finds_their_tags(
    cfg: Config, tagged_index: Path
) -> None:
    app = FNDApp(index_dir=tagged_index, config=cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        exact_rows, exact_tags = await _tags_after(app, pilot, "quantum")
        fuzzy_rows, fuzzy_tags = await _tags_after(app, pilot, "quantom")

    assert exact_rows, "the premise: the exact query finds the files"
    assert exact_tags["frontmatter"], "the premise: and their tags"
    assert fuzzy_rows, "the premise: the typo still finds the files"
    assert fuzzy_tags["frontmatter"], (
        "files with tags are on screen and the branch says none are indexed"
    )


@pytest.mark.asyncio
async def test_a_query_that_matches_exactly_is_not_widened(cfg: Config, tagged_index: Path) -> None:
    """The control: widening only where nothing matched exactly. Otherwise the
    branch would offer tags of files the search did not return."""
    app = FNDApp(index_dir=tagged_index, config=cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _rows, tags = await _tags_after(app, pilot, "entanglement")

    values = {entry.value for entry in tags["frontmatter"]}
    assert "lecture" in values, values
    assert "seminar" not in values, "the other file's tag reached a branch it does not describe"
