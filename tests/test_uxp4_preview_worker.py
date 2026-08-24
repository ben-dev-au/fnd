"""UXP-4 §4 — preview-load worker keeps the UI responsive on big files
and surfaces a real ProgressBar + LoadingIndicator (no border-title text)
so the user gets unambiguous feedback that work is happening."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.progress import FNDProgressBar
from tests._pilot_wait import run_search, wait_until


def _write_md(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def two_file_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """Two files: a small one and a many-section one, so we can switch
    between them and observe worker behaviour."""
    a = tmp_path / "notes"
    body_lines = ["# Big Book", ""]
    for i in range(60):
        body_lines.append(f"## Section {i}")
        body_lines.append(f"Body for section {i} mentioning the target term.")
        body_lines.append("")
    _write_md(a / "small.md", "# Small\n\ntarget mention here.\n")
    _write_md(a / "big.md", "\n".join(body_lines))
    build_index(roots=[a], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_preview_load_dispatches_worker_on_cache_miss(
    cfg: Config, two_file_index: Path
) -> None:
    """A cache MISS dispatches a preview-load worker."""
    app = FNDApp(index_dir=two_file_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        big_group = next(g for g in app._search.groups if g.path.endswith("big.md"))
        # Make the miss, don't hope for it: the coverage sweep the cursor-park
        # load starts decodes NEIGHBOURS into this same cache, and once it wins
        # ``render_full_doc`` takes the cached path and dispatches nothing.
        app._preview.chunk_cache.pop(big_group.parent_id, None)
        app._preview.render_full_doc(big_group.parent_id, focus_chunk_seq=0)
        # Worker dispatched in the preview-load group.
        worker_groups = [w.group for w in app.workers]
        assert "preview-load" in worker_groups
        # Wait for the OUTCOME, not for two ticks. Two bare pauses are enough
        # on an idle machine and degrade to no-ops under a loaded one, which is
        # how this passed alone and failed in the full suite.
        await wait_until(
            pilot,
            lambda: big_group.parent_id in app._preview.chunk_cache,
            message="the preview-load worker never populated the chunk cache",
        )
        # Cache hit path: no new worker, no progress, no spinner.
        before_workers = len(app.workers)
        app._preview.render_full_doc(big_group.parent_id, focus_chunk_seq=0)
        await pilot.pause()
        assert app._preview.load_progress is None
        assert len(app.workers) <= before_workers


@pytest.mark.asyncio
async def test_preview_clears_old_content_and_shows_progress_bar(
    cfg: Config, two_file_index: Path
) -> None:
    """On cache miss the progress strip becomes visible immediately."""
    app = FNDApp(index_dir=two_file_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        small_group = next(g for g in app._search.groups if g.path.endswith("small.md"))
        big_group = next(g for g in app._search.groups if g.path.endswith("big.md"))
        # Load small file first so something is mounted.
        app._preview.render_full_doc(small_group.parent_id, focus_chunk_seq=0)
        await pilot.pause()
        await pilot.pause()
        assert app._preview.parent_id == small_group.parent_id
        # Switch to the big file. Strip should become visible immediately.
        app._preview.render_full_doc(big_group.parent_id, focus_chunk_seq=0)
        strip = app.query_one(FNDProgressBar)
        assert "-idle" not in strip.classes
        # Pane has scroll lock during load.
        pane = app.query_one("#preview_pane")
        assert "is-loading" in pane.classes


@pytest.mark.asyncio
async def test_progress_strip_runs_determinate_then_hides_on_complete(
    cfg: Config, two_file_index: Path
) -> None:
    """Strip is determinate from the first frame and hides on mount-complete.
    The old indeterminate (red) phase is intentionally gone."""
    app = FNDApp(index_dir=two_file_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        big_group = next(g for g in app._search.groups if g.path.endswith("big.md"))
        app._preview.render_full_doc(big_group.parent_id, focus_chunk_seq=0)
        # Visible + determinate at start (decode phase).
        strip = app.query_one(FNDProgressBar)
        assert "-idle" not in strip.classes
        active_session = app._progress.active
        assert active_session is not None
        assert active_session.total >= 1
        # The line now holds a completed fill for a minimum duration before it
        # clears (so short loads read as "done" rather than flashing), so a
        # fixed number of pauses is no longer a sound way to wait for it —
        # under load it under-waits, and it says nothing about WHY it cleared.
        # Gate on the widget's own state instead.
        await wait_until(
            pilot,
            lambda: "-idle" in strip.classes,
            message="progress line never cleared after the mount completed",
        )
        pane = app.query_one("#preview_pane")
        assert "is-loading" not in pane.classes
        # Chunks landed in a PreviewContainer in the pane.
        assert len(app.query("PreviewContainer")) >= 1


@pytest.mark.asyncio
async def test_switching_files_mid_load_cancels_mount_task(
    cfg: Config, two_file_index: Path
) -> None:
    """Switching to a new file mid-mount cancels the previous mount
    task so chunks from the old file can't append into the new pane."""
    app = FNDApp(index_dir=two_file_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        small_group = next(g for g in app._search.groups if g.path.endswith("small.md"))
        big_group = next(g for g in app._search.groups if g.path.endswith("big.md"))
        # Trigger big.md load and let decode complete + mount start.
        app._preview.render_full_doc(big_group.parent_id, focus_chunk_seq=0)
        await pilot.pause()  # decode done, mount task started
        first_task: Any = app._preview.mount_task
        # Switch to small.md before big's mount finishes.
        app._preview.render_full_doc(small_group.parent_id, focus_chunk_seq=0)
        # task.cancel() only *requests* cancellation — the task settles to
        # cancelled()/done() one loop tick later, so asserting those here
        # races that settle (flakes under load). cancelling() flips to >0
        # synchronously on cancel and stays until the task finishes, so it
        # is the race-free postcondition. None covers the helper nilling
        # the field; done() covers a mount that already completed.
        assert first_task is None or first_task.done() or first_task.cancelling() > 0
        # Drain the small load.
        await pilot.pause()
        await pilot.pause()
        # Final state reflects small.md, not big.md.
        assert app._preview.parent_id == small_group.parent_id


@pytest.mark.asyncio
async def test_repeat_visit_uses_cached_widgets(cfg: Config, two_file_index: Path) -> None:
    """A previously-mounted file (with chunk count above the cache
    threshold) should NOT remount on revisit — its PreviewContainer
    stays in the LRU and a return visit is an O(1) class flip."""
    app = FNDApp(index_dir=two_file_index, config=cfg, collection="notes")
    # The shipped cache caps at 1 (see _PREVIEW_CACHE_MAX_FILES) — leaving a file
    # then returning rebuilds rather than reusing its container, which measured
    # faster (a larger cache adds arrange overhead without a faster revisit).
    # Lift the cap here to exercise the LRU-reuse path this test guards.
    app._preview.preview_cache.max_files = 8
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        big_group = next(g for g in app._search.groups if g.path.endswith("big.md"))
        small_group = next(g for g in app._search.groups if g.path.endswith("small.md"))
        # First visit to big — fully mount. Gated on the cache entry appearing
        # rather than on a tick count: a fixed number of pauses is a wait only
        # while the machine is idle, and degrades to a no-op under suite load,
        # which is exactly when this test used to fail.
        app._preview.render_full_doc(big_group.parent_id, focus_chunk_seq=0)
        await wait_until(
            pilot,
            lambda: (
                app._preview.preview_cache.get(big_group.parent_id, app._search.query_signature())
                is not None
            ),
            timeout=30.0,
            message="big.md never reached the preview cache",
        )
        cached = app._preview.preview_cache.get(big_group.parent_id, app._search.query_signature())
        assert cached is not None
        # Mount is radius-bounded (Phase 2a/2b cap at _BACKGROUND_FILL_RADIUS).
        # The contract this test guards is "revisit hits the same cached
        # container" (line 201 below), not full-file completion.
        big_container = cached
        # Switch to small; no new mount task expected for big when we
        # come back (it's complete and cached).
        app._preview.render_full_doc(small_group.parent_id, focus_chunk_seq=0)
        await wait_until(
            pilot,
            lambda: (
                app._preview.active is not None
                and app._preview.active.parent_doc_id == small_group.parent_id
            ),
            timeout=30.0,
            message="never switched away to small.md",
        )
        # Return to big — the strip shows briefly during the cache-hit reveal
        # cycle, then idles once _finalise_pre_reveal's on_done fires. Wait for
        # BOTH signals together: idling is what says the reveal finished, and
        # checking it on a tick count is what made this flaky.
        app._preview.render_full_doc(big_group.parent_id, focus_chunk_seq=0)
        strip = app.query_one(FNDProgressBar)
        # The line holds a completed fill briefly, so wait for the state
        # rather than a tick count.
        await wait_until(
            pilot,
            lambda: app._preview.active is big_container and "-idle" in strip.classes,
            timeout=30.0,
            message="revisit did not settle on the cached container with the strip idle",
        )
        assert "-idle" in strip.classes
        assert app._preview.active is big_container


@pytest.mark.asyncio
async def test_rapid_file_switching_does_not_raise_duplicate_ids(
    cfg: Config, two_file_index: Path
) -> None:
    """Regression: switching files mid-mount used to crash with
    DuplicateIds because cancellation didn't run the bar's cleanup,
    leaving an id-collision-prone widget in the DOM. Class-based
    selectors + try/finally cleanup make this benign."""
    app = FNDApp(index_dir=two_file_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        small_group = next(g for g in app._search.groups if g.path.endswith("small.md"))
        big_group = next(g for g in app._search.groups if g.path.endswith("big.md"))
        # Toggle several times in quick succession — each call cancels
        # the in-flight mount and starts a new one. None of these calls
        # should raise.
        for _ in range(5):
            app._preview.render_full_doc(big_group.parent_id, focus_chunk_seq=0)
            await pilot.pause()
            app._preview.render_full_doc(small_group.parent_id, focus_chunk_seq=0)
            await pilot.pause()
        # Drain to a settled state.
        await pilot.pause()
        await pilot.pause()


@pytest.mark.asyncio
async def test_preview_title_no_longer_carries_progress_text(
    cfg: Config, two_file_index: Path
) -> None:
    """The border title stays clean — never says 'loading N/M chunks'.
    Progress lives on the ProgressBar widget instead."""
    app = FNDApp(index_dir=two_file_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        big_group = next(g for g in app._search.groups if g.path.endswith("big.md"))
        app._preview.render_full_doc(big_group.parent_id, focus_chunk_seq=0)
        # During load, title must not contain 'loading' / 'chunks'.
        title = app._preview_title()
        assert "loading" not in title.lower()
        assert "chunks" not in title.lower()
        # Drain.
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        # After load the title shows the file basename.
        title = app._preview_title()
        assert "big.md" in title
