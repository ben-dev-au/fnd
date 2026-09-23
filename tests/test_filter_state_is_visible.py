"""A filter row's state carries colour as well as shape, and no_index cannot
be inverted.

`●`, `⊘` and `○` were painted the same colour and weight: `⊘` and `○` differ
by a hairline and mean opposites. Colour is a second channel beside the glyph,
not instead of it, and it is spent on the two states that change what gets
indexed.

`no_index` ships excluded, so one press on the state a user finds turned "never
index these" into "index ONLY these": nine files to zero, a reindex to undo.
It skips include entirely; every other tag keeps the full cycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.tui import FNDApp
from fnd.tui.settings_screen import FilterBrowserScreen
from fnd.tui.widgets.toggle_tree import ToggleTree


@pytest.fixture
def cfg_and_index(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """A real index with tags, so the sidebar has rows to paint."""
    import textwrap

    from fnd.config import load
    from fnd.index import build_index

    root = tmp_path / "papers"
    root.mkdir()
    (root / "a.md").write_text("---\ntags: [recipe, dinner]\n---\n\nsaffron\n", encoding="utf-8")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="papers")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.papers.sources]]
            path = "{root.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path), tmp_index_dir


_SAMPLE = SourceSample(kinds={"md": 3, "pdf": 1}, tags={"frontmatter": {"no_index": 1, "keep": 2}})


async def _tree(app: FNDApp, pilot: object, spec: FilterSpec) -> ToggleTree:
    app.push_screen(
        FilterBrowserScreen(
            title="Index filters",
            spec=spec,
            gitignore=True,
            fndignore=True,
            sample_provider=lambda _spec: _SAMPLE,
            on_save=lambda *_a: None,
        )
    )
    for _ in range(25):
        await pilot.pause()  # type: ignore[attr-defined]
    tree = app.screen.query_one("#filter_tree", ToggleTree)
    # Only the branches under test: File types offers all forty kinds, so
    # expanding everything pushes the tag rows off the screen.
    #
    # Case-insensitively: this means "the tag branch", and a lowercase match
    # only hits while the branch borrows its single source's name.
    for node in tree.root.children:
        label = str(node.label).lower()
        if any(word in label for word in ("tags", "notes & text")):
            node.expand()
            for child in node.children:
                child.expand()
    for _ in range(8):
        await pilot.pause()  # type: ignore[attr-defined]
    return tree


def _marker_colours(app: FNDApp) -> dict[str, set[str]]:
    """Every colour each glyph is painted in, not the first.

    The legend row spells the glyphs out unstyled, so taking the first
    occurrence measured the legend and never reached a row.
    """
    out: dict[str, set[str]] = {"●": set(), "⊘": set(), "○": set()}
    for strip in app.screen._compositor.render_strips():
        for seg in strip:
            glyph = seg.text.strip()
            if glyph in out and seg.style and seg.style.color:
                out[glyph].add(str(seg.style.color))
    return out


@pytest.mark.asyncio
async def test_include_and_exclude_carry_different_colours(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        # Both states in the one branch the helper expands: an included tag
        # beside an excluded one.
        await _tree(
            app,
            pilot,
            FilterSpec(
                include_tags={"frontmatter": ("keep",)},
                exclude_tags={"frontmatter": ("no_index",)},
            ),
        )
        painted = _marker_colours(app)
        success = app.get_css_variables().get("success", "")
        error = app.get_css_variables().get("error", "")

    assert any(success.lower() in c.lower() for c in painted["●"]), painted
    assert any(error.lower() in c.lower() for c in painted["⊘"]), painted


@pytest.mark.asyncio
async def test_the_off_state_carries_none(tmp_index_dir: Path) -> None:
    """The control: colour marks the states that change the index, so the
    neutral one must not compete with them."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        await _tree(app, pilot, FilterSpec(kinds=("md",)))
        painted = _marker_colours(app)

    assert painted["○"] == set(), painted


