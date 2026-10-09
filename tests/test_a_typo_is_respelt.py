"""A query word no document holds is respelt one edit away, searched as its own
fusion pass, named in a notice, and painted where it matches."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.layered import search_layered
from fnd.matching import MatchSpec, match_color, word_matches
from fnd.query import Searcher
from fnd.typos import corrections, describe, rare_spellings


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


def test_a_respelt_word_reaches_what_its_respelling_reaches(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """One edit from "cryptography", two from "cryptographic": the typo finds what the word finds."""
    root = tmp_path / "notes"
    root.mkdir()
    (root / "named.md").write_text("# Ciphers\n\ncryptography basics.\n")
    (root / "keys.md").write_text("# Keys\n\ncryptographic keys.\n")
    (root / "other.md").write_text("# Other\n\nnothing here.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    searcher = Searcher(index_dir=tmp_index_dir)

    def files(query: str) -> set[str]:
        groups = search_layered(searcher, query=query, limit=10)
        return {Path(g.path).name for g in groups}

    assert files("crytography") == files("cryptography") == {"named.md", "keys.md"}


def test_a_respelt_word_paints_what_its_respelling_reaches() -> None:
    fixes = {"crytography": ("cryptography",)}
    spec = MatchSpec.from_query("crytography", auto_fuzzy=True).with_corrections(fixes)
    assert word_matches("cryptographic", spec)
    assert not word_matches(
        "cryptographic",
        MatchSpec.from_query("crytography", auto_fuzzy=False).with_corrections(fixes),
    )


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


def _footer(app: object) -> str:
    from textual.widgets import Static

    return app.query_one("#footer_hints", Static).render_line(0).text  # type: ignore[attr-defined]


@pytest.fixture
def plain_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.delenv("TMUX", raising=False)


@pytest.mark.asyncio
async def test_the_tui_names_the_respelling(
    searcher: Searcher, one_collection: object, tmp_index_dir: Path, plain_terminal: None
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
        assert "^T  As typed" in _footer(app)
        assert app._search.groups


def test_a_search_as_typed_respells_nothing(searcher: Searcher) -> None:
    groups, trace = search_layered(
        searcher, query="polymorphsim", limit=10, as_typed=True, with_trace=True
    )
    assert groups == []
    assert trace.respellings == {}


@pytest.mark.parametrize(
    ("env", "key"),
    [
        ({"TERM": "xterm-256color"}, "^T"),
        ({"TERM": "xterm-kitty"}, "Shift+Esc"),
        ({"TERM": "xterm-256color", "TERM_PROGRAM": "ghostty"}, "Shift+Esc"),
        ({"TERM": "xterm-kitty", "TMUX": "/tmp/tmux-1/default"}, "^T"),
    ],
)
def test_the_footer_names_a_key_the_terminal_sends(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], key: str
) -> None:
    from fnd.tui.app import as_typed_key

    for name in ("TERM_PROGRAM", "TMUX"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("fnd.os_labels.is_macos", lambda: False)
    assert as_typed_key() == key


@pytest.mark.parametrize("key", ["shift+escape", "ctrl+t"])
@pytest.mark.asyncio
async def test_the_tui_searches_as_typed_and_back(
    searcher: Searcher, one_collection: object, tmp_index_dir: Path, plain_terminal: None, key: str
) -> None:
    from textual.widgets import Static

    from fnd.tui import FNDApp
    from tests._pilot_wait import run_search, wait_until

    app = FNDApp(index_dir=tmp_index_dir, config=one_collection)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "polymorphsim")
        notice = app.query_one("#query_notice", Static)
        await pilot.press(key)
        await wait_until(pilot, lambda: app._search.idle, message="as-typed search never landed")
        assert app._search.groups == []
        assert not notice.display
        assert "^T  Respell" in _footer(app)
        await pilot.press(key)
        await wait_until(pilot, lambda: app._search.idle, message="respelt search never landed")
        assert app._search.groups
        assert notice.display
        assert "^T  As typed" in _footer(app)


@pytest.mark.asyncio
async def test_a_new_query_is_respelt_again(
    searcher: Searcher, one_collection: object, tmp_index_dir: Path
) -> None:
    from fnd.tui import FNDApp
    from tests._pilot_wait import run_search, wait_until

    app = FNDApp(index_dir=tmp_index_dir, config=one_collection)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "polymorphsim")
        await pilot.press("ctrl+t")
        await wait_until(pilot, lambda: app._search.idle, message="as-typed search never landed")
        await run_search(pilot, app, "interface")
        assert "As typed" not in _footer(app)
        assert "Respell" not in _footer(app)
        await run_search(pilot, app, "polymorphsim")
        assert app._search.latest_trace is not None
        assert app._search.latest_trace.respellings == {"polymorphsim": ("polymorphism",)}


@pytest.mark.asyncio
async def test_a_failed_query_ends_a_search_as_typed(
    searcher: Searcher, one_collection: object, tmp_index_dir: Path
) -> None:
    from fnd.tui import FNDApp
    from tests._pilot_wait import run_search, wait_until

    app = FNDApp(index_dir=tmp_index_dir, config=one_collection)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "polymorphsim")
        await pilot.press("ctrl+t")
        await wait_until(pilot, lambda: app._search.idle, message="as-typed search never landed")
        await run_search(pilot, app, '"interface')
        assert "Respell" not in _footer(app)


@pytest.mark.asyncio
async def test_fuzzy_off_ends_a_search_as_typed(
    searcher: Searcher, one_collection: object, tmp_index_dir: Path
) -> None:
    """With nothing to respell, the footer offers no respelling."""
    from fnd.tui import FNDApp
    from tests._pilot_wait import run_search, wait_until

    app = FNDApp(index_dir=tmp_index_dir, config=one_collection)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "polymorphsim")
        before = app._search.query_signature()
        await pilot.press("ctrl+t")
        await wait_until(pilot, lambda: app._search.idle, message="as-typed search never landed")
        assert app._search.query_signature() != before
        await pilot.press("ctrl+f")
        await wait_until(pilot, lambda: app._search.idle, message="fuzzy-off search never landed")
        assert "Respell" not in _footer(app)
        await pilot.press("ctrl+f")
        await wait_until(pilot, lambda: app._search.idle, message="fuzzy-on search never landed")
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


def test_a_typo_sharing_a_rare_stem_reaches_its_commoner_neighbour(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """ "risle" stems to "risl" as "risling" does; "riesling" is far commoner."""
    root = tmp_path / "notes"
    root.mkdir()
    (root / "river.md").write_text("# River\n\nthe risle runs north.\n")
    for i in range(50):
        (root / f"grape{i:02}.md").write_text("# Grape\n\nriesling from the north.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    searcher = Searcher(index_dir=tmp_index_dir)
    assert corrections(searcher, ["risling"]) == {}
    assert rare_spellings(searcher, ["risling"]) == {"risling": ("riesling",)}


def test_a_word_found_by_its_stem_keeps_its_matches_above_a_commoner_neighbour(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Respelt or not, the files search finds by the stem rank above the neighbour's."""
    root = tmp_path / "notes"
    root.mkdir()
    (root / "t0.md").write_text("# T0\n\nWe test the parser.\n")
    (root / "t1.md").write_text("# T1\n\nA test of the lexer.\n")
    for i in range(110):
        (root / f"n{i:03}.md").write_text(f"# N\n\nNesting season {i}.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    searcher = Searcher(index_dir=tmp_index_dir)
    groups, trace = search_layered(searcher, query="testing parser", limit=50, with_trace=True)
    assert {Path(g.path).name for g in groups[:2]} == {"t0.md", "t1.md"}
    assert trace.corrections == {}


@pytest.mark.parametrize("word", ["testing", "running", "patterns"])
def test_a_word_search_finds_by_its_stem_is_left_alone(
    tmp_path: Path, tmp_index_dir: Path, word: str
) -> None:
    """No file writes the word, but its stem is found; a rare one-edit neighbour is no fix."""
    root = tmp_path / "notes"
    root.mkdir()
    for i in range(4):
        (root / f"n{i}.md").write_text("# N\n\nWe test each pattern, and it runs.\n")
    (root / "birds.md").write_text("# Birds\n\nNesting season; a cunning fox.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    searcher = Searcher(index_dir=tmp_index_dir)
    assert corrections(searcher, [word]) == {}
    groups, trace = search_layered(searcher, query=word, limit=10, with_trace=True)
    assert trace.respellings == {}
    assert "birds.md" not in {Path(g.path).name for g in groups}


@pytest.fixture
def a_shared_typo(tmp_path: Path, tmp_index_dir: Path) -> Searcher:
    """One file writes "ponter"; fifty write "pointer", and one "scrum" beside one "scrub"."""
    root = tmp_path / "notes"
    root.mkdir()
    (root / "typo.md").write_text("# Typo\n\na ponter to the heap.\n")
    (root / "scrum.md").write_text("# Scrum\n\nthe scrum board.\n")
    (root / "scrub.md").write_text("# Scrub\n\nscrub the data.\n")
    for i in range(50):
        (root / f"p{i:02}.md").write_text(f"# Note {i}\n\na pointer to the heap.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    return Searcher(index_dir=tmp_index_dir)


def test_a_rare_word_beside_a_far_commoner_one_reads_as_a_shared_typo(
    a_shared_typo: Searcher,
) -> None:
    assert rare_spellings(a_shared_typo, ["ponter", "pointer", "scrum"]) == {"ponter": ("pointer",)}


def test_the_respelling_fills_the_free_slots_below_the_exact_match(a_shared_typo: Searcher) -> None:
    groups, trace = search_layered(a_shared_typo, query="ponter", limit=10, with_trace=True)
    names = [Path(g.path).name for g in groups]
    assert names[0] == "typo.md"
    assert len(names) == 10
    assert all(n.startswith("p") for n in names[1:])
    for g in groups:
        sections = [(h.parent_id, h.chunk_seq) for h in g.hits]
        assert len(sections) == len(set(sections)), g.path
    assert trace.widened == {"ponter": ("pointer",)}
    spec = trace.paint_spec(MatchSpec.from_query("ponter"), MatchSpec.from_query("ponter"))
    assert word_matches("pointer", spec)


def test_a_common_word_is_never_widened(a_shared_typo: Searcher) -> None:
    groups, trace = search_layered(a_shared_typo, query="scrum", limit=10, with_trace=True)
    assert [Path(g.path).name for g in groups] == ["scrum.md"]
    assert trace.widened == {}


def test_a_widened_word_keeps_its_own_colour() -> None:
    """The typed word is in a file, so its respelling paints beside it, not in its place."""
    spec = MatchSpec.from_query("heap ponter", auto_fuzzy=False).with_corrections(
        {"ponter": ("pointer",)}, typed_written=True
    )
    assert [match_color(w, spec) for w in ("heap", "ponter", "pointer")] == [0, 1, 2]
