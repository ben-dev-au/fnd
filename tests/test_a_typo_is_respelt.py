"""A query word no document holds is respelt one edit away, searched as its own
fusion pass, named in a notice, and painted where it matches."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.layered import search_layered
from fnd.matching import MatchSpec, word_matches
from fnd.query import Searcher
from fnd.typos import corrections, describe


@pytest.fixture
def searcher(tmp_path: Path, tmp_index_dir: Path) -> Searcher:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "oop.md").write_text("# OOP\n\npolymorphism lets one interface serve many types.\n")
    (root / "other.md").write_text("# Types\n\nan interface without much else.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    return Searcher(index_dir=tmp_index_dir)


def test_a_suffix_typo_is_respelt(searcher: Searcher) -> None:
    """Three edits from the stem "polymorph", one swap from "polymorphism"."""
    assert corrections(searcher, ["polymorphsim"]) == {"polymorphsim": ("polymorphism",)}


def test_a_known_word_is_left_alone(searcher: Searcher) -> None:
    assert corrections(searcher, ["interface", "polymorphism"]) == {}


def test_the_respelt_word_ranks_its_file_first(searcher: Searcher) -> None:
    """Without the typo pass, "interface" alone matches both files equally."""
    groups, trace = search_layered(
        searcher, query="polymorphsim interface", limit=10, with_trace=True
    )
    assert Path(groups[0].path).name == "oop.md"
    assert trace.corrections == {"polymorphsim": ("polymorphism",)}


def test_the_respelt_word_is_painted() -> None:
    spec = MatchSpec.from_query("polymorphsim").with_corrections(
        {"polymorphsim": ("polymorphism",)}
    )
    assert word_matches("polymorphism", spec)
    assert not word_matches("polymorphism", MatchSpec.from_query("polymorphsim"))


def test_the_notice_names_each_respelling() -> None:
    fixes = {"recurson": ("recursion", "recursive")}
    assert describe(fixes) == "Also searched recurson as recursion or recursive."


@pytest.fixture
def one_collection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    import textwrap

    from fnd.config import load

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.c.sources]]
            path = "{(tmp_path / "notes").as_posix()}"
        """)
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.mark.asyncio
async def test_the_tui_names_the_respelling(
    searcher: Searcher, one_collection: object, tmp_index_dir: Path
) -> None:
    from textual.widgets import Static

    from fnd.tui import FNDApp
    from tests._pilot_wait import run_search

    app = FNDApp(index_dir=tmp_index_dir, config=one_collection)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "polymorphsim")
        notice = app.query_one("#query_notice", Static)
        assert notice.display
        assert str(notice.render()) == "Also searched polymorphsim as polymorphism."
        assert app._search.groups


def test_a_respelt_word_stops_the_strong_signal_shortcut(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """The probe scored "garbage" alone and looked decisive; the respelt word was never searched."""
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "gc.md").write_text("# Garbage\n\ngarbage garbage garbage collection of garbage.\n")
    (root / "oop.md").write_text("# OOP\n\npolymorphism lets one interface serve many types.\n")
    for i in range(30):
        (root / f"filler{i}.md").write_text(f"# Filler {i}\n\nunrelated words number {i}.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")

    groups, trace = search_layered(
        Searcher(index_dir=tmp_index_dir), query="polymorphsim garbage", limit=10, with_trace=True
    )
    assert trace.strong_signal.disabled_by_respelling
    assert "oop.md" in {Path(g.path).name for g in groups}


def test_a_word_one_edit_from_a_stopword_is_left_alone(tmp_path: Path, tmp_index_dir: Path) -> None:
    """ "teh" is "the", which stripping drops; "ten" and "tea" are one edit away but wrong."""
    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.md").write_text("# A\n\nthe tea took ten minutes of recursion\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    assert corrections(Searcher(index_dir=tmp_index_dir), ["teh"]) == {}


def test_the_notice_names_the_word_as_the_files_write_it(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Several edits share the indexed token; the one a file holds is named, every run."""
    root = tmp_path / "notes"
    root.mkdir()
    (root / "wind.md").write_text("# Winds\n\nthe zephyrs blew.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    assert corrections(Searcher(index_dir=tmp_index_dir), ["zephyrus"]) == {
        "zephyrus": ("zephyrs",)
    }
