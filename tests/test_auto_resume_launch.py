"""Launch must never start indexing the user didn't ask for.

Indexing is heavy (PDF texturising); a laptop shouldn't burn battery on
work it didn't trigger. So ``_maybe_resume_indexer`` is opt-in
(defaults.indexer_auto_resume, off by default) and, even when enabled,
only resumes recent state for a still-existing collection.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.index_runner import IndexState, save_state, state_file_for
from fnd.tui import FNDApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def _write_resumable_state() -> None:
    """A fresh, unfinished state file for the default collection."""
    now = datetime.datetime.now(tz=datetime.UTC).isoformat(timespec="seconds")
    save_state(
        state_file_for("default"),
        IndexState(
            collection="default", started_at=now, total_files=3, files_completed=1, last_update=now
        ),
    )


@pytest.mark.asyncio
async def test_launch_does_not_resume_by_default(
    built_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default config (auto_resume off): a resumable state file on disk must
    NOT trigger background indexing on launch."""
    _write_resumable_state()
    calls: list[str] = []

    def _record(self: FNDApp, **kw: object) -> None:
        calls.append(str(kw.get("collection", "")))

    monkeypatch.setattr(FNDApp, "start_indexer", _record)

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._indexer.maybe_resume()
        await pilot.pause()

    assert calls == [], "indexing started on launch without the user asking"
