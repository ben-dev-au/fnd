"""Search-time prefetch warms ``_chunk_cache`` (and, for flat-path
files, ``_prebuilt_cache``) for the top-N results so a cursor move
lands on a pre-warmed cache. The autouse conftest fixture disables
prefetch by default; these tests opt in with their own Config."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fnd.config import Config, Defaults, RankingProfileConfig
from fnd.index import build_index
from fnd.tui import FNDApp


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
        # Give the prefetch worker time to walk its sequential targets.
        for _ in range(20):
            await pilot.pause()
            await asyncio.sleep(0.05)
            if app._search.groups and app._search.groups[0].parent_id in app._preview.chunk_cache:
                break
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
        flat_parents: set[str] = set()
        # Drain a few cycles to give the prefetch worker time.
        for _ in range(30):
            await pilot.pause()
            await asyncio.sleep(0.05)
            flat_parents = {
                g.parent_id for g in app._search.groups if g.path.lower().endswith((".pdf", ".txt"))
            }
            if flat_parents and any(
                (pid, app._search.query_signature()) in app._preview.prebuilt_cache
                for pid in flat_parents
            ):
                break
        if not flat_parents:
            pytest.skip("no flat-path results in fixture corpus for this query")
        assert any(
            (pid, app._search.query_signature()) in app._preview.prebuilt_cache
            for pid in flat_parents
        )


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

        # Event-driven, wall-clock budgeted: a fixed iteration count is defeated
        # by slow prefetch decode on a serial CI runner (the original flake);
        # wait on the cache-population condition instead.
        try:
            await wait_until(
                pilot,
                _flat_cached,
                timeout=15.0,
                message="prefetch never populated _flat_buffer_cache",
            )
        except AssertionError:
            if not _flat_parents():
                pytest.skip("no flat-path results in fixture corpus for this query")
            raise
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
    import asyncio

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

        # Two-phase wait: first let the initial deferred prefetch run; if
        # the user-mount was still in flight when it walked the targets,
        # it bails without caching anything, so nudge it once user-mount
        # has cleared. Cap total wait at ~12s.
        nudged = False
        for _ in range(240):
            await pilot.pause()
            await asyncio.sleep(0.05)
            if _non_top_done():
                break
            if (
                not nudged
                and not app._preview.user_mount_in_flight()
                and len(app._search.groups) >= 3
            ):
                app._prefetch.prefetch_top_results()
                nudged = True
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
    import asyncio

    from fnd.tui.preview.tuning import BACKGROUND_FILL_RADIUS

    cfg = Config(
        defaults=Defaults(preview_prefetch_count=10, preview_load_debounce_ms=0),
        ranking={"default": RankingProfileConfig()},
    )
    app = FNDApp(index_dir=multi_md_index, config=cfg, collection="notes")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._search.run("prefetch-anchor")
        for _ in range(60):
            await pilot.pause()
            await asyncio.sleep(0.05)
        assert len(app._search.groups) >= 3
        target = app._search.groups[1]
        target_focus = target.hits[0].chunk_seq if target.hits else 0
        app._preview.render_full_doc(target.parent_id, focus_chunk_seq=target_focus)

        def _expected_coverage(total: int, focus_idx: int, radius: int) -> int:
            """Phase 2a+2b coverage: [max(0, focus-r), min(total, focus+r+1))."""
            return min(total, focus_idx + radius + 1) - max(0, focus_idx - radius)

        for _ in range(80):
            await pilot.pause()
            await asyncio.sleep(0.05)
            ap = app._preview.active
            if ap is None:
                continue
            focus_idx = next(
                (
                    i
                    for i, c in enumerate(app._preview.chunk_cache.get(target.parent_id, []))
                    if c.chunk_seq == target_focus
                ),
                0,
            )
            expected = _expected_coverage(ap.total_chunks, focus_idx, BACKGROUND_FILL_RADIUS)
            if len(ap.mounted_indices) >= expected:
                break
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