@pytest.mark.asyncio
async def test_no_index_cycles_past_include(tmp_index_dir: Path) -> None:
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        tree = await _tree(app, pilot, FilterSpec(exclude_tags={"frontmatter": ("no_index",)}))
        item = "tag:frontmatter:no_index"
        assert item in tree._excluded, "the premise: it ships excluded"
        tree._cycle(item)
        after_one = (item in tree._excluded, item in tree._selected)
        tree._cycle(item)
        after_two = (item in tree._excluded, item in tree._selected)

    assert after_one == (False, False), "one press must reach off, not include"
    assert after_two == (True, False), "and the next returns to never-index"


@pytest.mark.asyncio
async def test_every_other_tag_keeps_the_full_cycle(tmp_index_dir: Path) -> None:
    """The control: the exception is one tag, not a change to the mechanism."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        tree = await _tree(app, pilot, FilterSpec(exclude_tags={"frontmatter": ("keep",)}))
        item = "tag:frontmatter:keep"
        assert item in tree._excluded, "the premise"
        tree._cycle(item)
        reached_include = item in tree._selected

    assert reached_include, "an ordinary tag must still reach index-only"


@pytest.mark.asyncio
async def test_the_sidebar_paints_the_same_states_the_same_way(
    cfg_and_index: tuple[object, Path],
) -> None:
    """The two panes are both called Filters. The sidebar hand-rolls its
    markers rather than using ToggleTree, so the same state read differently
    in each until they shared one mapping."""
    config, index_dir = cfg_and_index
    app = FNDApp(index_dir=index_dir, config=config)  # type: ignore[arg-type]
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._scope.tag_include["frontmatter"] = {"recipe"}
        app._scope.tag_exclude["frontmatter"] = {"dinner"}
        app._scope.refresh_filters_panel()
        for _ in range(8):
            await pilot.pause()
        from textual.widgets import Tree

        tree = app.query_one("#filters_panel_tree", Tree)
        for node in tree.root.children:
            node.expand()
            for child in node.children:
                child.expand()
        for _ in range(8):
            await pilot.pause()
        painted = _marker_colours(app)
        success = app.get_css_variables().get("success", "")
        error = app.get_css_variables().get("error", "")
        # Prove the sidebar is what was measured: without this the assertions
        # below pass on any coloured marker anywhere on the screen.
        on_screen = "\n".join(
            "".join(s.text for s in strip) for strip in app.screen._compositor.render_strips()
        )

    assert "recipe" in on_screen, "the sidebar rows never reached the screen"
    assert any(success.lower() in c.lower() for c in painted["●"]), painted
    assert any(error.lower() in c.lower() for c in painted["⊘"]), painted


def test_a_theme_without_the_variable_still_colours() -> None:
    """A theme is not required to define every semantic colour, and dropping
    the signal in silence is indistinguishable from the feature being absent."""
    from fnd.tui.results_labels import (
        STATE_COLOUR_FALLBACK,
        STATE_COLOUR_VARIABLE,
        state_colour,
    )

    assert state_colour("●", {}) == STATE_COLOUR_FALLBACK["●"]
    assert state_colour("⊘", {}) == STATE_COLOUR_FALLBACK["⊘"]
    assert state_colour("○", {}) == "", "the neutral state stays neutral"
    assert set(STATE_COLOUR_VARIABLE) == set(STATE_COLOUR_FALLBACK)


def test_the_theme_wins_when_it_names_one() -> None:
    """The control: the fallback is a floor, not a replacement."""
    from fnd.tui.results_labels import state_colour

    assert state_colour("●", {"success": "#123456"}) == "#123456"


@pytest.mark.asyncio
async def test_a_collapsed_screen_shows_colour(tmp_index_dir: Path) -> None:
    """Every marker on a collapsed screen is a BRANCH roll-up, so colouring
    only the leaves would show a user no colour at all until they expand
    something."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.push_screen(
            FilterBrowserScreen(
                title="Index filters",
                spec=FilterSpec(exclude_tags={"frontmatter": ("no_index",)}),
                gitignore=True,
                fndignore=True,
                # One tag, wholly excluded, so the branch rolls up to ⊘
                # rather than the ◐ that a partial one earns.
                sample_provider=lambda _spec: SourceSample(
                    kinds={"md": 3}, tags={"frontmatter": {"no_index": 1}}
                ),
                on_save=lambda *_a: None,
            )
        )
        for _ in range(25):
            await pilot.pause()
        # No expansion at all: this is the screen as it opens.
        rows = [
            (
                "".join(s.text for s in strip),
                [
                    str(s.style.color)
                    for s in strip
                    if s.text.strip() in ("●", "⊘") and s.style and s.style.color
                ],
            )
            for strip in app.screen._compositor.render_strips()
        ]
        success = app.get_css_variables().get("success", "")
        error = app.get_css_variables().get("error", "")

    branch_colours = [c for line, cols in rows if "▶" in line for c in cols]
    assert branch_colours, "no branch marker carried a colour on the opening screen"
    assert any(success.lower() in c.lower() for c in branch_colours), branch_colours
    assert any(error.lower() in c.lower() for c in branch_colours), branch_colours


