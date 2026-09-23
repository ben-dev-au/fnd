"""Renaming or deleting a collection waits for the index run to finish.

Both verbs pair a config write with a drop from the index, and the drop needs
the index writer. Mid-run it cannot have it: measured, the drop raised, the
config write had already landed, and the running task kept writing under the
old name: 3,600 documents under a collection no config held, reachable by no
UI path, and a full `reindex -c all --rebuild` did not clear them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fnd.config import CollectionConfig, SourceConfig, load, write_collection
from fnd.tui import FNDApp
from fnd.tui.settings_screen import DeleteCollectionScreen, RenameCollectionScreen


class _Busy:
    """An indexer service with a run in flight."""

    collection = "notes"

    class _Task:
        @staticmethod
        def done() -> bool:
            return False

    task = _Task()

    def reindex_with_warning(self, *_a: Any, **_kw: Any) -> None:  # pragma: no cover
        raise AssertionError("no reindex may start while one is running")


class _Idle:
    collection = None
    task = None

    def __init__(self) -> None:
        self.calls: list[str] = []

    def reindex_with_warning(self, name: str, **_kw: Any) -> None:
        self.calls.append(name)


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    corpus = tmp_path / "vault"
    corpus.mkdir()
    write_collection(
        config_path=cfg_path,
        name="notes",
        collection=CollectionConfig(sources=[SourceConfig(path=corpus)]),
    )
    return cfg_path


@pytest.mark.asyncio
async def test_a_rename_is_refused_while_indexing(configured: Path, tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    said: list[str] = []
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._config = load()
        app._indexer = _Busy()  # type: ignore[assignment]
        screen = RenameCollectionScreen(collection_name="notes")
        app.push_screen(screen)
        await pilot.pause()
        screen.notify = lambda msg, **kw: said.append(str(msg))  # type: ignore[method-assign]
        from textual.widgets import Input

        screen.query_one("#new_collection_name", Input).value = "Archive"
        await pilot.press("enter")
        await pilot.pause()

    assert set(load(configured).collections) == {"notes"}, "the config must be untouched"
    assert said, "a refusal must say why"
    assert "still running" in said[0], said


@pytest.mark.asyncio
async def test_a_delete_is_refused_while_indexing(configured: Path, tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    said: list[str] = []
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._config = load()
        app._indexer = _Busy()  # type: ignore[assignment]
        screen = DeleteCollectionScreen(collection_name="notes")
        app.push_screen(screen)
        await pilot.pause()
        screen.notify = lambda msg, **kw: said.append(str(msg))  # type: ignore[method-assign]
        from textual.widgets import OptionList

        lst = screen.query_one("#confirm_list", OptionList)
        lst.highlighted = next(i for i, o in enumerate(lst._options) if o.id == "yes")
        lst.action_select()
        await pilot.pause()

    assert set(load(configured).collections) == {"notes"}
    assert said, "a refusal must say why"
    assert "still running" in said[0], said


@pytest.mark.asyncio
async def test_a_rename_still_works_when_nothing_is_running(
    configured: Path, tmp_index_dir: Path
) -> None:
    """The control: the guard must not block the ordinary case."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._config = load()
        app._indexer = _Idle()  # type: ignore[assignment]
        from textual.screen import Screen

        app.push_screen(Screen())
        await pilot.pause()
        screen = RenameCollectionScreen(collection_name="notes")
        app.push_screen(screen)
        await pilot.pause()
        from textual.widgets import Input

        screen.query_one("#new_collection_name", Input).value = "Archive"
        await pilot.press("enter")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert set(load(configured).collections) == {"Archive"}


def test_the_guard_reads_the_service_not_a_flag() -> None:
    """A bool would go stale; the task itself is the signal."""
    from fnd.tui.settings_screen import _indexing_now

    class _App:
        _indexer = _Busy()

    class _Done:
        _indexer = _Idle()

    assert _indexing_now(_App()) == "notes"  # type: ignore[arg-type]
    assert _indexing_now(_Done()) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_removing_a_source_is_refused_while_indexing(
    configured: Path, tmp_index_dir: Path
) -> None:
    """The dialog promises the collection is rebuilt straight afterwards. Mid
    run that rebuild is refused and dropped, so the removed source's files
    stay searchable and the promise is false."""
    from textual.widgets import OptionList

    from fnd.tui.settings_screen import DeleteSourceScreen

    app = FNDApp(index_dir=tmp_index_dir)
    said: list[str] = []
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._config = load()
        app._indexer = _Busy()  # type: ignore[assignment]
        screen = DeleteSourceScreen(collection_name="notes", source_index=0)
        app.push_screen(screen)
        await pilot.pause()
        screen.notify = lambda msg, **kw: said.append(str(msg))  # type: ignore[method-assign]
        lst = screen.query_one("#confirm_list", OptionList)
        lst.highlighted = next(i for i, o in enumerate(lst._options) if o.id == "yes")
        lst.action_select()
        await pilot.pause()

    assert load(configured).collections["notes"].sources, "the source must still be there"
    assert said, "a refusal must say why"
    assert "still running" in said[0], said


@pytest.mark.asyncio
async def test_a_dropped_reindex_is_announced(tmp_index_dir: Path) -> None:
    """A caller that opens no modal still has to learn its request was
    dropped: delete-source promised a rebuild and got silence, so the run
    never happened and the removed folder stayed searchable."""
    from fnd.tui.indexer_service import IndexerService

    app = FNDApp(index_dir=tmp_index_dir)
    said: list[str] = []
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        service = IndexerService(app)
        service.task = _Busy.task  # type: ignore[assignment]
        service.collection = "notes"
        app.notify = lambda msg, **kw: said.append(str(msg))  # type: ignore[method-assign]
        started = service.start(collection="notes", open_modal=False)
        await pilot.pause()

    assert started is False
    assert said, "a dropped request must not be silent"
    assert "not re-indexed" in said[0], said
