"""OutlineView's bookkeeping, against a stand-in app: builds, staleness, title."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from fnd.extract.base import Block
from fnd.query import FileChunk
from fnd.tui.outline_model import Outline
from fnd.tui.outline_view import OutlineView


class _Tree:
    def __init__(self) -> None:
        self.outline = Outline()
        self.border_title = ""
        self.has_focus = False
        self.classes: set[str] = set()

    def set_outline(self, outline: Outline, placeholder: str = "") -> None:
        del placeholder
        self.outline = outline

    def cursor_index(self) -> int | None:
        return None

    def place_cursor(self, index: int | None) -> None:
        del index


class _Preview:
    def __init__(self) -> None:
        self.parent_id: str | None = None
        self.shown: str | None = None
        self.painted = True
        self.busy = True  # keeps the scroll follow out of most of these tests
        self.outgoing: object | None = None
        self.target: tuple[str, int] | None = None
        self.jumps: list[tuple[str, int, int]] = []
        self.chunk_cache: dict[str, list[FileChunk]] = {}

    def is_painted(self) -> bool:
        return self.painted

    def pipeline_busy(self) -> bool:
        return self.busy

    def cursor_target(self) -> tuple[str, int] | None:
        return self.target

    def jump_to_chunk_top(self, parent_id: str, chunk_seq: int, heading: int) -> None:
        self.jumps.append((parent_id, chunk_seq, heading))

    def showing_parent(self) -> str | None:
        return self.shown


class _App:
    def __init__(self) -> None:
        self.tree = _Tree()
        self._preview = _Preview()
        self._scope = SimpleNamespace(collapsed_marker=lambda _id: "")
        self._preview_scroll = SimpleNamespace(anchor=None)
        self._reading_mode = False
        self.workers: list[Any] = []
        self.notices: list[str] = []

    def query_one(self, *_args: object) -> _Tree:
        return self.tree

    def run_worker(self, work: Any, **_kw: object) -> None:
        self.workers.append(work)

    def notify(self, message: str, **_kw: object) -> None:
        self.notices.append(message)


def _doc(parent_id: str, *titles: str) -> list[FileChunk]:
    return [
        FileChunk(
            parent_id=parent_id,
            path=f"/tmp/{parent_id}.md",
            kind="md",
            page=0,
            slide=0,
            heading_path=title,
            chunk_seq=i,
            blocks=[Block("h2", title)],
        )
        for i, title in enumerate(titles)
    ]


def _show(app: _App, parent_id: str, *, painted: bool = True) -> None:
    app._preview.parent_id = parent_id
    app._preview.shown = parent_id if painted else app._preview.shown
    app._preview.painted = painted


def test_a_build_superseded_mid_flight_does_not_block_its_file_for_good() -> None:
    app = _App()
    view = OutlineView(app)  # type: ignore[arg-type]
    app._preview.chunk_cache = {"a": _doc("a", "Alpha"), "b": _doc("b", "Beta")}
    _show(app, "a")
    view.sync()
    assert len(app.workers) == 1
    _show(app, "b", painted=False)  # b activates, not yet revealed
    asyncio.run(app.workers[0])  # a's build lands after the preview has moved on
    assert app.tree.outline.entries == ()
    _show(app, "a")  # back to a, served from cache
    view.sync()
    assert len(app.workers) == 2, "a's stale build still claimed the file"
    asyncio.run(app.workers[1])
    assert [e.title for e in app.tree.outline.entries] == ["Alpha"]


def test_the_title_counts_headings_in_the_singular_too() -> None:
    app = _App()
    view = OutlineView(app)  # type: ignore[arg-type]
    app._preview.chunk_cache = {"a": _doc("a", "Only")}
    _show(app, "a")
    view.sync()
    asyncio.run(app.workers[0])
    assert app.tree.border_title == "Outline: 1 heading"


class _Scroller:
    def __init__(self) -> None:
        self.scroll_offset = SimpleNamespace(y=40)
        self.max_scroll_y = 400
        self.size = SimpleNamespace(height=40)


def _following_app() -> tuple[_App, OutlineView, list[int | None]]:
    app = _App()
    app._reading_mode = False
    scroller = _Scroller()
    app._flat = SimpleNamespace(active_buffer=scroller)  # type: ignore[attr-defined]
    app.animator = SimpleNamespace(is_being_animated=lambda *_a: False)  # type: ignore[attr-defined]
    app._preview.busy = False
    app._preview_scroll = SimpleNamespace(
        anchor=None,
        is_settling=False,
        reading_position=lambda _f: (0 if scroller.scroll_offset.y < 60 else 1, 0),
        chunk_heading_rows=lambda _s: None,
        landing_target=lambda: None,
        view_row=lambda _seq, _row: None,
    )
    view = OutlineView(app)  # type: ignore[arg-type]
    app._preview.chunk_cache = {"a": _doc("a", "Alpha", "Beta")}
    _show(app, "a")
    view.sync()
    asyncio.run(app.workers[0])
    placed: list[int | None] = []
    view._place = lambda _tree, index: placed.append(index)  # type: ignore[method-assign]
    app._flat.active_buffer.scroll_offset.y = 80  # type: ignore[attr-defined]  # the reader scrolls
    return app, view, placed


def test_the_reader_is_followed_on_the_file_the_tree_outlines() -> None:
    _app, view, placed = _following_app()
    view.follow()
    assert placed == [1], "the control: a scroll on the outlined file is followed"


def test_positions_on_another_file_never_move_the_cursor() -> None:
    app, view, placed = _following_app()
    app._preview.shown = "b"  # the preview has moved on; b's outline is still building
    view.follow()
    assert placed == []


def test_a_scroll_still_gliding_is_not_sampled() -> None:
    app, view, placed = _following_app()
    app.animator = SimpleNamespace(is_being_animated=lambda *_a: True)  # type: ignore[attr-defined]
    view.follow()
    assert placed == []


def test_a_new_navigation_is_not_sampled_before_its_scroll_commits() -> None:
    app, view, placed = _following_app()
    app._preview_scroll.anchor = SimpleNamespace(  # a navigation arms
        parent_id="a", focus_chunk_seq=0, intent="chunk_top", heading=0
    )
    view.follow()  # sees the new anchor and places its target
    app._preview_scroll.is_settling = True  # its scroll has not committed yet
    app._flat.active_buffer.scroll_offset.y = 20  # type: ignore[attr-defined]
    view.follow()
    assert placed == [0], "sampled the position the navigation is leaving"


def _outlined(*titles: str) -> tuple[_App, OutlineView]:
    app = _App()
    view = OutlineView(app)  # type: ignore[arg-type]
    app._preview.chunk_cache = {"a": _doc("a", *titles), "b": _doc("b", "Beta")}
    _show(app, "a")
    view.sync()
    asyncio.run(app.workers[0])
    return app, view


def test_a_hidden_outline_builds_nothing_until_it_is_shown_again() -> None:
    for hide in ("collapsed", "reading"):
        app, view = _outlined("Alpha")
        if hide == "collapsed":
            app.tree.classes.add("collapsed")
        else:
            app._reading_mode = True
        _show(app, "b")
        view.sync()
        assert len(app.workers) == 1, hide
        assert app.tree.outline.entries == (), f"{hide}: kept another file's headings"
        app.tree.classes.discard("collapsed")
        app._reading_mode = False
        view.sync()
        asyncio.run(app.workers[1])
        assert [e.title for e in app.tree.outline.entries] == ["Beta"], hide


def test_a_rerender_keeps_the_tree_while_the_preview_rebuilds() -> None:
    app, view = _outlined("Alpha")
    app._preview.parent_id = None  # a re-render (`h`) clears the preview first
    view.sync()
    assert [e.title for e in app.tree.outline.entries] == ["Alpha"]
    app._preview.busy = False  # nothing is coming: the preview is empty
    view.sync()
    assert app.tree.outline.entries == ()


def test_a_refused_jump_says_why() -> None:
    app, view = _outlined("Alpha", "Omega")
    preview = app._preview
    preview.target = ("a", 0)
    cases = [
        ("outgoing", "The preview is still loading."),
        ("unpainted", "The preview is still loading."),
        ("other file shown", "The outline is still catching up with the preview."),
        ("cursor elsewhere", "The Results cursor is on another file."),
    ]
    for case, notice in cases:
        preview.outgoing = object() if case == "outgoing" else None
        preview.painted = case != "unpainted"
        preview.shown = "b" if case == "other file shown" else "a"
        preview.target = ("b", 0) if case == "cursor elsewhere" else ("a", 0)
        app.notices.clear()
        assert view.jump(1) is False, case
        assert app.notices == [notice], case
    preview.outgoing, preview.painted, preview.shown, preview.target = None, True, "a", ("a", 0)
    assert view.jump(1) is True
    assert preview.jumps == [("a", 1, 0)]


def _held_short(y: int) -> tuple[OutlineView, list[int | None]]:
    """A landing on Beta 30 rows down a 40-row view (20 below the reading line),
    scrolled to ``y`` of at most 20."""
    app, view, placed = _following_app()
    app._preview_scroll.anchor = SimpleNamespace(
        parent_id="a", focus_chunk_seq=1, intent="first_match", heading=-1
    )
    view.follow()  # the navigation's own placement
    placed.clear()
    scroller = app._flat.active_buffer  # type: ignore[attr-defined]
    scroller.max_scroll_y = 20
    scroller.scroll_offset.y = y
    app._preview_scroll.landing_target = lambda: (1, None)
    app._preview_scroll.view_row = lambda _seq, _row: 30
    view.follow()
    return view, placed


@pytest.mark.parametrize(("y", "held"), [(20, True), (19, True), (15, False), (0, False)])
def test_the_target_is_the_reader_only_while_the_view_rests_against_the_end(
    y: int, held: bool
) -> None:
    """At the end, or a restore's row short of it; not once the reader scrolls away."""
    _view, placed = _held_short(y)
    assert placed == ([1] if held else [])


def test_the_reader_is_tracked_while_browsing_and_the_cursor_follows_on_leaving() -> None:
    app, view, placed = _following_app()
    app.tree.has_focus = True
    view.follow()
    app._flat.active_buffer.scroll_offset.y = 20  # type: ignore[attr-defined]
    view.follow()
    assert (placed, view._reader) == ([], 0)
    app.tree.has_focus = False
    view.follow()
    assert placed == [0]
