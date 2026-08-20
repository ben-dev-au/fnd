"""The paint check must count a running decode as work in flight.

``render_full_doc`` on a chunk-cache miss cancels the mount task, cancels the
debounce timer, and hands the file to a background decode worker — so during a
cold load none of ``load_timer`` / ``mount_task`` / ``_finalise_task`` is set.
``pipeline_busy()`` looked only at those three, so it reported *idle* while the
decode was still running.

That is exactly when the pane legitimately shows the PREVIOUS file (the decode
path deliberately keeps the old content up rather than blanking). So after
``PAINT_CHECK_MS`` the check would see "painted, but the wrong file", call it a
strand, and spend its single repair — and the repair re-enters
``render_full_doc``, whose worker group is ``exclusive=True``, restarting the
very decode that was about to finish. On a slow file (a large PDF, or one
evicted to iCloud, where decode is measured in minutes) that both delays the
preview and burns the repair budget for that target.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import safe_pause, wait_until


@pytest.fixture
def built_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "corpus"
    root.mkdir()
    for i in range(4):
        (root / f"note_{i:02d}.md").write_text(
            f"# Apples {i}\n\nThis note is about apples for query matching.\n\n"
            f"## More {i}\n\nAnother apple paragraph here.\n"
        )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_a_running_decode_counts_as_busy(built_index: Path) -> None:
    app = FNDApp(index_dir=built_index, initial_query="apple")
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        assert app._search.groups, "setup — query produced no results"
        preview = app._preview
        await wait_until(pilot, preview.is_painted, message="preview never painted")
        await wait_until(
            pilot, lambda: not preview.pipeline_busy(), message="pipeline never went idle"
        )

        searcher = app._search.searcher
        assert searcher is not None
        target_group = next(
            (g for g in app._search.groups if g.parent_id != preview.showing_parent()),
            None,
        )
        assert target_group is not None, "fixture needs a second file"

        # Park the decode on a thread event — the worker runs off the loop.
        gate = threading.Event()
        original = searcher.get_file_chunks

        def _blocking(parent_id: str, **kw: object):  # type: ignore[no-untyped-def]
            gate.wait(timeout=30)
            return original(parent_id, **kw)  # type: ignore[arg-type]

        searcher.get_file_chunks = _blocking  # type: ignore[assignment,method-assign]
        preview.chunk_cache.pop(target_group.parent_id, None)
        preview.prebuilt_cache.clear()

        try:
            seq = target_group.hits[0].chunk_seq if target_group.hits else 0
            preview.render_full_doc(target_group.parent_id, focus_chunk_seq=seq)
            await safe_pause(pilot)

            assert preview.pipeline_busy(), (
                "a running decode is work in flight — reporting idle lets the paint "
                "check spend its one repair on a load that has not finished yet"
            )

            rebuilt: list[str] = []
            orig_render = preview.render_full_doc
            preview.render_full_doc = lambda parent_id, *, focus_chunk_seq: rebuilt.append(  # type: ignore[assignment,method-assign]
                parent_id
            )
            preview._verify_painted()
            assert not rebuilt, "the check must defer while the decode is still running"
            preview.render_full_doc = orig_render  # type: ignore[method-assign]
        finally:
            gate.set()
            searcher.get_file_chunks = original  # type: ignore[method-assign]

        # And once the decode lands, the pipeline reports idle again.
        await wait_until(
            pilot,
            lambda: not preview.pipeline_busy(),
            timeout=20.0,
            message="pipeline stayed busy after the decode completed",
        )
