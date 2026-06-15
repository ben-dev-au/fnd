"""Search-time prefetch warms ``_chunk_cache`` (and, for flat-path
files, ``_prebuilt_cache``) for the top-N results so a cursor move
lands on a pre-warmed cache. The autouse conftest fixture disables
prefetch by default; these tests opt in with their own Config."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import Config, Defaults, RankingProfileConfig
from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import wait_until


@pytest.fixture
def cfg_with_prefetch() -> Config:
    return Config(
        defaults=Defaults(preview_prefetch_count=3, preview_load_debounce_ms=0),
        ranking={"default": RankingProfileConfig()},
    )


@pytest.fixture
def two_file_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    """Index a folder with both a markdown note and a PDF so prefetch
    has a flat-path file (``test.pdf``) and a structural file
    (``index.md``) to walk."""
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_prefetch_populates_chunk_cache(
    two_file_index: Path, cfg_with_prefetch: Config
) -> None:
    """After a search, the prefetch worker warms ``_chunk_cache`` for
    the top result file(s)."""
    app = FNDApp(index_dir=two_file_index, config=cfg_with_prefetch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._search.run("test")
        # Wait (wall-clock, not a fixed iteration cap) for the prefetch worker
        # to walk its targets — a fixed cap starves under full-suite CI load.
        await wait_until(
            pilot,
            lambda: (
                bool(app._search.groups)
                and app._search.groups[0].parent_id in app._preview.chunk_cache
            ),
            timeout=30.0,
            message="prefetch didn't warm the top result's chunk cache",
        )
        assert app._search.groups, "search returned no results"
        top = app._search.groups[0]
        assert top.parent_id in app._preview.chunk_cache, (
            f"prefetch didn't warm {top.parent_id} in _chunk_cache"
        )


@pytest.mark.asyncio
async def test_prefetch_populates_prebuilt_cache_for_flat_files(
    two_file_index: Path, cfg_with_prefetch: Config
) -> None:
    """For flat-path files (PDF / TXT) the prefetch worker also
    pre-builds the FileView + Strips bundle so the user-visible mount
    is instant."""
    app = FNDApp(index_dir=two_file_index, config=cfg_with_prefetch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._search.run("results")
        await wait_until(
            pilot,
            lambda: bool(app._search.groups),
            timeout=30.0,
            message="search returned no results",
        )
        sig = app._search.query_signature()
        flat_parents = {
            g.parent_id for g in app._search.groups if g.path.lower().endswith((".pdf", ".txt"))
        }
        if not flat_parents:
            pytest.skip("no flat-path results in fixture corpus for this query")
        # Wall-clock wait for the prefetch worker to pre-build a flat bundle.
        await wait_until(
            pilot,
            lambda: any((pid, sig) in app._preview.prebuilt_cache for pid in flat_parents),
            timeout=30.0,
            message="prefetch didn't pre-build a flat-path bundle",
        )
        assert any((pid, sig) in app._preview.prebuilt_cache for pid in flat_parents)


@pytest.mark.asyncio
async def test_prefetch_zero_disables(two_file_index: Path) -> None:
    """``preview_prefetch_count=0`` means no prefetch worker is
    spawned at all."""
    cfg = Config(defaults=Defaults(preview_prefetch_count=0, preview_load_debounce_ms=0))
    app = FNDApp(index_dir=two_file_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._search.run("test")
        await pilot.pause()
        assert not any(w.group == "preview-prefetch" for w in app.workers)


@pytest.mark.asyncio
async def test_query_change_clears_prebuilt_cache(
    two_file_index: Path, cfg_with_prefetch: Config
) -> None:
    """Bundles bake in the query's highlight spans; a new query must
    invalidate them."""
    app = FNDApp(index_dir=two_file_index, config=cfg_with_prefetch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._search.run("test")
        # Force a bundle into the cache directly so we don't depend
        # on prefetch timing.
        from fnd.tui.line_buffer import FileView, RenderedDocument

        app._preview.prebuilt_cache[("fake-parent", "old-sig")] = RenderedDocument(fv=FileView())
        app._search.run("different")
        await pilot.pause()
        assert app._preview.prebuilt_cache == {}


@pytest.mark.asyncio
async def test_prefetch_populates_flat_buffer_cache(
    two_file_index: Path, cfg_with_prefetch: Config
) -> None:
    """Prefetch stashes a RenderedDocument in _flat_buffer_cache so the next user
    click installs into the shared widget without a fresh build."""
    from fnd.tui.line_buffer import RenderedDocument
    from tests._pilot_wait import safe_pause, wait_until

    app = FNDApp(index_dir=two_file_index, config=cfg_with_prefetch)
    async with app.run_test() as pilot:
        await safe_pause(pilot)
        app._search.run("results")
        sig = app._search.query_signature()

        def _flat_parents() -> set[str]:
            return {
                g.parent_id for g in app._search.groups if g.path.lower().endswith((".pdf", ".txt"))
            }

        def _flat_cached() -> bool:
            fps = _flat_parents()
            return bool(fps) and any((pid, sig) in app._flat.cache for pid in fps)

        # Wait for results first so a no-flat-corpus skip is immediate, not a
        # 15s timeout. Then event-gate on cache population (a fixed iteration
        # count is outrun by slow prefetch decode on a serial CI runner).
        await wait_until(
            pilot,
            lambda: bool(app._search.groups),
            timeout=5.0,
            message="search results never populated",
        )
        if not _flat_parents():
            pytest.skip("no flat-path results in fixture corpus for this query")
        await wait_until(
            pilot,
            _flat_cached,
            timeout=15.0,
            message="prefetch never populated _flat_buffer_cache",
        )
        flat_parents = _flat_parents()
        prefetched = [pid for pid in flat_parents if (pid, sig) in app._flat.cache]
        assert prefetched, f"prefetch failed to cache any flat doc; flat={flat_parents}"
        for pid in prefetched:
            doc = app._flat.cache[(pid, sig)]
            assert isinstance(doc, RenderedDocument)
            assert doc.strips, f"prefetched doc for {pid} has no strips"


@pytest.fixture
def multi_md_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """Three matching md files. The top-rank user-loads; the rest are pure
    prefetch territory."""
    notes = tmp_path / "notes"
    notes.mkdir()
    for label in ("alpha", "beta", "gamma"):
        body = ["# Top heading", "Lead-in text."]
        for i in range(40):
            body.extend([f"## Section {i}", f"Filler text in section {i}."])
        body.extend(["## Anchor section", f"prefetch-anchor mention in {label}."])
        (notes / f"{label}.md").write_text("\n".join(body), encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_prefetch_premounts_structural_container(multi_md_index: Path) -> None:
    """Prefetch pre-mounts a hidden PreviewContainer into _preview_cache for
    structural files the user hasn't selected yet."""

    cfg = Config(
        defaults=Defaults(preview_prefetch_count=3, preview_load_debounce_ms=0),
        ranking={"default": RankingProfileConfig()},
    )
    app = FNDApp(index_dir=multi_md_index, config=cfg, collection="notes")
    # Prefetch pre-mounts up to the cache size; the shipped default caps the
    # cache at 1 (a larger cache adds mount/arrange overhead without speeding
    # revisits — see PREVIEW_CACHE_MAX_FILES), with the decode still prefetched
    # for every target. Lift the cap here to exercise the multi-file pre-mount.
    app._preview.preview_cache.max_files = 8
    async with app.run_test() as pilot:
        await pilot.pause()
        app._search.run("prefetch-anchor")
        sig = app._search.query_signature()

        def _non_top_done() -> bool:
            if len(app._search.groups) < 3:
                return False
            for g in app._search.groups[1:]:
                c = app._preview.preview_cache.get(g.parent_id, sig)
                if c is None or not c.mounted_indices:
                    return False
            return True

        # Two-phase wait. The initial deferred prefetch bails without caching
        # if the user-mount was still in flight when it walked the targets, so
        # wait for the user-mount to clear and results to be ready, then nudge
        # it once. Both waits are wall-clock (not a fixed iteration cap) so a
        # contended CI runner can't starve the assertions before mounts land.
        await wait_until(
            pilot,
            lambda: len(app._search.groups) >= 3 and not app._preview.user_mount_in_flight(),
            timeout=30.0,
            message="results never reached 3 / user-mount never cleared",
        )
        app._prefetch.prefetch_top_results()
        await wait_until(
            pilot,
            _non_top_done,
            timeout=30.0,
            message="prefetch did not pre-mount all non-top results",
        )
        assert len(app._search.groups) >= 3, "expected three md results in this corpus"
        for g in app._search.groups[1:]:
            cont = app._preview.preview_cache.get(g.parent_id, sig)
            assert cont is not None, f"prefetch failed to pre-mount {g.parent_id}"
            assert "-hidden" in cont.classes, f"prefetched {g.parent_id} not hidden"
            assert cont.mounted_indices, f"prefetched {g.parent_id} has no mounted chunks"


