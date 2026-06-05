"""Stale-searcher regression: a reindex that commits new chunks while
the app is open must surface those chunks on the next query, without an
app restart.

The live ``Searcher`` captures a Tantivy searcher against the index
generation it was opened at; after a reindex commits, that snapshot is
stale until refreshed. ``Searcher.reload()`` re-points at the latest
generation, and ``_run_query`` calls it so new files appear on the very
next query (matching the user's expectation).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import Config, load
from fnd.index import build_index
from fnd.query import Searcher
from fnd.tui import FNDApp


def _write_md(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    notes_dir = (tmp_path / "notes").as_posix()
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.notes.sources]]
            path = "{notes_dir}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


def test_searcher_reload_picks_up_new_commit(tmp_path: Path, tmp_index_dir: Path) -> None:
    """A Searcher opened before a commit misses the new doc until
    reload() re-points it at the latest generation."""
    docs = tmp_path / "notes"
    _write_md(docs / "first.md", "# First\nalpha content here.\n")
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="notes")

    searcher = Searcher(index_dir=tmp_index_dir)
    assert searcher.search("alpha", limit=10, collection="notes")

    # Reindex with a second doc — commits a new generation.
    _write_md(docs / "second.md", "# Second\nbravo zero seconds marker.\n")
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="notes")

    # Stale snapshot: the new doc is invisible.
    assert not searcher.search("bravo", limit=10, collection="notes")

    # reload() re-points at the committed generation.
    searcher.reload()
    assert searcher.search("bravo", limit=10, collection="notes")


def test_get_file_chunks_survives_concurrent_reload(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Regression: a reload() that swaps self._searcher (to a new
    generation) mid-decode must not corrupt an in-flight
    get_file_chunks(). Tantivy DocAddresses are generation-specific, so
    decoding G1 addresses against a swapped-in G2 searcher would yield
    garbage or panic; get_file_chunks pins one generation for the whole
    search→decode sequence to prevent that.
    """
    import threading

    docs = tmp_path / "notes"
    # >= _PARALLEL_DECODE_THRESHOLD (50) sections so the ThreadPool decode
    # path runs — that's where the cross-thread swap is dangerous.
    body = "# Doc\n" + "".join(
        f"\n## Section {i}\nzero seconds marker body for section {i}.\n" for i in range(80)
    )
    _write_md(docs / "big.md", body)
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="notes")

    searcher = Searcher(index_dir=tmp_index_dir)
    hits = searcher.search("marker", limit=5, collection="notes")
    assert hits, "baseline search should find the doc"
    parent_id = hits[0].parent_id

    stop = threading.Event()
    errors: list[BaseException] = []

    def churn_generations() -> None:
        # Commit fresh generations and reload the shared searcher, racing
        # the foreground decode loop.
        try:
            for n in range(6):
                if stop.is_set():
                    return
                _write_md(docs / f"extra_{n}.md", f"# Extra {n}\nzero seconds extra {n}.\n")
                build_index(roots=[docs], index_dir=tmp_index_dir, collection="notes")
                searcher.reload()
        except BaseException as e:
            errors.append(e)

    churn = threading.Thread(target=churn_generations, daemon=True)
    churn.start()
    try:
        while churn.is_alive():
            chunks = searcher.get_file_chunks(parent_id, max_workers=8)
            assert chunks, "decode returned no chunks"
            # A cross-generation leak would surface as a mismatched
            # parent_id or undecodable body.
            assert all(c.parent_id == parent_id for c in chunks), "cross-generation doc leaked in"
            assert all(c.body_md or c.blocks for c in chunks), "chunk body failed to decode"
    finally:
        stop.set()
        churn.join(timeout=10)
    assert not errors, f"generation churn thread raised: {errors!r}"


@pytest.mark.asyncio
async def test_new_file_appears_on_next_query_without_restart(
    cfg: Config, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """User scenario: with the app open, a new file is indexed; the very
    next query surfaces it — no restart needed."""
    docs = tmp_path / "notes"
    _write_md(docs / "first.md", "# First\nalpha content here.\n")
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="notes")

    app = FNDApp(index_dir=tmp_index_dir, config=cfg, collection="notes")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._run_query("alpha")
        await pilot.pause()
        assert app._groups, "baseline doc should be found"

        # Index a new file while the app is running (mirrors an in-app or
        # external reindex committing a new generation).
        _write_md(docs / "second.md", "# Second\nbravo zero seconds marker.\n")
        build_index(roots=[docs], index_dir=tmp_index_dir, collection="notes")

        app._run_query("bravo")
        await pilot.pause()
        assert app._groups, "newly-indexed file must appear on the next query"
        assert app._groups[0].path.endswith("second.md")
