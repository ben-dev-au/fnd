"""Indexer modal: Indexed + Texturising status-line formatting."""

from __future__ import annotations

from fnd.tui.indexer_modal import _format_indexed_line, _format_texturising_line


def test_indexed_line_drops_failed_tail_when_zero() -> None:
    out = _format_indexed_line(newly=5, already=8, failed=0)
    assert "5 newly indexed" in out
    assert "8 already indexed" in out
    assert "failed" not in out
    assert "⚠" not in out


def test_indexed_line_shows_failed_when_nonzero() -> None:
    out = _format_indexed_line(newly=5, already=8, failed=1)
    assert "⚠ 1 failed" in out


def test_texturising_line_drops_still_flat_when_zero() -> None:
    out = _format_texturising_line(newly=4, already=2, still_flat=0)
    assert "4 newly textured" in out
    assert "2 already textured" in out
    assert "still flat" not in out
    assert "⚠" not in out


def test_texturising_line_shows_still_flat_when_nonzero() -> None:
    out = _format_texturising_line(newly=4, already=2, still_flat=1)
    assert "⚠ 1 still flat" in out


def test_todo_scope_single_update_is_the_active_collection() -> None:
    """The Flat-PDFs badge count, its background refresh, and the drill-in it
    opens must all share one scope so the number matches what's shown."""
    from fnd.tui.indexer_modal import IndexerScreen

    single = IndexerScreen("default", chain_total=1)
    assert single._todo_scope() == "default"


def test_todo_scope_mid_chain_is_all_collections() -> None:
    from fnd.tui.indexer_modal import IndexerScreen

    chain = IndexerScreen("default", chain_total=3)
    assert chain._todo_scope() is None


def test_current_line_shows_the_ordinary_file_when_nothing_is_being_fetched() -> None:
    from fnd.tui.indexer_modal import _format_current_line

    out = _format_current_line(wait=None, current_path="/a/b/Week 7 Notes.md", stuck_suffix="")
    assert "Current:" in out
    assert "Week 7 Notes.md" in out
    assert "Fetching" not in out


def test_current_line_names_the_provider_and_wait_while_fetching() -> None:
    """The line that turns a multi-minute cloud download from "the app is
    frozen" into "it's waiting on iCloud Drive, and for how long"."""
    import time

    from fnd.cloud_files import FetchWait
    from fnd.tui.indexer_modal import _format_current_line

    wait = FetchWait(
        path="/a/b/Week 7 Notes.md",
        provider="iCloud Drive",
        started_monotonic=time.monotonic() - 9.0,
    )
    out = _format_current_line(
        wait=wait, current_path="/a/b/other.md", stuck_suffix="   · stuck 3s"
    )
    assert "Fetching from iCloud Drive" in out
    assert "Week 7 Notes.md" in out
    assert "waiting 9s" in out
    # The fetch owns the line — the unrelated per-page stall tag would be
    # misleading while the extractor hasn't even been handed the file.
    assert "stuck" not in out
