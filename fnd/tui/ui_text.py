"""How variable text reaches a Textual widget: literal, and display-safe.

Textual parses a plain ``str`` as markup, so a file called ``notes [draft].md``
renders as ``notes .md`` and a collection called ``Q3 [growth=5%]`` raises
``MarkupError`` at paint time, taking the app down. ``escape()`` does not cure
it: Textual's own parser still eats ``[PAID]`` and ``[$accent]``. So in fnd a
``str`` is never markup. The widgets here show any ``str`` literally, passed
through :mod:`fnd.display_text`; styling is explicit, as :class:`Content` built
by :func:`ui_text` from a markup *template* whose ``$name`` slots are filled
literally. ``tests/test_markup_sinks.py`` keeps every sink on this path.
"""

from __future__ import annotations

from typing import LiteralString

from rich.text import Text, TextType
from textual.app import App
from textual.content import Content
from textual.notifications import SeverityLevel
from textual.visual import Visual, VisualType
from textual.widget import Widget
from textual.widgets import OptionList, Static, Tree
from textual.widgets._option_list import OptionListContent
from textual.widgets.option_list import Option

from fnd.display_text import display_block, display_line
from fnd.tui.widgets.arrow_expansion import ArrowsExpand

__all__ = [
    "PlainOptionList",
    "PlainStatic",
    "PlainToastApp",
    "PlainTree",
    "literal",
    "set_border_subtitle",
    "set_border_title",
    "ui_block",
    "ui_text",
]


def literal(text: str) -> Content:
    """``text`` shown as it is, as a :func:`display_line`."""
    return Content(display_line(text))


def ui_text(template: LiteralString, /, **values: object) -> Content:
    """``template`` markup with each ``$name`` filled literally by
    ``values[name]`` as a :func:`display_line`."""
    return Content.from_markup(template, **{k: display_line(str(v)) for k, v in values.items()})


def ui_block(template: LiteralString, /, **values: object) -> Content:
    """:func:`ui_text` for multi-line slots: each value a :func:`display_block`."""
    return Content.from_markup(template, **{k: display_block(str(v)) for k, v in values.items()})


def set_border_title(widget: Widget, title: str | Content | None) -> None:
    """Title ``widget``'s border: a ``str`` literally, Content as styled."""
    widget.border_title = literal(title) if isinstance(title, str) else title


def set_border_subtitle(widget: Widget, subtitle: str | Content | None) -> None:
    """:func:`set_border_title` for the bottom edge."""
    widget.border_subtitle = literal(subtitle) if isinstance(subtitle, str) else subtitle


def _as_block(content: VisualType) -> VisualType:
    return Content(display_block(content)) if isinstance(content, str) else content


class PlainToastApp(App[None]):
    """An App whose toasts are plain text: messages name files, collections and
    errors, so ``markup`` is ignored and nothing is parsed."""

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        del markup
        super().notify(
            display_block(message),
            title=display_line(title),
            severity=severity,
            timeout=timeout,
            markup=False,
        )


class PlainStatic(Static):
    """A Static that shows a ``str`` literally, as a :func:`display_block`."""

    def __init__(
        self,
        content: VisualType = "",
        *,
        expand: bool = False,
        shrink: bool = False,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            _as_block(content),
            expand=expand,
            shrink=shrink,
            markup=False,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )

    def update(self, content: VisualType = "", *, layout: bool = True) -> None:
        super().update(_as_block(content), layout=layout)


class PlainTree[T](ArrowsExpand, Tree[T]):
    """fnd's Tree: a ``str`` label is literal :func:`display_line` text, and
    space does not expand (see :class:`ArrowsExpand`)."""

    def process_label(self, label: TextType) -> Text:
        return super().process_label(Text(display_line(label)) if isinstance(label, str) else label)


class PlainOptionList(OptionList):
    """An OptionList that renders a ``str`` prompt literally, as a
    :func:`display_block`; ``option.prompt`` keeps the value it was given."""

    def __init__(
        self,
        *content: OptionListContent,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
        compact: bool = False,
    ) -> None:
        # Markup off as well: _get_visual is private, and this keeps an upgrade
        # that stops calling it literal rather than parsing.
        super().__init__(
            *content,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
            markup=False,
            compact=compact,
        )

    def _get_visual(self, option: Option) -> Visual:
        if option._visual is None and isinstance(option.prompt, str):
            option._visual = Content(display_block(option.prompt))
        return super()._get_visual(option)
