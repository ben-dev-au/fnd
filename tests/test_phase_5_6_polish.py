"""Phase 5.6: visible highlights, precise scroll, Skim search, Esc, theme."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rich.text import Text

from acorn import opener
from acorn.extract.base import Block
from acorn.index import build_index
from acorn.query import FileChunk
from acorn.render import render_document_rich
from acorn.tui import AcornApp

# ── Visible highlights via Rich Text styling ─────────────────────────


def test_render_document_rich_returns_text_and_offsets() -> None:
    chunks = [
        FileChunk(
            parent_id="x",
            path="/x.pdf",
            kind="pdf",
            page=1,
            slide=0,
            heading_path="",
            chunk_seq=0,
            blocks=[Block(kind="p", text="alpha bravo charlie")],
        ),
        FileChunk(
            parent_id="x",
            path="/x.pdf",
            kind="pdf",
            page=2,
            slide=0,
            heading_path="",
            chunk_seq=1,
            blocks=[Block(kind="p", text="alpha delta echo")],
        ),
    ]
    text, offsets = render_document_rich(chunks, query="alpha")
    assert isinstance(text, Text)
    # offsets maps chunk_seq → line offset; chunk 0 starts at line 0, chunk 1
    # starts later (after some lines for the first chunk).
    assert offsets[0] == 0
    assert offsets[1] > offsets[0]


def test_render_document_rich_highlights_every_match_with_explicit_style() -> None:
    chunks = [
        FileChunk(
            parent_id="x",
            path="/x.pdf",
            kind="pdf",
            page=p,
            slide=0,
            heading_path="",
            chunk_seq=p - 1,
            blocks=[Block(kind="p", text=f"page {p} mentions susy here")],
        )
        for p in range(1, 4)
    ]
    text, _offsets = render_document_rich(chunks, query="susy")
    plain = text.plain
    # Three "susy" occurrences across three chunks.
    assert plain.lower().count("susy") == 3
    # Each gets a highlight span with our explicit on-yellow style.
    susy_spans = [sp for sp in text.spans if plain[sp.start : sp.end].lower() == "susy"]
    assert len(susy_spans) == 3
    for sp in susy_spans:
        assert "on #ffd866" in str(sp.style)


# ── Skim search URL ──────────────────────────────────────────────────


def test_skim_url_includes_search_fragment(tmp_path: Path) -> None:
    f = tmp_path / "wine.pdf"
    f.touch()
    url = opener.skim_url(f, page=42, search="Yalumba")
    assert "#page=42" in url
    assert "&search=Yalumba" in url


def test_skim_url_search_is_percent_encoded(tmp_path: Path) -> None:
    f = tmp_path / "doc.pdf"
    f.touch()
    url = opener.skim_url(f, page=1, search="hello world & friends")
    assert "search=hello%20world" in url
    assert "%26" in url  # the &


def test_open_smart_routes_to_url_when_query_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a non-empty query, open_smart should use the URL form (which
    supports &search=) rather than AppleScript."""
    f = tmp_path / "doc.pdf"
    f.touch()
    monkeypatch.setattr(opener, "_has_skim", lambda: True)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        opener,
        "open_pdf_via_url",
        lambda path, page, *, search="": calls.append({"strategy": "url", "search": search}) or 0,
    )
    monkeypatch.setattr(
        opener,
        "open_pdf_via_applescript",
        lambda path, page: calls.append({"strategy": "applescript"}) or 0,
    )
    opener.open_smart(path=f, kind="pdf", page=7, query="Yalumba")
    assert calls == [{"strategy": "url", "search": "Yalumba"}]


def test_open_smart_uses_applescript_when_query_blank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "doc.pdf"
    f.touch()
    monkeypatch.setattr(opener, "_has_skim", lambda: True)
    calls: list[str] = []
    monkeypatch.setattr(
        opener,
        "open_pdf_via_applescript",
        lambda path, page: calls.append("applescript") or 0,
    )
    monkeypatch.setattr(
        opener,
        "open_pdf_via_url",
        lambda path, page, *, search="": calls.append("url") or 0,
    )
    opener.open_smart(path=f, kind="pdf", page=7, query="")
    assert calls == ["applescript"]


# ── TUI: query plumbed to opener ────────────────────────────────────


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_tui_passes_query_to_opener_for_skim_search(
    built_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []

    def fake_open_smart(
        *, path: Path, kind: str, page: int = 0, query: str = "", **_kw: Any
    ) -> int:
        seen.append({"path": str(path), "kind": kind, "page": page, "query": query})
        return 0

    monkeypatch.setattr(opener, "open_smart", fake_open_smart)

    app = AcornApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Tree

        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

    assert seen
    assert seen[-1]["query"] == "blue penguin sandwich", seen[-1]


# ── Esc dismisses overlays ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_escape_dismisses_help_overlay(built_index: Path) -> None:
    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        assert app.query("#help_overlay")
        await pilot.press("escape")
        await pilot.pause()
        assert not app.query("#help_overlay")


@pytest.mark.asyncio
async def test_escape_dismisses_command_palette(built_index: Path) -> None:
    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        assert app.query("#cmd_palette")
        await pilot.press("escape")
        await pilot.pause()
        assert not app.query("#cmd_palette")


# ── Theme applied ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_theme_is_set_on_mount(built_index: Path) -> None:
    """Confirm the muted blue/teal pastel theme (tokyo-night) is applied."""
    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "tokyo-night"


# ── Precise scroll-to-chunk ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_scroll_offsets_recorded_per_chunk(built_index: Path) -> None:
    """After rendering a multi-chunk PDF, the offset map should contain a
    row for every chunk_seq, with strictly increasing line numbers."""
    app = AcornApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Tree

        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        await pilot.pause()

        parent_ids = list(app._chunk_offsets)
        assert parent_ids
        offsets = app._chunk_offsets[parent_ids[0]]
        # All 12 PDF pages should be represented.
        assert len(offsets) == 12
        # Line numbers strictly increase with chunk_seq.
        sorted_seqs = sorted(offsets)
        last_line = -1
        for seq in sorted_seqs:
            assert (
                offsets[seq] > last_line
            ), f"chunk {seq} offset {offsets[seq]} not greater than {last_line}"
            last_line = offsets[seq]
