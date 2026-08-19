"""Navigating inside ONE file must not accumulate preview containers.

The same-file out-of-window navigation path builds a FRESH ``PreviewContainer``
and atomic-swaps to it. It used to return before reaching the sweep — which
lives on the cross-file path — on the assumption that the old container would be
"swept on the next navigation". Inside a single file that next navigation takes
the same early return, so nothing was ever reclaimed.

Nothing failed loudly; it just got slower and slower. Measured on a real
1018-chunk PDF, 30 in-file navigations left 23 containers, 270 mounted chunks
and 14,551 widgets in the pane, with navigation degrading from ~1.9s to 4-7.8s
because Textual's arrange scales with total DOM.

These tests pin the invariant that makes that impossible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.tui import FNDApp
from fnd.tui.preview import tuning
from fnd.tui.widgets.preview_container import PreviewContainer
from tests._pilot_wait import settle, wait_until
from tests._preview_corpus import wide_doc


@pytest.mark.asyncio
async def test_in_file_navigation_does_not_accumulate_containers(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Captures would serve these jumps without building a fresh container,
    # which is the point of coverage but hides the leak this pins.
    monkeypatch.setattr("fnd.tui.preview.tuning.COVERAGE_CHUNK_BUDGET", 0)
    index = wide_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and app._preview.active is not None,
            timeout=20.0,
            message="preview never became active",
        )
        group = app._search.groups[0]
        seqs = sorted({h.chunk_seq for h in group.hits})
        assert len(seqs) >= 6, f"need several spread-out hits, got {seqs}"
        searcher = app._search.searcher
        assert searcher is not None
        chunks = searcher.get_file_chunks(group.parent_id)
        assert len(chunks) > tuning.FULLMOUNT_CHUNK_BUDGET, (
            f"fixture has {len(chunks)} chunks — under the full-mount budget it is "
            "mounted whole and every jump scrolls in place, so this never exercises "
            "the rebuild path that leaked"
        )

        for seq in seqs[:8]:
            app._preview.render_full_doc(group.parent_id, focus_chunk_seq=seq)
            await settle(pilot, ticks=6)

        await settle(pilot, ticks=10)
        containers = list(app.query(PreviewContainer))
        # The cache holds one file and the active preview is protected, so a
        # couple of containers may legitimately coexist mid-swap. Eight
        # navigations must not leave eight containers.
        assert len(containers) <= 3, (
            f"{len(containers)} containers after 8 in-file navigations "
            f"(parents={[c.parent_doc_id[:6] for c in containers]}) — "
            "the same-file rebuild path is not reclaiming"
        )


@pytest.mark.asyncio
async def test_sweep_reclaims_a_stranded_container(tmp_path: Path, tmp_index_dir: Path) -> None:
    """The sweep itself: a container nothing owns is removed, the active one is not."""
    index = wide_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: app._preview.active is not None,
            timeout=20.0,
            message="preview never became active",
        )
        active = app._preview.active
        assert active is not None
        stranded = PreviewContainer(
            parent_doc_id="stranded-doc", query_signature="sig", total_chunks=1
        )
        await app.query_one("#preview_pane").mount(stranded)
        await settle(pilot, ticks=2)

        removed = app._preview.sweep_stranded_containers()
        await settle(pilot, ticks=4)

        assert removed >= 1, "sweep did not reclaim the stranded container"
        live = list(app.query(PreviewContainer))
        assert stranded not in live, "stranded container survived the sweep"
        assert active in live, "sweep removed the ACTIVE container"


@pytest.mark.asyncio
async def test_reclaim_runs_after_an_in_window_landing(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reclamation must not depend on a navigation that REBUILDS.

    The sweep ran on cross-file dispatch and the same-file rebuild path only, so
    a container stranded earlier survived for as long as the user stayed inside
    one file, where every jump is an in-window scroll and neither path runs.
    Measured on a real file: twelve in-window navigations later, one strand still
    held 6 live chunk trees and 169 widgets — more DOM than the 92 frozen chunks
    of the file actually being read.

    Asserts the sweep is REACHED from the in-window landing rather than that a
    strand disappears. A first version checked the strand and passed with the fix
    reverted, because the navigation it drove took the rebuild path after all and
    was swept by the old call site.
    """
    index = wide_doc(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and app._preview.active is not None,
            timeout=20.0,
            message="preview never became active",
        )
        container = app._preview.active
        assert container is not None
        group = app._search.groups[0]
        mounted = sorted(container.chunk_widgets)
        assert mounted, "nothing mounted to navigate within"
        target = mounted[len(mounted) // 2]

        calls: list[int] = []
        original = type(app._preview).sweep_stranded_containers

        def counting(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(1)
            return original(self, **kwargs)

        monkeypatch.setattr(type(app._preview), "sweep_stranded_containers", counting)

        # In-window by construction: the target chunk is already mounted, so
        # dispatch takes the scroll-only path.
        assert target in container.chunk_widgets
        app._preview.render_full_doc(group.parent_id, focus_chunk_seq=target)
        await settle(pilot, ticks=12)

        assert calls, (
            "no reclamation ran for an in-window navigation — a container "
            "stranded earlier would survive for the whole session"
        )
