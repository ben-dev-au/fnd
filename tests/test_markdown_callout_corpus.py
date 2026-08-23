"""Pin: every callout and inline shape seen in real vaults renders end to end."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from fnd.matching import MatchSpec
from fnd.tui.widgets.markdown import (
    FNDMarkdown,
    FNDMarkdownBlockQuote,
    FNDMarkdownFence,
    FNDMarkdownParagraph,
)

# Shapes taken from the 20 real vault files that carry callouts.
CORPUS = [
    ("> [!warning] Oversized chunks\n> Body.\n", "callout-warning", "▲  Oversized chunks"),
    ("> [!tip] Cap the rows\n> Body.\n", "callout-tip", "◆  Cap the rows"),
    ("> [!info] Context\n> Body.\n", "callout-info", "◉  Context"),
    ("> [!note]\n> Body only.\n", "callout-note", "●  Note"),
    ("> [!example] Worked\n> Body.\n", "callout-example", "▪  Worked"),
    ("> [!quote] Source\n> Body.\n", "callout-quote", '"  Source'),
    ("> [!tip]- Folded\n> Body.\n", "callout-tip", "▾ ◆  Folded"),
    ("> [!TIP] Uppercase\n> Body.\n", "callout-tip", "◆  Uppercase"),
]


class _Host(App[None]):
    def __init__(self, md: str, spec: MatchSpec | None = None) -> None:
        super().__init__()
        self._md = md
        self._spec = spec

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield FNDMarkdown(self._md, match_spec=self._spec)

    def on_mount(self) -> None:
        self.theme = "tokyo-night"


@pytest.mark.asyncio
@pytest.mark.parametrize(("md", "css_class", "title"), CORPUS)
async def test_corpus_callout_renders(md: str, css_class: str, title: str) -> None:
    app = _Host(md)
    async with app.run_test(size=(80, 30)) as pilot:
        await app.query_one(FNDMarkdown).update(md)
        await pilot.pause()
        assert app.query_one(FNDMarkdownBlockQuote).has_class(css_class)
        first = next(iter(app.query(FNDMarkdownParagraph).results()))
        assert first._content.plain == title


@pytest.mark.asyncio
async def test_inline_pass_runs_inside_the_widget() -> None:
    """The inline rewrites reach a mounted block, not just ``collect_edits``."""
    md = "See [[Projects/Alpha|the Alpha note]] and ==this bit== under #uni/web.\n"
    app = _Host(md)
    async with app.run_test(size=(80, 30)) as pilot:
        await app.query_one(FNDMarkdown).update(md)
        await pilot.pause()
        para = next(iter(app.query(FNDMarkdownParagraph).results()))
        assert para._content.plain == "See the Alpha note and this bit under #uni/web."


@pytest.mark.asyncio
async def test_inline_code_keeps_obsidian_syntax_literal() -> None:
    """Markers inside inline code are left alone by the inline pass."""
    md = "Write `==x==` and `[[y]]` verbatim.\n"
    app = _Host(md)
    async with app.run_test(size=(80, 30)) as pilot:
        await app.query_one(FNDMarkdown).update(md)
        await pilot.pause()
        para = next(iter(app.query(FNDMarkdownParagraph).results()))
        assert para._content.plain == "Write ==x== and [[y]] verbatim."


@pytest.mark.asyncio
async def test_fenced_code_is_untouched_by_the_inline_pass() -> None:
    """Fences never run the inline pass, so code keeps its punctuation."""
    md = "```python\nx = a[[0]] + 1  # ==not a mark==\n```\n"
    app = _Host(md)
    async with app.run_test(size=(80, 30)) as pilot:
        widget = app.query_one(FNDMarkdown)
        await widget.update(md)
        await pilot.pause()
        fences = widget.query(FNDMarkdownFence).results()
        rendered = "\n".join(str(f._content.plain) for f in fences)
        assert "a[[0]]" in rendered
        assert "==not a mark==" in rendered
