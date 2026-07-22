"""Scope regressions: spaced collection names must survive the multi-
collection ``c:`` filter, and a corrupted/phantom collection name (a
comma-joined ``--collection`` value or stale persisted entry) must not
drive search scope.

Both stem from a collection name that contains a space/comma. The
multi-collection filter built an unquoted ``c:`` value, so a spaced name
was split at the space — the tail became a stray bare term and the name
was dropped. A phantom name with no config row could then pin every
search while being invisible (untoggleable) in the sidebar.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import Config, load
from fnd.index import build_index
from fnd.tui import FNDApp


@pytest.fixture
def two_collection_index(tmp_path: Path) -> tuple[Path, Config]:
    """One index holding three collections (the second has a spaced name).
    Every doc shares a common term so a search hits all three unless the
    collection scope actually restricts."""
    plain = tmp_path / "plain"
    spaced = tmp_path / "spaced"
    third = tmp_path / "third"
    for d in (plain, spaced, third):
        d.mkdir()
    (plain / "a.md").write_text("# Alpha\n\nshared topic here\n", encoding="utf-8")
    (spaced / "b.md").write_text(
        "# Bravo\n\nshared topic plus zylophone marker\n", encoding="utf-8"
    )
    (third / "c.md").write_text("# Charlie\n\nshared topic elsewhere\n", encoding="utf-8")

    index_dir = tmp_path / "index"
    build_index(roots=[plain], index_dir=index_dir, collection="Plain")
    build_index(roots=[spaced], index_dir=index_dir, collection="Spaced Coll")
    build_index(roots=[third], index_dir=index_dir, collection="Third")

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.Plain.sources]]
            path = "{plain.as_posix()}"
            [[collections."Spaced Coll".sources]]
            path = "{spaced.as_posix()}"
            [[collections.Third.sources]]
            path = "{third.as_posix()}"
        """),
        encoding="utf-8",
    )
    return index_dir, load(cfg_path)


@pytest.mark.asyncio
async def test_multi_collection_scope_hard_restricts(
    two_collection_index: tuple[Path, Config], isolated_ui_state: Path
) -> None:
    """Selecting 2 collections (one spaced) must HARD-restrict to exactly
    those two: the third collection's doc is excluded, and the spaced
    collection's doc is included (not dropped by a ``c:`` space split)."""
    index_dir, cfg = two_collection_index
    from fnd.tui.scope_panel import FULL

    app = FNDApp(index_dir=index_dir, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._scope.selection = {"Plain": FULL, "Spaced Coll": FULL}
        app._search.run("shared topic")
        await pilot.pause()
        names = {Path(g.path).name for g in app._search.groups}
        assert "b.md" in names, "spaced collection 'Spaced Coll' was dropped from scope"
        assert "a.md" in names, "'Plain' collection missing from scope"
        assert "c.md" not in names, (
            "'Third' collection leaked into a 2-collection scope — multi-"
            "collection scoping is ranking softly instead of hard-filtering"
        )


@pytest.mark.asyncio
async def test_phantom_collection_name_dropped_on_load(
    two_collection_index: tuple[Path, Config], isolated_ui_state: Path
) -> None:
    index_dir, cfg = two_collection_index
    # Persist a corrupted scope: a comma-joined phantom that matches no
    # config collection, plus one real collection.
    isolated_ui_state.parent.mkdir(parents=True, exist_ok=True)
    isolated_ui_state.write_text(
        '[scope]\ncollections = ["Spaced Coll,Plain", "Plain"]\nsources = []\n'
        "[panels]\ncollapsed = []\nexpanded_collections = []\n"
        "expanded_filter_branches = []\n"
        '[filters]\nkinds = []\ndate = "any"\n'
    )
    app = FNDApp(index_dir=index_dir, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._scope.collections == ["Plain"], (
            "phantom 'Spaced Coll,Plain' should be dropped — it has no panel "
            f"row yet drives scope; got {app._scope.collections}"
        )


@pytest.mark.asyncio
async def test_cli_collection_value_splits_and_validates(
    two_collection_index: tuple[Path, Config], isolated_ui_state: Path
) -> None:
    index_dir, cfg = two_collection_index
    # A comma-separated --collection value resolves to the real names; an
    # unknown name is dropped rather than becoming a phantom.
    app = FNDApp(index_dir=index_dir, config=cfg, collection="Plain,Spaced Coll,Nope")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert set(app._scope.collections) == {"Plain", "Spaced Coll"}
