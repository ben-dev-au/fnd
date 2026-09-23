"""An update that removed three files reported "0 newly / 11 already".

`prune_removed_files` returns the number it dropped; discarding it leaves the
one line the user reads after a filter change describing everything except the
change.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fnd.config import CollectionConfig, SourceConfig, SourceFilters
from fnd.index_runner import ProgressEvent, run_indexer
from fnd.tui.indexer_modal import _format_indexed_line


def _collection(root: Path, *, max_size: int | None) -> CollectionConfig:
    return CollectionConfig(
        sources=[SourceConfig(path=root, filters=SourceFilters(max_size=max_size))]
    )


def _run(config: CollectionConfig, index_dir: Path, *, rebuild: bool = False) -> ProgressEvent:
    async def _drive() -> ProgressEvent:
        last: ProgressEvent | None = None
        async for ev in run_indexer(
            config=config,
            collection="notes",
            index_dir=index_dir,
            rebuild=rebuild,
            echo_skips=True,
        ):
            last = ev
        assert last is not None
        return last

    return asyncio.run(_drive())


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "small.md").write_text("saffron\n", encoding="utf-8")
    (root / "big.md").write_text("saffron " * 400, encoding="utf-8")
    return root


def test_the_done_event_carries_what_was_dropped(corpus: Path, tmp_index_dir: Path) -> None:
    first = _run(_collection(corpus, max_size=None), tmp_index_dir)
    assert first.kind == "done"
    assert first.removed_total == 0, "nothing left the collection on the first run"

    narrowed = _run(_collection(corpus, max_size=100), tmp_index_dir)

    assert narrowed.kind == "done"
    assert narrowed.indexed_newly_total == 0, "the point: nothing was added"
    assert narrowed.removed_total == 1, "and one file left"


def test_the_line_says_so() -> None:
    assert "1 removed" in _format_indexed_line(0, 1, 0, 1)


def test_a_run_that_removed_nothing_stays_quiet() -> None:
    """The control: an ordinary update must not grow a "0 removed" chip."""
    assert "removed" not in _format_indexed_line(2, 9, 0, 0)


def test_a_rebuild_counts_what_did_not_come_back(corpus: Path, tmp_index_dir: Path) -> None:
    """A rebuild re-processes files in place (no wipe under normalised storage),
    so a survivor is already-indexed, not new; the point is the departure."""
    _run(_collection(corpus, max_size=None), tmp_index_dir)

    narrowed = _run(_collection(corpus, max_size=100), tmp_index_dir, rebuild=True)

    assert narrowed.kind == "done"
    assert narrowed.indexed_newly_total == 0, "the survivor was already a member"
    assert narrowed.removed_total == 1, "and one file did not come back"


def test_a_rebuild_that_loses_nothing_reports_nothing(corpus: Path, tmp_index_dir: Path) -> None:
    """The control: an ordinary rebuild must not claim to have removed files."""
    _run(_collection(corpus, max_size=None), tmp_index_dir)

    again = _run(_collection(corpus, max_size=None), tmp_index_dir, rebuild=True)

    assert again.removed_total == 0


def test_the_first_rebuild_of_a_collection_removes_nothing(
    corpus: Path, tmp_index_dir: Path
) -> None:
    """Nothing was there to lose, so the count must not read as loss."""
    first = _run(_collection(corpus, max_size=None), tmp_index_dir, rebuild=True)

    assert first.removed_total == 0
