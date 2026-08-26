"""Phase 5.6: visible highlights, precise scroll, Skim search, Esc, theme."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rich.text import Text

from fnd import opener
from fnd.extract.base import Block
from fnd.index import build_index
from fnd.query import FileChunk
from fnd.render import render_document_rich
from fnd.tui import FNDApp
from tests._pilot_wait import preview_landed, settings_ready

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
    """With a non-empty query, open_smart hands Skim a URL whose
    ``search=`` fragment makes Skim highlight the matching string."""
    from fnd.config import Config

    f = tmp_path / "doc.pdf"
    f.touch()
    monkeypatch.setattr("sys.platform", "darwin")  # Skim auto-promote is macOS-only
    monkeypatch.setattr(opener, "_has_skim", lambda: True)
    # Isolate from the developer's real config — without this the resolver
    # routes through whichever [app_defaults] they have set (eg. `preview`)
    # and the test fires a real `osascript`, opening Preview against the
    # empty tmp file.
    monkeypatch.setattr("fnd.config.load", lambda *a, **kw: Config())
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        opener,
        "open_pdf_via_url",
        lambda path, page, *, search="": calls.append({"strategy": "url", "search": search}) or 0,
    )
    opener.open_smart(path=f, kind="pdf", page=7, query="Yalumba")
    assert calls == [{"strategy": "url", "search": "Yalumba"}]


def test_open_smart_uses_url_when_query_blank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Query-less PDF opens still use the Skim URL form — the AppleScript
    path was removed because filename control-char injection through the
    AppleScript string literal could execute arbitrary `osascript`."""
    from fnd.config import Config

    f = tmp_path / "doc.pdf"
    f.touch()
    monkeypatch.setattr("sys.platform", "darwin")  # Skim auto-promote is macOS-only
    monkeypatch.setattr(opener, "_has_skim", lambda: True)
    monkeypatch.setattr("fnd.config.load", lambda *a, **kw: Config())
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        opener,
        "open_pdf_via_url",
        lambda path, page, *, search="": calls.append({"strategy": "url", "search": search}) or 0,
    )
    opener.open_smart(path=f, kind="pdf", page=7, query="")
    assert calls == [{"strategy": "url", "search": ""}]


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

    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Tree

        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        # Phase 5.7: explicit `o` action is the open trigger now;
        # plain Enter / click only updates the preview.
        app.action_open_at_locator()
        await pilot.pause()

    assert seen
    assert seen[-1]["query"] == "blue penguin sandwich", seen[-1]


# ── Esc dismisses overlays ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_escape_closes_help_menu(built_index: Path) -> None:
    """Help (`?`) opens the Settings menu pre-navigated to Keybindings;
    Esc walks the user back to the main app."""
    from fnd.tui.settings_screen import SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        # Pop Keybindings → root → main app.
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SettingsScreen)


@pytest.mark.asyncio
async def test_escape_closes_settings_menu(built_index: Path) -> None:
    """`:` opens the unified Settings menu; Esc closes it."""
    from fnd.tui.settings_screen import SettingsScreen

    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await settings_ready(pilot, app)
        assert isinstance(app.screen, SettingsScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SettingsScreen)


# ── Theme applied ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_theme_is_set_on_mount(built_index: Path) -> None:
    """Confirm the muted blue/teal pastel theme (tokyo-night) is applied."""
    app = FNDApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "tokyo-night"


# ── Precise scroll-to-chunk ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_chunk_widgets_mounted_per_pdf_page(
    built_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 5 model: PDFs mount through the flat-buffer pipeline.
    The widget owns a FileView whose ``chunk_to_range`` covers every
    page; the focused chunk's first-match line is the scroll target.

    Forces flat preview routing — when the pdf-structure extra is
    installed in the dev venv, PDFs carry body_md and would otherwise
    route structural. The Phase 5 flat-pipeline contract is what this
    test asserts; structural routing has its own coverage."""
    monkeypatch.setenv("_FND_FORCE_FLAT", "1")
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import Tree

        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        first.expand()
        await pilot.pause()
        tree.focus()
        await pilot.press("down")
        await preview_landed(pilot, app)

        buf = app._flat.active_buffer
        assert buf is not None, "PDF should mount the flat-buffer preview"
        fv = buf.file_view
        assert fv is not None
        # 12 PDF pages → 12 chunk ranges, keyed by chunk_seq 0..11.
        assert set(fv.chunk_to_range) == set(range(12))
        # The focused chunk has a recorded first-match line.
        assert fv.first_hit_line_in_chunk, fv.first_hit_line_in_chunk
