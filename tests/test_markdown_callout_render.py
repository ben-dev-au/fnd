"""Pin: callouts render as classed blockquotes with an icon-prefixed title."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.color import Color
from textual.containers import VerticalScroll

from fnd.matching import MatchSpec
from fnd.tui.widgets.markdown import (
    FNDMarkdown,
    FNDMarkdownBlockQuote,
    FNDMarkdownParagraph,
)


class _Host(App[None]):
    def __init__(self, md: str, spec: MatchSpec | None = None) -> None:
        super().__init__()
        self._md = md
        self._spec = spec

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield FNDMarkdown(self._md, match_spec=self._spec)

    def on_mount(self) -> None:
        # The theme app.py sets. Under the default textual-dark, $accent and
        # $warning are the same colour, so a note<->warning token swap would
        # resolve identically and the per-type assertions would go blind.
        self.theme = "tokyo-night"


@pytest.mark.asyncio
async def test_callout_blockquote_carries_type_classes() -> None:
    md = "> [!warning] Careful\n> Body text.\n"
    app = _Host(md)
    async with app.run_test(size=(80, 30)) as pilot:
        await app.query_one(FNDMarkdown).update(md)
        await pilot.pause()
        quote = app.query_one(FNDMarkdownBlockQuote)
        assert quote.has_class("callout")
        assert quote.has_class("callout-warning")


@pytest.mark.asyncio
async def test_title_gets_icon_prefix_and_its_own_block() -> None:
    md = "> [!warning] Careful\n> Body text.\n"
    app = _Host(md)
    async with app.run_test(size=(80, 30)) as pilot:
        await app.query_one(FNDMarkdown).update(md)
        await pilot.pause()
        paras = list(app.query(FNDMarkdownParagraph).results())
        plains = [p._content.plain for p in paras]
        assert plains == ["▲  Careful", "Body text."]
        assert paras[0].has_class("callout-title")
        assert not paras[1].has_class("callout-title")


@pytest.mark.asyncio
async def test_foldable_callout_shows_open_marker_and_stays_expanded() -> None:
    md = "> [!tip]- Foldable\n> Hidden in Obsidian.\n"
    app = _Host(md)
    async with app.run_test(size=(80, 30)) as pilot:
        await app.query_one(FNDMarkdown).update(md)
        await pilot.pause()
        plains = [p._content.plain for p in app.query(FNDMarkdownParagraph).results()]
        assert plains == ["▾ ◆  Foldable", "Hidden in Obsidian."]


@pytest.mark.asyncio
async def test_plain_blockquote_gets_no_callout_class() -> None:
    md = "> ordinary quote\n"
    app = _Host(md)
    async with app.run_test(size=(80, 30)) as pilot:
        await app.query_one(FNDMarkdown).update(md)
        await pilot.pause()
        assert not app.query_one(FNDMarkdownBlockQuote).has_class("callout")


@pytest.mark.asyncio
async def test_match_inside_a_callout_title_still_highlights() -> None:
    md = "> [!tip] Cap the rows\n> Body text.\n"
    spec = MatchSpec.from_query("rows", auto_fuzzy=False)
    app = _Host(md, spec)
    async with app.run_test(size=(80, 30)) as pilot:
        widget = app.query_one(FNDMarkdown)
        await widget.update(md)
        await pilot.pause()
        title = next(iter(app.query(FNDMarkdownParagraph).results()))
        plain = title._content.plain
        assert plain == "◆  Cap the rows"
        # The highlight must land on "rows" in the PREFIXED plain, so its offset
        # is the icon prefix plus the word's position in the title.
        want = plain.index("rows")
        hit = [s for s in title._content.spans if s.start == want]
        assert hit, title._content.spans
        assert plain[hit[0].start : hit[0].end] == "rows"
        assert widget.first_match_block is title


@pytest.mark.asyncio
async def test_each_callout_type_resolves_to_its_own_theme_colour() -> None:
    md = "> [!warning] Warned\n> Body.\n\n> [!tip] Tipped\n> Body.\n\n> [!note] Noted\n> Body.\n"
    app = _Host(md)
    async with app.run_test(size=(80, 30)) as pilot:
        await app.query_one(FNDMarkdown).update(md)
        await pilot.pause()
        variables = app.get_css_variables()
        quotes = list(app.query(FNDMarkdownBlockQuote).results())
        titles = list(app.query(".callout-title").results(FNDMarkdownParagraph))
        typed = zip(
            quotes,
            titles,
            ("warning", "tip", "note"),
            ("warning", "success", "accent"),
            strict=True,
        )
        for quote, title, key, variable in typed:
            assert quote.has_class(f"callout-{key}")
            colour = Color.parse(variables[variable])
            assert quote.styles.border_left == ("outer", colour), key
            assert quote.styles.background == colour.with_alpha(0.12), key
            assert title.styles.color == colour, key


@pytest.mark.asyncio
async def test_every_callout_type_is_tinted_apart_from_plain_blockquotes() -> None:
    md = "> [!quote] Quoted\n> Body.\n\n> [!example] Sampled\n> Body.\n\n> ordinary quote\n"
    app = _Host(md)
    async with app.run_test(size=(80, 30)) as pilot:
        await app.query_one(FNDMarkdown).update(md)
        await pilot.pause()
        quoted, sampled, plain = list(app.query(FNDMarkdownBlockQuote).results())
        assert quoted.has_class("callout-quote")
        assert sampled.has_class("callout-example")
        assert not plain.has_class("callout")
        # $boost resolves fully transparent here, so a callout that inherits it
        # renders with no fill — every callout type must carry its own tint.
        assert plain.styles.background.a == 0
        assert quoted.styles.background.a > 0
        assert sampled.styles.background.a > 0
        assert sampled.styles.background != quoted.styles.background
        assert quoted.styles.border_left != plain.styles.border_left


@pytest.mark.asyncio
async def test_nested_callout_title_takes_its_own_type_colour() -> None:
    md = "> [!warning] Outer\n> Body.\n>\n> > [!tip] Inner\n> > Inner body.\n"
    app = _Host(md)
    async with app.run_test(size=(80, 30)) as pilot:
        await app.query_one(FNDMarkdown).update(md)
        await pilot.pause()
        variables = app.get_css_variables()
        warning, success = Color.parse(variables["warning"]), Color.parse(variables["success"])
        # Without a distinguishable pair the outer-colour bleed would be invisible.
        assert warning != success
        outer, inner = list(app.query(FNDMarkdownBlockQuote).results())
        assert outer.has_class("callout-warning")
        assert inner.has_class("callout-tip")
        outer_title, inner_title = list(app.query(".callout-title").results(FNDMarkdownParagraph))
        assert [outer_title._content.plain, inner_title._content.plain] == ["▲  Outer", "◆  Inner"]
        assert outer_title.styles.color == warning
        assert inner_title.styles.color == success
