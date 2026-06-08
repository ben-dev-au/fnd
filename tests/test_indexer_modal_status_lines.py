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
