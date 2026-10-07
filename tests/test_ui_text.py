"""``fnd.tui.ui_text``: variable text reaches a widget literal and display-safe."""

from __future__ import annotations

import pytest
from textual.app import ComposeResult
from textual.content import Content
from textual.widgets import OptionList
from textual.widgets._toast import Toast

from fnd.display_text import display_block, display_line
from fnd.tui.ui_text import (
    PlainOptionList,
    PlainStatic,
    PlainToastApp,
    PlainTree,
    literal,
    set_border_subtitle,
    set_border_title,
    ui_block,
    ui_text,
)
from tests import _hostile_text
from tests._pilot_wait import wait_until


@pytest.mark.parametrize("raw", _hostile_text.ALL)
def test_a_value_shows_literally_as_one_display_line(raw: str) -> None:
    assert ui_text("Renamed to $name", name=raw).plain == f"Renamed to {display_line(raw)}"


@pytest.mark.parametrize("raw", _hostile_text.ALL)
def test_literal_shows_any_text_as_one_display_line(raw: str) -> None:
    assert literal(raw).plain == display_line(raw)


def test_the_template_keeps_its_styling() -> None:
    content = ui_text("[b]$name[/b] kept", name="[i]x[/i]")
    assert content.plain == "[i]x[/i] kept"
    assert [(span.start, span.end) for span in content.spans] == [(0, 8)]


def test_a_block_value_keeps_its_line_breaks() -> None:
    assert ui_block("Failed:\n$why", why="a\r\nb\x1b").plain == "Failed:\na\nb"


def test_a_value_that_is_not_text_shows_as_text() -> None:
    assert ui_text("$count files", count=3).plain == "3 files"


def _plain(visual: object) -> str:
    assert isinstance(visual, Content)
    return visual.plain


@pytest.mark.parametrize("raw", _hostile_text.ALL)
def test_a_static_shows_any_text_literally(raw: str) -> None:
    assert _plain(PlainStatic(raw).visual) == display_block(raw)
    static = PlainStatic()
    static.update(raw)
    assert _plain(static.visual) == display_block(raw)


def test_a_static_keeps_styled_content() -> None:
    styled = ui_text("[b]$name[/b]", name="x")
    assert PlainStatic(styled).visual is styled


@pytest.mark.parametrize("raw", _hostile_text.ALL)
def test_a_tree_label_shows_any_text_literally(raw: str) -> None:
    tree = PlainTree[None](raw)
    node = tree.root.add(raw)
    assert str(tree.root.label) == display_line(raw)
    assert str(node.label) == display_line(raw)
    node.set_label(raw)
    assert str(node.label) == display_line(raw)


@pytest.mark.parametrize("raw", _hostile_text.ALL)
def test_an_option_shows_any_text_literally(raw: str) -> None:
    options = PlainOptionList(raw)
    options.add_option(raw)
    for index in (0, 1):
        option = options.get_option_at_index(index)
        assert option.prompt == raw
        assert _plain(options._get_visual(option)) == display_block(raw)


@pytest.mark.parametrize("raw", _hostile_text.ALL)
def test_a_border_title_shows_any_text_literally(raw: str) -> None:
    widget = PlainStatic()
    set_border_title(widget, raw)
    set_border_subtitle(widget, raw)
    assert _plain(widget._border_title) == display_line(raw)
    assert _plain(widget._border_subtitle) == display_line(raw)


def test_a_border_title_keeps_styled_content_and_clears() -> None:
    widget = PlainStatic()
    set_border_title(widget, ui_text("[b]$name[/b]", name="x"))
    assert isinstance(widget._border_title, Content)
    assert widget._border_title.spans
    set_border_title(widget, None)
    assert widget._border_title is None


def test_an_option_list_without_the_seam_still_shows_markup_literally() -> None:
    options = PlainOptionList("Invoice [PAID] a [/] b")
    option = options.get_option_at_index(0)
    assert _plain(OptionList._get_visual(options, option)) == "Invoice [PAID] a [/] b"


class _Probe(PlainToastApp):
    def compose(self) -> ComposeResult:
        yield PlainStatic(id="static")


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", _hostile_text.ALL)
async def test_a_toast_shows_any_message_and_title_literally(raw: str) -> None:
    app = _Probe()
    async with app.run_test(notifications=True) as pilot:
        app.notify(raw, title=raw)
        await wait_until(pilot, lambda: bool(app.screen.query(Toast)), message="no toast mounted")
        shown = app.screen.query_one(Toast).render().plain
        assert shown == f"{display_line(raw)}\n{display_block(raw)}"


@pytest.mark.parametrize("raw", _hostile_text.ALL)
def test_a_tri_state_row_shows_any_name_literally(raw: str) -> None:
    from fnd.tui.results_labels import _styled_state_row

    assert _styled_state_row("●", f"  {raw}", "red").plain == f"●  {display_line(raw)}"
