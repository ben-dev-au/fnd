"""The rename dialog does not promise Update index will reclaim the old name.

Declining the reindex leaves the old name's documents in the index. Nothing
reaches them (the config no longer names that collection) and nothing removes
them: a rebuild only touches names the config still has, and `_ensure_index`
wipes the directory only on a schema mismatch. The orphan survives Update all,
Update index, a later rename WITH reindex, and a restart.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from fnd.config import (
    CollectionConfig,
    SourceConfig,
    delete_collection,
    load,
    write_collection,
)
from fnd.index import build_index, indexed_parent_ids
from fnd.index_runner import run_indexer


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.md").write_text("saffron\n", encoding="utf-8")
    (root / "b.md").write_text("stock\n", encoding="utf-8")
    return root


def _update(root: Path, index_dir: Path, collection: str) -> None:
    async def _drive() -> None:
        async for _ev in run_indexer(
            config=CollectionConfig(sources=[SourceConfig(path=root)]),
            collection=collection,
            index_dir=index_dir,
            rebuild=False,
        ):
            pass

    asyncio.run(_drive())


def _open(index_dir: Path):
    import tantivy

    index = tantivy.Index.open(str(index_dir))
    index.reload()
    return index


def test_update_index_does_not_reclaim_the_old_name(
    corpus: Path, tmp_index_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_index(roots=[corpus], index_dir=tmp_index_dir, collection="papers")
    assert indexed_parent_ids(_open(tmp_index_dir), "papers"), "precondition: papers is indexed"

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.papers.sources]]
            path = "{corpus.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    write_collection(
        config_path=cfg_path,
        name="research",
        collection=CollectionConfig(sources=[SourceConfig(path=corpus)]),
    )
    delete_collection(config_path=cfg_path, name="papers", renamed_to="research")
    assert "papers" not in load(cfg_path).collections, "the rename landed"

    # What "No, leave the index for now" leaves behind, then the act the
    # dialog named as the one that fixes it.
    _update(corpus, tmp_index_dir, "research")

    orphaned = indexed_parent_ids(_open(tmp_index_dir), "papers")
    assert orphaned, "if this is empty the dialog's sentence became true: reword it back"


@pytest.mark.asyncio
async def test_the_dialog_does_not_promise_update_index_reclaims_it(
    corpus: Path, tmp_index_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim the user reads, checked against the behaviour above."""
    from textual.widgets import Input

    from fnd.tui import FNDApp
    from fnd.tui.settings_screen import RenameCollectionScreen

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.papers.sources]]
            path = "{corpus.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    app = FNDApp(index_dir=tmp_index_dir, config=load(cfg_path))
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        app._config = load(cfg_path)
        # The real stack is per-collection screen → Rename; the handler pops
        # past both before pushing the confirm.
        from textual.screen import Screen as _Screen

        app.push_screen(_Screen())
        await pilot.pause()
        app.push_screen(RenameCollectionScreen(collection_name="papers"))
        for _ in range(10):
            await pilot.pause()
        app.screen.query_one("#new_collection_name", Input).value = "research"
        await pilot.press("enter")
        for _ in range(15):
            await pilot.pause()
        painted = " ".join(
            "".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()
        )

    assert "until you run Update index" not in painted, "the promise is back"
    assert "no later run removes them" in " ".join(painted.split()), painted
