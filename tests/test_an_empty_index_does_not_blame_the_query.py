"""A scope holding nothing was worded exactly like a query matching nothing.

`No results for 'risotto'.` is true of both, and only one of them is about the
query. A user whose collection has never been indexed is told to change their
search.

The index must EXIST for this: with no index at all the app never runs a
search, which is a different gap.
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
def two_collections(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """`indexed` is built; `fresh` never has been. The index exists either way."""
    for name in ("indexed", "fresh"):
        root = tmp_path / name
        root.mkdir()
        (root / "a.md").write_text(f"# {name}\n\nrisotto here.\n", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.indexed.sources]]
            path = "{(tmp_path / "indexed").as_posix()}"
            [[collections.fresh.sources]]
            path = "{(tmp_path / "fresh").as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    build_index(roots=[tmp_path / "indexed"], index_dir=tmp_index_dir, collection="indexed")
    return load(cfg_path)


async def _empty_state(cfg: Config, index_dir: Path, collection: str, query: str) -> str:
    app = FNDApp(index_dir=index_dir, config=cfg, collection=collection, initial_query=query)
    async with app.run_test(size=(110, 30)) as pilot:
        # Gated on the query having RUN: at startup `idle` is already True and
        # `groups` already empty, so the weaker wait reads the pre-search state.
        await wait_until(
            pilot,
            lambda: (
                (app._search.current_query or "").strip() == query
                and app._search.idle
                and not app._search.groups
            ),
            timeout=30.0,
            message="the search never settled on an empty result",
        )
        return app._results.empty_state()


@pytest.mark.asyncio
async def test_a_scope_with_nothing_indexed_says_so(
    two_collections: Config, tmp_index_dir: Path
) -> None:
    """`fresh` has files on disk and nothing in the index. The word is there;
    the index has never been told about it."""
    text = await _empty_state(two_collections, tmp_index_dir, "fresh", "risotto")

    assert "Nothing is indexed" in text, text
    assert "Update index" in text, text


@pytest.mark.asyncio
async def test_a_genuine_miss_still_blames_the_query(
    two_collections: Config, tmp_index_dir: Path
) -> None:
    """The control, and the one that matters: an indexed scope that simply does
    not hold the word must NOT send the user off to index."""
    text = await _empty_state(two_collections, tmp_index_dir, "indexed", "vellichor")

    assert "No results for 'vellichor'" in text, text
    assert "Nothing is indexed" not in text, text
