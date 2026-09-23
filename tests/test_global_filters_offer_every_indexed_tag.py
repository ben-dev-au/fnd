"""The defaults screen offers every tag in the index, not the first few it walked.

`_distinct_roots` capped the scan at the first three collections and their
first two sources, so on a twelve-collection config the screen sampled five
roots, flagged itself "partial scan", and simply did not offer the tags of the
other nine. These are the defaults for EVERY collection, so the answer wanted
is every collection's tags.

The index already holds them. Asking it is faster than a walk, opens no
file, and hydrates nothing from a cloud folder: measured at 65 ms for 141
distinct tags across a real twelve-collection index.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import Config, load
from fnd.index import build_index_from_config
from fnd.tui import FNDApp
from fnd.tui.menu import _indexed_tags


@pytest.fixture
def three_collections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Path]:
    """More collections than a capped walk would reach, each with its own tag."""
    index_dir = tmp_path / "index"
    lines = []
    for n in range(4):
        root = tmp_path / f"c{n}"
        root.mkdir()
        (root / "note.md").write_text(f"---\ntags: [only_in_c{n}]\n---\n\nbody\n", encoding="utf-8")
        lines.append(f'[[collections.c{n}.sources]]\npath = "{root.as_posix()}"\n')
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(textwrap.dedent("".join(lines)), encoding="utf-8")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    cfg = load(cfg_path)
    for name, collection in cfg.collections.items():
        build_index_from_config(config=collection, collection=name, index_dir=index_dir)
    return cfg, index_dir


@pytest.mark.asyncio
async def test_a_tag_from_the_last_collection_is_offered(
    three_collections: tuple[Config, Path],
) -> None:
    cfg, index_dir = three_collections
    app = FNDApp(index_dir=index_dir, config=cfg)
    async with app.run_test(size=(110, 30)) as pilot:
        for _ in range(15):
            await pilot.pause()
        sample = _indexed_tags(app)

    assert sample is not None
    offered = set(sample.tags.get("frontmatter", {}))
    assert offered == {f"only_in_c{n}" for n in range(4)}, offered


@pytest.mark.asyncio
async def test_it_does_not_call_itself_partial(
    three_collections: tuple[Config, Path],
) -> None:
    """The walk flagged `truncated` because it genuinely was. The index is
    the whole answer, so claiming partial would be its own dishonesty."""
    cfg, index_dir = three_collections
    app = FNDApp(index_dir=index_dir, config=cfg)
    async with app.run_test(size=(110, 30)) as pilot:
        for _ in range(15):
            await pilot.pause()
        sample = _indexed_tags(app)

    assert sample is not None
    assert not sample.truncated, "it called a complete answer partial"


@pytest.mark.asyncio
async def test_the_index_sample_carries_no_kinds(
    three_collections: tuple[Config, Path],
) -> None:
    """Every kind is offered from the registry, so a sampled subset of them
    would only shrink the picker as collections were added."""
    cfg, index_dir = three_collections
    app = FNDApp(index_dir=index_dir, config=cfg)
    async with app.run_test(size=(110, 30)) as pilot:
        for _ in range(15):
            await pilot.pause()
        sample = _indexed_tags(app)

    assert sample is not None
    assert sample.kinds == {}


@pytest.mark.asyncio
async def test_an_unbuilt_corpus_offers_no_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_index_dir: Path
) -> None:
    """None is the right answer before the first index: a walk standing in for
    it offers tags from files the index does not hold."""
    root = tmp_path / "vault"
    root.mkdir()
    (root / "a.md").write_text("---\ntags: [never_indexed]\n---\n\nbody\n", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.fresh.sources]]
            path = "{root.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    cfg = load(cfg_path)

    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test(size=(110, 30)) as pilot:
        for _ in range(15):
            await pilot.pause()
        sample = _indexed_tags(app)

    assert sample is None


@pytest.mark.asyncio
async def test_the_screen_says_why_it_offers_none(tmp_index_dir: Path) -> None:
    """A branch that is simply absent reads as a missing feature."""
    from textual.containers import Vertical

    from fnd.filters import FilterSpec
    from fnd.tui.settings_screen import FilterBrowserScreen

    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.push_screen(
            FilterBrowserScreen(
                title="Index filters",
                spec=FilterSpec(),
                gitignore=True,
                fndignore=True,
                sample_provider=lambda _spec: None,
                no_tags_note="tags are offered once a collection is indexed",
                on_save=lambda *_a: None,
            )
        )
        for _ in range(25):
            await pilot.pause()
        assert app.screen.query_one("#settings_box", Vertical) is not None
        rows = [
            "".join(s.text for s in strip).strip().strip("│").strip()
            for strip in app.screen._compositor.render_strips()
        ]
        # The note wraps inside the box, so the border and the wrap have to go
        # before the sentence can be looked for at all.
        painted = " ".join(" ".join(rows).split())

    assert "tags are offered once a collection is indexed" in painted, painted


def test_the_index_lookup_survives_a_stub_app() -> None:
    """The settings invariant tests drive these getters with a SimpleNamespace
    carrying only `_config`. Reaching straight through `app._search` raised
    AttributeError and took the walk fallback down with it (eight tests, all
    of them about the fallback rather than about the index)."""
    from types import SimpleNamespace
    from typing import Any, cast

    from fnd.tui.menu import _indexed_tags

    stub = SimpleNamespace(_config=None)
    assert _indexed_tags(cast("Any", stub)) is None


@pytest.mark.asyncio
async def test_an_indexed_but_tagless_corpus_is_not_called_unindexed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`None` meant three things, so a fully indexed collection with no tags
    was told "tags are offered once a collection is indexed"."""
    index_dir = tmp_path / "index"
    root = tmp_path / "plain"
    root.mkdir()
    (root / "note.md").write_text("no frontmatter, no tags\n", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.plain.sources]]
            path = "{root.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    cfg = load(cfg_path)
    build_index_from_config(
        config=cfg.collections["plain"], collection="plain", index_dir=index_dir
    )

    app = FNDApp(index_dir=index_dir, config=cfg)
    async with app.run_test(size=(110, 30)) as pilot:
        for _ in range(15):
            await pilot.pause()
        sample = _indexed_tags(app)

    assert sample is not None, "it could ask, and did"
    assert not any(sample.tags.values())


