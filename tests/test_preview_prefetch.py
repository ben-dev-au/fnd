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
        app._prebuilt_cache[("fake-parent", "old-sig")] = (None, [], [], [], 0, 1)  # type: ignore[arg-type]
        app._run_query("different")
        await pilot.pause()
        assert app._prebuilt_cache == {}