@pytest.mark.asyncio
async def test_user_selection_of_prefetched_container_runs_to_completion(
    multi_md_index: Path,
) -> None:
    """Selecting a prefetched container completes mount up to the
    background-fill radius (regression for a prefetch/user-side mount
    race that stalled at the visible window — narrower than the radius).
    With ``BACKGROUND_FILL_RADIUS = 10`` Phase 2a/2b cap mount at
    ``focus +/- 10``; full-file completion would need a wider radius."""

    from fnd.tui.preview.tuning import BACKGROUND_FILL_RADIUS

    cfg = Config(
        defaults=Defaults(preview_prefetch_count=10, preview_load_debounce_ms=0),
        ranking={"default": RankingProfileConfig()},
    )
    app = FNDApp(index_dir=multi_md_index, config=cfg, collection="notes")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._search.run("prefetch-anchor")
        await wait_until(
            pilot,
            lambda: len(app._search.groups) >= 3,
            timeout=30.0,
            message="expected three md results in this corpus",
        )
        assert len(app._search.groups) >= 3
        target = app._search.groups[1]
        target_focus = target.hits[0].chunk_seq if target.hits else 0
        app._preview.render_full_doc(target.parent_id, focus_chunk_seq=target_focus)

        def _expected_coverage(total: int, focus_idx: int, radius: int) -> int:
            """Phase 2a+2b coverage: [max(0, focus-r), min(total, focus+r+1))."""
            return min(total, focus_idx + radius + 1) - max(0, focus_idx - radius)

        def _coverage_reached() -> bool:
            ap = app._preview.active
            # Must be the TARGET's container: until render_full_doc's swap
            # completes, ``active`` is still the auto-loaded top result, whose
            # mounted_indices could clear the (target-derived) coverage bar and
            # exit the wait before the post-wait parent_doc_id assert is true.
            if ap is None or ap.parent_doc_id != target.parent_id:
                return False
            focus_idx = next(
                (
                    i
                    for i, c in enumerate(app._preview.chunk_cache.get(target.parent_id, []))
                    if c.chunk_seq == target_focus
                ),
                0,
            )
            expected = _expected_coverage(ap.total_chunks, focus_idx, BACKGROUND_FILL_RADIUS)
            return len(ap.mounted_indices) >= expected

        # Wall-clock wait for the user-side mount to reach the fill radius.
        await wait_until(
            pilot,
            _coverage_reached,
            timeout=30.0,
            message="user-side mount didn't reach the background-fill radius",
        )
        ap = app._preview.active
        assert ap is not None, "user-side mount produced no active preview"
        assert ap.parent_doc_id == target.parent_id
        focus_idx = next(
            (
                i
                for i, c in enumerate(app._preview.chunk_cache.get(target.parent_id, []))
                if c.chunk_seq == target_focus
            ),
            0,
        )
        expected = _expected_coverage(ap.total_chunks, focus_idx, BACKGROUND_FILL_RADIUS)
        assert len(ap.mounted_indices) >= expected, (
            f"user-side mount stalled at {len(ap.mounted_indices)}/{ap.total_chunks} "
            f"(expected at least {expected} from focus +/- {BACKGROUND_FILL_RADIUS} at idx {focus_idx})"
        )
        pane = app.query_one("#preview_pane")
        placeholders = [w for w in pane.children if getattr(w, "id", None) == "placeholder"]
        assert not placeholders, "placeholder still in pane after preview activated"
