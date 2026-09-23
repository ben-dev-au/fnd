"""`c` on a completed run printed "Cancelling… waiting for current file to
abort." and left it there forever.

`_sync_action_options` already removes Background, Cancel and Skip from the
option list once the chain finishes, but the key BINDINGS were never gated, so
`c` still ran `action_cancel`, over a `100%` bar and a `Done.` line, on a run
with no current file and nothing to abort.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import CollectionConfig, Config, SourceConfig, load, write_collection
from fnd.index_runner import ProgressEvent
from fnd.tui import FNDApp
from fnd.tui.indexer_modal import IndexerScreen


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    write_collection(
        config_path=cfg_path,
        name="probe",
        collection=CollectionConfig(sources=[SourceConfig(path=root)]),
    )
    return load(cfg_path)


@pytest.mark.asyncio
async def test_c_on_a_finished_run_says_nothing_about_cancelling(
    config: Config, tmp_index_dir: Path
) -> None:
    """The run is over: there is no current file and nothing to abort."""
    app = FNDApp(index_dir=tmp_index_dir, config=config)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.push_screen(IndexerScreen("probe"))
        for _ in range(15):
            await pilot.pause()
        app._indexer.last_event = ProgressEvent(kind="done", files_done=17, files_total=17)
        for _ in range(10):
            await pilot.pause()

        await pilot.press("c")
        for _ in range(10):
            await pilot.pause()
        painted = "\n".join(
            "".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()
        )

    assert "Cancelling" not in painted, painted
