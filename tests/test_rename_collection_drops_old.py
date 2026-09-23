"""Renaming a collection takes its old documents out of the index with it.

The rename wrote the new name and deleted the old one from the config, and
stopped there. The old name's documents stayed: Delete is the only caller of
delete_documents(F_COLLECTION, …) and it is unreachable once the config no
longer names the collection, `-c <old>` is refused, and `reindex -c all
--rebuild` leaves them. They are served, too: the CLI's default scope applies
no collection filter, so a file deleted from disk came back as the top hit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import tantivy
from textual.screen import Screen
from textual.widgets import Input, OptionList

from fnd.config import CollectionConfig, SourceConfig, load, write_collection
from fnd.index import build_index_from_config
from fnd.schema import F_COLLECTION
from fnd.tui import FNDApp
from fnd.tui.settings_screen import RenameCollectionScreen


def _counts(index_dir: Path) -> dict[str, int]:
    index = tantivy.Index.open(str(index_dir))
    index.reload()
    searcher = index.searcher()
    hits = searcher.search(tantivy.Query.all_query(), limit=500).hits
    out: dict[str, int] = {}
    for _score, address in hits:
        name = str(searcher.doc(address).get_first(F_COLLECTION))
        out[name] = out.get(name, 0) + 1
    return out


class _StubIndexer:
    """Records the reindex instead of pushing the modal."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def reindex_with_warning(self, name: str, *, rebuild: bool = False, **_kw: Any) -> None:
        self.calls.append((name, rebuild))


@pytest.mark.asyncio
async def test_rename_leaves_no_documents_under_the_old_name(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    corpus = tmp_path / "vault"
    corpus.mkdir()
    (corpus / "a.md").write_text("# Alpha One\n\nbody\n", encoding="utf-8")
    collection = CollectionConfig(sources=[SourceConfig(path=corpus)])
    write_collection(config_path=cfg_path, name="Alpha", collection=collection)
    build_index_from_config(config=collection, collection="Alpha", index_dir=tmp_index_dir)
    assert _counts(tmp_index_dir).get("Alpha")

    app = FNDApp(index_dir=tmp_index_dir)
    stub = _StubIndexer()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._config = load()
        app._indexer = stub  # type: ignore[assignment]
        # The rename pops twice, past itself and the per-collection screen
        # it was opened from, so it needs one below it.
        app.push_screen(Screen())
        await pilot.pause()
        app.push_screen(RenameCollectionScreen(collection_name="Alpha"))
        await pilot.pause()
        app.screen.query_one("#new_collection_name", Input).value = "Archive"
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause()
        # Dropping the old name's documents asks first, so the test confirms,
        # which also proves the dialog does not disturb the drop-then-rebuild
        # ordering the rest of this test is about.
        assert app.screen.__class__.__name__ == "RebuildConfirmScreen", app.screen
        options = app.screen.query_one("#confirm_list", OptionList)
        options.highlighted = next(i for i, o in enumerate(options._options) if o.id == "yes")
        await pilot.pause()
        options.action_select()
        for _ in range(10):
            await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert "Alpha" not in _counts(tmp_index_dir), "the old name's documents are unreachable"
    assert stub.calls == [("Archive", True)], "the new name is still rebuilt, after the drop"
    assert "Alpha" not in load(cfg_path).collections