@pytest.mark.asyncio
async def test_the_two_states_say_different_things(tmp_index_dir: Path) -> None:
    """The one the user reads. Unindexed and tagless are different sentences."""
    from textual.containers import Vertical

    from fnd.filters import FilterSpec
    from fnd.filters.scan import SourceSample
    from fnd.tui.settings_screen import FilterBrowserScreen

    def _painted(app: FNDApp) -> str:
        rows = [
            "".join(s.text for s in strip).strip().strip("│").strip()
            for strip in app.screen._compositor.render_strips()
        ]
        return " ".join(" ".join(rows).split())

    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        for provider, expected, forbidden in (
            (
                lambda _spec: None,
                "tags are offered once a collection is indexed",
                "no tags in what",
            ),
            (
                lambda _spec: SourceSample(),
                "no tags in what is indexed",
                "once a collection is indexed",
            ),
        ):
            app.push_screen(
                FilterBrowserScreen(
                    title="Index filters",
                    spec=FilterSpec(),
                    gitignore=True,
                    fndignore=True,
                    sample_provider=provider,
                    no_tags_note="no tags in what is indexed",
                    unindexed_note="tags are offered once a collection is indexed",
                    on_save=lambda *_a: None,
                )
            )
            for _ in range(25):
                await pilot.pause()
            assert app.screen.query_one("#settings_box", Vertical) is not None
            painted = _painted(app)
            assert expected in painted, painted
            assert forbidden not in painted, painted
            app.pop_screen()
            await pilot.pause()
