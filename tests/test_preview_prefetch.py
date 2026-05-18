"""Search-time prefetch warms ``_chunk_cache`` (and, for flat-path
files, ``_prebuilt_cache``) for the top-N results so a cursor move
lands on a pre-warmed cache. The autouse conftest fixture disables
prefetch by default; these tests opt in with their own Config."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from acorn.config import Config, Defaults, RankingProfileConfig
from acorn.index import build_index
from acorn.tui import AcornApp


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
    app = AcornApp(index_dir=two_file_index, config=cfg_with_prefetch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("test")
        # Give the prefetch worker time to walk its sequential targets.
        for _ in range(20):
            await pilot.pause()
            await asyncio.sleep(0.05)
            if app._groups and app._groups[0].parent_id in app._chunk_cache:
                break
        assert app._groups, "search returned no results"
        top = app._groups[0]
        assert (
            top.parent_id in app._chunk_cache
        ), f"prefetch didn't warm {top.parent_id} in _chunk_cache"


@pytest.mark.asyncio
async def test_prefetch_populates_prebuilt_cache_for_flat_files(
    two_file_index: Path, cfg_with_prefetch: Config
) -> None:
    """For flat-path files (PDF / TXT) the prefetch worker also
    pre-builds the FileView + Strips bundle so the user-visible mount
    is instant."""
    app = AcornApp(index_dir=two_file_index, config=cfg_with_prefetch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("results")
        flat_parents: set[str] = set()
        # Drain a few cycles to give the prefetch worker time.
        for _ in range(30):
            await pilot.pause()
            await asyncio.sleep(0.05)
            flat_parents = {
                g.parent_id for g in app._groups if g.path.lower().endswith((".pdf", ".txt"))
            }
            if flat_parents and any(
                (pid, app._current_query_signature()) in app._prebuilt_cache for pid in flat_parents
            ):
                break
        if not flat_parents:
            pytest.skip("no flat-path results in fixture corpus for this query")
        assert any(
            (pid, app._current_query_signature()) in app._prebuilt_cache for pid in flat_parents
        )


@pytest.mark.asyncio
async def test_prefetch_zero_disables(two_file_index: Path) -> None:
    """``preview_prefetch_count=0`` means no prefetch worker is
    spawned at all."""
    cfg = Config(defaults=Defaults(preview_prefetch_count=0, preview_load_debounce_ms=0))
    app = AcornApp(index_dir=two_file_index, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("test")
        await pilot.pause()
        assert not any(w.group == "preview-prefetch" for w in app.workers)


@pytest.mark.asyncio
async def test_query_change_clears_prebuilt_cache(
    two_file_index: Path, cfg_with_prefetch: Config
) -> None:
    """Bundles bake in the query's highlight spans; a new query must
    invalidate them."""
    app = AcornApp(index_dir=two_file_index, config=cfg_with_prefetch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("test")
        # Force a bundle into the cache directly so we don't depend
        # on prefetch timing.
        from acorn.tui.line_buffer import FileView, RenderedDocument

        app._prebuilt_cache[("fake-parent", "old-sig")] = RenderedDocument(fv=FileView())
        app._run_query("different")
        await pilot.pause()
        assert app._prebuilt_cache == {}


@pytest.mark.asyncio
async def test_prefetch_populates_flat_buffer_cache(
    two_file_index: Path, cfg_with_prefetch: Config
) -> None:
    """Prefetch stashes a RenderedDocument in _flat_buffer_cache so the next user
    click installs into the shared widget without a fresh build."""
    import asyncio

    from acorn.tui.line_buffer import RenderedDocument

    app = AcornApp(index_dir=two_file_index, config=cfg_with_prefetch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("results")
        sig = app._current_query_signature()
        flat_parents: set[str] = set()
        for _ in range(40):
            await pilot.pause()
            await asyncio.sleep(0.05)
            flat_parents = {
                g.parent_id for g in app._groups if g.path.lower().endswith((".pdf", ".txt"))
            }
            if flat_parents and any((pid, sig) in app._flat_buffer_cache for pid in flat_parents):
                break
        if not flat_parents:
            pytest.skip("no flat-path results in fixture corpus for this query")
        prefetched = [pid for pid in flat_parents if (pid, sig) in app._flat_buffer_cache]
        assert prefetched, f"prefetch failed to cache any flat doc; flat={flat_parents}"
        for pid in prefetched:
            doc = app._flat_buffer_cache[(pid, sig)]
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
    app = AcornApp(index_dir=multi_md_index, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("prefetch-anchor")
        sig = app._current_query_signature()
        for _ in range(80):
            await pilot.pause()
            await asyncio.sleep(0.05)
            if len(app._groups) >= 3:
                non_top = [g.parent_id for g in app._groups[1:]]
                if all(app._preview_cache.get(pid, sig) is not None for pid in non_top):
                    break
        assert len(app._groups) >= 3, "expected three md results in this corpus"
        non_top = [g.parent_id for g in app._groups[1:]]
        for pid in non_top:
            cont = app._preview_cache.get(pid, sig)
            assert cont is not None, f"prefetch failed to pre-mount {pid}"
            assert "-hidden" in cont.classes, f"prefetched {pid} not hidden"
            assert cont.mounted_indices, f"prefetched {pid} has no mounted chunks"


@pytest.mark.asyncio
async def test_user_selection_of_prefetched_container_runs_to_completion(
    multi_md_index: Path,
) -> None:
    """Selecting a prefetched container completes mount up to the
    background-fill radius (regression for a prefetch/user-side mount
    race that stalled at the visible window — narrower than the radius).
    With ``_BACKGROUND_FILL_RADIUS = 10`` Phase 2a/2b cap mount at
    ``focus +/- 10``; full-file completion would need a wider radius."""
    import asyncio

    from acorn.tui.app import _BACKGROUND_FILL_RADIUS

    cfg = Config(
        defaults=Defaults(preview_prefetch_count=10, preview_load_debounce_ms=0),
        ranking={"default": RankingProfileConfig()},
    )
    app = AcornApp(index_dir=multi_md_index, config=cfg, collection="notes")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._run_query("prefetch-anchor")
        for _ in range(60):
            await pilot.pause()
            await asyncio.sleep(0.05)
        assert len(app._groups) >= 3
        target = app._groups[1]
        target_focus = target.hits[0].chunk_seq if target.hits else 0
        app._render_full_doc(target.parent_id, focus_chunk_seq=target_focus)

        def _expected_coverage(total: int, focus_idx: int, radius: int) -> int:
            """Phase 2a+2b coverage: [max(0, focus-r), min(total, focus+r+1))."""
            return min(total, focus_idx + radius + 1) - max(0, focus_idx - radius)

        for _ in range(80):
            await pilot.pause()
            await asyncio.sleep(0.05)
            ap = app._active_preview
            if ap is None:
                continue
            focus_idx = next(
                (
                    i
                    for i, c in enumerate(app._chunk_cache.get(target.parent_id, []))
                    if c.chunk_seq == target_focus
                ),
                0,
            )
            expected = _expected_coverage(ap.total_chunks, focus_idx, _BACKGROUND_FILL_RADIUS)
            if len(ap.mounted_indices) >= expected:
                break
        ap = app._active_preview
        assert ap is not None, "user-side mount produced no active preview"
        assert ap.parent_doc_id == target.parent_id
        focus_idx = next(
            (
                i
                for i, c in enumerate(app._chunk_cache.get(target.parent_id, []))
                if c.chunk_seq == target_focus
            ),
            0,
        )
        expected = _expected_coverage(ap.total_chunks, focus_idx, _BACKGROUND_FILL_RADIUS)
        assert len(ap.mounted_indices) >= expected, (
            f"user-side mount stalled at {len(ap.mounted_indices)}/{ap.total_chunks} "
            f"(expected at least {expected} from focus +/- {_BACKGROUND_FILL_RADIUS} at idx {focus_idx})"
        )
        pane = app.query_one("#preview_pane")
        placeholders = [w for w in pane.children if getattr(w, "id", None) == "placeholder"]
        assert not placeholders, "placeholder still in pane after preview activated"
