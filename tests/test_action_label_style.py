"""Action rows (Clear filters, Match mode) render in the inactive-pane colour."""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from rich.text import Text
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.results_labels import _styled_action_label


def test_action_label_applies_the_colour() -> None:
    label = _styled_action_label("✕  Clear all filters", "#BB9AF7")
    assert isinstance(label, Text)
    assert "#BB9AF7" in str(label.style) or "#bb9af7" in str(label.style).lower()
    assert "Clear all filters" in label.plain


def test_action_label_is_not_dim() -> None:
    """Distinct from the dim category headers — actions should read brighter."""
    label = _styled_action_label("x", "#BB9AF7")
    assert "dim" not in str(label.style).lower()


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.papers.sources]]
            path = "/tmp/papers"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.fixture
def idx(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "papers"
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.md").write_text("---\ntags: [recipe]\n---\n\n# A\n\nsaffron\n", encoding="utf-8")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="papers")
    return tmp_index_dir


def _find(tree: Tree[Any], needle: str) -> TreeNode[Any]:
    def walk(node: TreeNode[Any]) -> Iterator[TreeNode[Any]]:
        yield node
        for c in node.children:
            yield from walk(c)

    for node in walk(tree.root):
        if needle in str(node.label):
            return node
    raise AssertionError(f"{needle!r} not found")


@pytest.mark.asyncio
async def test_match_row_carries_the_action_colour(cfg: Config, idx: Path) -> None:
    """The in-tree Match row takes the inactive-border colour. (Clear moved to
    its own pinned bar, which gets the same colour straight from CSS.)"""
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        expected = app._scope._action_colour().lower()
        assert expected, "no action colour resolved"

        tree = app.query_one("#filters_panel_tree", Tree)
        _find(tree, "Tags").expand()
        await pilot.pause()

        label = _find(tree, "Match:").label
        assert isinstance(label, Text)
        assert expected in str(label.style).lower()


@pytest.mark.asyncio
async def test_action_colour_equals_the_border_foreground(cfg: Config, idx: Path) -> None:
    """The rows use the border's exact foreground — $primary 50% over surface.
    Same foreground + same thin-stroke glyphs => same perceived colour."""
    from textual.color import Color

    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        v = app.get_css_variables()
        border_fg = Color.parse(v["surface"]).blend(Color.parse(v["primary"]), 0.5).hex.lower()
        assert app._scope._action_colour().lower() == border_fg
        # Dimmer than full-strength primary, so it doesn't shout.
        assert app._scope._action_colour().lower() != v["primary"].lower()


def test_action_colour_matches_the_default_theme_border() -> None:
    """Guards the blend against drift: default theme lands on #6F6199."""
    from textual.color import Color

    got = Color.parse("#24283B").blend(Color.parse("#BB9AF7"), 0.5).hex.upper()
    assert got == "#6F6199"


@pytest.mark.asyncio
async def test_a_tag_row_is_not_action_coloured(cfg: Config, idx: Path) -> None:
    """Only control rows get the colour; a real tag stays default."""
    app = FNDApp(index_dir=idx, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#filters_panel_tree", Tree)
        _find(tree, "Tags").expand()
        await pilot.pause()
        _find(tree, "Frontmatter").expand()
        await pilot.pause()
        primary = app.get_css_variables().get("primary", "").lower()
        recipe = _find(tree, "recipe").label
        # A plain string label (no primary styling) — tags aren't actions.
        assert primary not in str(getattr(recipe, "style", "")).lower()