def _walk(node: Any) -> list[Any]:
    """Every node under this one, at any depth."""
    return [n for child in node.children for n in (child, *_walk(child))]


def _rows(app: FNDApp) -> list[tuple[str, list[str]]]:
    """Each painted row, with the colours its state markers carry.

    Row-scoped, not screen-scoped: a whole-screen scan passes on a coloured
    marker anywhere, so an uncoloured branch passes while the tags rows below
    it carry the assertion.
    """
    return [
        (
            "".join(s.text for s in strip),
            [
                str(s.style.color)
                for s in strip
                if s.text.strip() in ("●", "⊘", "◐") and s.style and s.style.color
            ],
        )
        for strip in app.screen._compositor.render_strips()
    ]


async def _sidebar_filters(app: FNDApp, pilot: object) -> Any:
    from textual.widgets import Tree

    tree = app.query_one("#filters_panel_tree", Tree)
    for node in tree.root.children:
        node.expand()
        for child in node.children:
            child.expand()
    for _ in range(8):
        await pilot.pause()  # type: ignore[attr-defined]
    return tree


@pytest.mark.asyncio
async def test_the_sidebar_colours_every_branch_and_not_just_tags(
    cfg_and_index: tuple[object, Path],
) -> None:
    """Tags were the only sidebar branch painted through the shared helper.
    File type, Modified and Created built their rows as plain strings, so the
    same state read coloured in one branch and plain in the next."""
    config, index_dir = cfg_and_index
    app = FNDApp(index_dir=index_dir, config=config)  # type: ignore[arg-type]
    async with app.run_test(size=(120, 46)) as pilot:
        await pilot.pause()
        app._scope.filter_kinds.append("md")
        app._scope.filter_date = "week"
        app._scope.filter_created = "month"
        app._scope.refresh_filters_panel()
        for _ in range(8):
            await pilot.pause()
        await _sidebar_filters(app, pilot)
        rows = _rows(app)
        success = app.get_css_variables().get("success", "").lower()

    def coloured(label: str) -> list[str]:
        return [c for line, cols in rows if label in line for c in cols]

    for label in ("Markdown", "Notes & text", "week", "month"):
        found = coloured(label)
        assert found, f"{label!r} carried no colour: {[line for line, _ in rows if label in line]}"
        assert any(success in c.lower() for c in found), (label, found)


@pytest.mark.asyncio
async def test_a_toggled_file_type_stays_coloured(cfg_and_index: tuple[object, Path]) -> None:
    """The path a user actually takes. Toggling a file type repaints in place
    rather than rebuilding the tree, so the build path being coloured says
    nothing about what is on screen one keypress later."""
    from textual.widgets import Tree

    config, index_dir = cfg_and_index
    app = FNDApp(index_dir=index_dir, config=config)  # type: ignore[arg-type]
    async with app.run_test(size=(120, 46)) as pilot:
        await pilot.pause()
        tree = await _sidebar_filters(app, pilot)
        leaf = next(
            n
            for n in _walk(tree.root)
            if (n.data or {}).get("category") == "kinds" and (n.data or {}).get("value") == "md"
        )
        app._scope.on_filters_selected(Tree.NodeSelected(leaf))
        for _ in range(8):
            await pilot.pause()
        rows = _rows(app)
        success = app.get_css_variables().get("success", "").lower()

    assert "md" in app._scope.filter_kinds, "the premise: the toggle landed"
    painted = [c for line, cols in rows if "Markdown" in line for c in cols]
    assert painted, "the repainted row lost its colour"
    assert any(success in c.lower() for c in painted), painted


