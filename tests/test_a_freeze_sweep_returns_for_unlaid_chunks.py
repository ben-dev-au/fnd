"""A freeze sweep that meets chunks not yet laid out comes back for them."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.containers import VerticalScroll

from fnd.tui import FNDApp
from fnd.tui.preview.presenter import PreviewPresenter
from tests._pilot_wait import wait_until
from tests.test_a_requery_reuses_what_it_does_not_highlight import _sections


@pytest.mark.asyncio
async def test_a_sweep_whose_settle_gave_up_still_captures_the_fill(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settle that times out on a loaded runner leaves the fill unlaid."""

    async def gave_up(self: PreviewPresenter, max_rounds: int = 10) -> None:
        return None

    monkeypatch.setattr(PreviewPresenter, "await_settled", gave_up)
    app = FNDApp(index_dir=_sections(tmp_path, tmp_index_dir), initial_query="alphaword")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and app._preview.active is not None,
            timeout=20.0,
            message="the query never showed a preview",
        )
        parent_id = app._search.groups[0].parent_id
        width = app._preview.capture_width(app.query_one("#preview_pane", VerticalScroll))
        searcher = app._search.searcher
        assert searcher is not None
        far = next(
            c.chunk_seq for c in searcher.get_file_chunks(parent_id) if "betaword" in c.body_md
        )
        await wait_until(
            pilot,
            lambda: app._preview.capture_store.get_plain(parent_id, width, far) is not None,
            timeout=20.0,
            message="the sweep never came back for the chunks it met unlaid",
        )
