"""A run that stopped part-way is visible, on the screen and at launch.

Measured against a real corpus: cancelling a rebuild at 7% took a collection
from 3007 documents to 377, and SIGKILL took it to 207. The app writes
`files_completed = 210, total_files = 3000` to disk, so it knows; shown like
the intact collections, searches answer from 7% of the corpus, silently.

Opting out of auto-resume is not opting out of being told.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from fnd.config import CollectionConfig, Config, SourceConfig
from fnd.index_runner import IndexState, save_state, state_file_for
from fnd.tui import FNDApp
from fnd.tui.menu import _collection_summary, interrupted_index


@pytest.fixture
def config(tmp_path: Path) -> Config:
    root = tmp_path / "notes"
    root.mkdir()
    return Config(collections={"bulk": CollectionConfig(sources=[SourceConfig(path=root)])})


def _stopped_at(done: int, total: int, name: str = "bulk") -> None:
    """Write the state an interrupted run leaves behind.

    `isolated_indexer_resume_state` in conftest redirects the whole state
    directory, so this never touches a real one.
    """
    save_state(
        state_file_for(name),
        IndexState(
            collection=name,
            started_at=dt.datetime.now(tz=dt.UTC).isoformat(),
            total_files=total,
            files_completed=done,
        ),
    )


def test_a_part_way_run_is_reported() -> None:
    _stopped_at(210, 3000)
    assert interrupted_index("bulk") == (210, 3000)


def test_a_finished_run_is_not() -> None:
    """The control: a completed run leaves state behind too, and a warning
    over a healthy collection is worse than none."""
    _stopped_at(3000, 3000)
    assert interrupted_index("bulk") is None


def test_a_run_that_never_started_is_not() -> None:
    _stopped_at(0, 3000)
    assert interrupted_index("bulk") is None


def test_another_collection_is_left_alone() -> None:
    _stopped_at(210, 3000, name="other")
    assert interrupted_index("bulk") is None


@pytest.mark.asyncio
async def test_the_row_carries_it(config: Config, tmp_index_dir: Path) -> None:
    """Where a user looks when wondering about a collection."""
    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        healthy = _collection_summary(app, "bulk")
        _stopped_at(210, 3000)
        interrupted = _collection_summary(app, "bulk")

    assert "incomplete" not in healthy, healthy
    assert "⚠ incomplete: 210 of 3000 files" in interrupted, interrupted
    assert "ranking:" in interrupted, "the row lost what it already said"


@pytest.mark.asyncio
async def test_the_update_row_carries_it_too(config: Config, tmp_index_dir: Path) -> None:
    """On the screen whose button fixes it, not only where someone browsing
    would notice: the collection's own screen (the one you open to act) as
    well as the Collections LIST."""
    from fnd.tui.menu import _summary_collection_update

    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        healthy = _summary_collection_update(app, "bulk")
        _stopped_at(210, 3000)
        interrupted = _summary_collection_update(app, "bulk")

    assert "incomplete" not in healthy, healthy
    assert "⚠ incomplete: 210 of 3000 files" in interrupted, interrupted
    assert "sources" in interrupted, "the row lost what it already said"