@pytest.mark.asyncio
async def test_the_cursor_row_keeps_its_marker_colour(cfg_and_index: tuple[object, Path]) -> None:
    """The row a user is certain to be looking at. Textual stylises the whole
    label with the cursor's component style after the label's own spans, so
    the marker went plain on exactly the row under the cursor."""
    config, index_dir = cfg_and_index
    app = FNDApp(index_dir=index_dir, config=config)  # type: ignore[arg-type]
    async with app.run_test(size=(120, 46)) as pilot:
        await pilot.pause()
        app._scope.filter_kinds.append("md")
        app._scope.refresh_filters_panel()
        for _ in range(8):
            await pilot.pause()
        tree = await _sidebar_filters(app, pilot)
        leaf = next(
            n
            for n in _walk(tree.root)
            if (n.data or {}).get("category") == "kinds" and (n.data or {}).get("value") == "md"
        )
        tree.focus()
        tree.move_cursor(leaf)
        for _ in range(8):
            await pilot.pause()
        rows = _rows(app)
        success = app.get_css_variables().get("success", "").lower()

    painted = [c for line, cols in rows if "Markdown" in line for c in cols]
    assert painted, "the cursor row lost its marker colour"
    assert any(success in c.lower() for c in painted), painted


@pytest.mark.asyncio
async def test_the_settings_cursor_row_keeps_it_too(tmp_index_dir: Path) -> None:
    """The same contract, through the same seam, on the other pane: the two
    are the reason the mixin exists rather than a fix inside one tree."""
    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        tree = await _tree(app, pilot, FilterSpec(exclude_tags={"frontmatter": ("no_index",)}))
        node = next(n for n in _walk(tree.root) if "no_index" in str(n.label))
        tree.focus()
        tree.move_cursor(node)
        for _ in range(8):
            await pilot.pause()
        rows = _rows(app)
        error = app.get_css_variables().get("error", "").lower()

    painted = [c for line, cols in rows if "no_index" in line for c in cols]
    assert painted, "the cursor row lost its marker colour"
    assert any(error in c.lower() for c in painted), painted


def test_a_branch_still_scanning_never_claims_all_of_them() -> None:
    """The red flash. Before the scan lands the only tags known are the
    excluded ones the spec named, so the roll-up read ⊘ ("never index any
    of these") and became ◐ a moment later when the real tags arrived."""
    from fnd.filters.tree_model import spec_branches

    spec = FilterSpec(exclude_tags={"frontmatter": ("no_index",)})
    scanning = next(b for b in spec_branches(spec, None) if b.id.startswith("tags"))
    landed = next(b for b in spec_branches(spec, _SAMPLE) if b.id.startswith("tags"))

    assert not scanning.complete, "a sampleless branch has not seen its leaves"
    assert landed.complete, "the control: a scanned branch knows what it holds"


@pytest.mark.asyncio
async def test_the_scanning_branch_paints_partial_not_excluded(tmp_index_dir: Path) -> None:
    """And the widget honours it: same spec, both states, on screen."""
    from fnd.tui.widgets.toggle_tree import ToggleGroup, ToggleItem

    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        tree = ToggleTree("Filters")
        item = ToggleItem("tag:frontmatter:no_index", "no_index")
        scanning = ToggleGroup("tags", "Tags", (item,), mode="cycle", complete=False)
        landed = ToggleGroup("tags", "Tags", (item,), mode="cycle")
        tree._excluded = {item.id}
        while_scanning = str(tree._group_label(scanning))
        once_landed = str(tree._group_label(landed))

    assert while_scanning.startswith("◐"), while_scanning
    assert once_landed.startswith("⊘"), once_landed
