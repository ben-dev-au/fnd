"""A collection a filter has emptied says so on its row.

Cut to ZERO documents by a path filter, a row reading `● 1 source`, `2/2 active`
leaves "nothing matched" for a file on disk with nothing explaining it (config
31/11, index 21/3).

"Behind the config" needs a record of the last run and is a separate question.
"Holds nothing at all" does not: the index can answer it.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import Config, load
from fnd.index import build_index, collection_is_empty
from fnd.tui import FNDApp
from fnd.tui.menu import _collection_summary


@pytest.fixture
def two_collections(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    full = tmp_path / "full"
    full.mkdir()
    (full / "a.md").write_text("saffron\n", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.full.sources]]
            path = "{full.as_posix()}"

            [[collections.hollow.sources]]
            path = "{empty.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    cfg = load(cfg_path)
    build_index(roots=[full], index_dir=tmp_index_dir, collection="full")
    return cfg


def test_the_index_can_answer_it(tmp_path: Path, tmp_index_dir: Path) -> None:
    """The premise, at the level below the UI."""
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.md").write_text("saffron\n", encoding="utf-8")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="full")

    from fnd.index import _ensure_index

    index = _ensure_index(tmp_index_dir)

    assert not collection_is_empty(index, "full")
    assert collection_is_empty(index, "hollow")


@pytest.mark.asyncio
async def test_the_row_marks_a_collection_holding_nothing(
    two_collections: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=two_collections)
    async with app.run_test(size=(110, 30)) as pilot:
        for _ in range(15):
            await pilot.pause()
        hollow = _collection_summary(app, "hollow")
        full = _collection_summary(app, "full")

    assert "nothing indexed" in hollow, hollow
    assert "nothing indexed" not in full, full
    assert "1 source" in hollow, "the rest of the row is still wanted"


def test_without_an_index_it_says_nothing(two_collections: Config, tmp_index_dir: Path) -> None:
    """The control: a first run has no index to ask, and the launch warning
    already covers that state."""
    from types import SimpleNamespace
    from typing import Any, cast

    stub = cast(
        "Any", SimpleNamespace(_config=two_collections, _scope=SimpleNamespace(collections=[]))
    )

    assert "nothing indexed" not in _collection_summary(stub, "hollow")
