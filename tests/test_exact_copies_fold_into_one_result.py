"""Byte-identical copies share a content hash and fold into one result."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.layered import search_layered
from fnd.query import Hit, Searcher, group_by_file
from fnd.schema import F_CONTENT_HASH, F_PATH


def test_copies_share_a_hash_and_an_edit_does_not(tmp_path: Path, tmp_index_dir: Path) -> None:
    root = tmp_path / "notes"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    (root / "a" / "note.md").write_text("# Note\n\nshared words here.\n")
    (root / "b" / "note.md").write_text("# Note\n\nshared words here.\n")
    (root / "edited.md").write_text("# Note\n\nshared words here, edited.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")

    searcher = Searcher(index_dir=tmp_index_dir)._searcher
    query = Searcher(index_dir=tmp_index_dir)._index.parse_query("shared", ["body"])
    hashes = {
        Path(str(doc.get_first(F_PATH))).relative_to(root).as_posix(): str(
            doc.get_first(F_CONTENT_HASH)
        )
        for doc in (searcher.doc(a) for _s, a in searcher.search(query, limit=10).hits)
    }
    assert hashes["a/note.md"] == hashes["b/note.md"]
    assert hashes["edited.md"] != hashes["a/note.md"]
    assert all(len(h) == 64 for h in hashes.values())


def _hit(pid: str, digest: str, score: float) -> Hit:
    return Hit(
        memberships=((pid, f"/{pid}"),),
        score=score,
        parent_id=pid,
        path=f"/{pid}/note.md",
        kind="md",
        page=0,
        slide=0,
        heading_path="",
        title="",
        snippet="",
        content_hash=digest,
    )


def test_a_copy_folds_into_the_first_and_takes_no_slot() -> None:
    hits = [_hit("a", "h1", 3.0), _hit("b", "h1", 3.0), _hit("c", "h2", 2.0)]
    groups = group_by_file(hits, limit=2, collapse_copies=True)
    assert [(g.parent_id, g.copies) for g in groups] == [("a", ("/b/note.md",)), ("c", ())]
    assert groups[0].memberships == (("a", "/a"), ("b", "/b")), "a copy keeps its collection mark"


def test_collapsing_off_or_an_unknown_hash_keeps_every_file() -> None:
    hits = [_hit("a", "h1", 3.0), _hit("b", "h1", 3.0), _hit("c", "", 2.0), _hit("d", "", 1.0)]
    assert [g.parent_id for g in group_by_file(hits, limit=9)] == ["a", "b", "c", "d"]
    collapsed = group_by_file(hits, limit=9, collapse_copies=True)
    assert [g.parent_id for g in collapsed] == ["a", "c", "d"]


def test_the_row_notes_the_copies() -> None:
    from fnd.tui.results_labels import _format_file_label

    group = group_by_file(
        [_hit("a", "h1", 3.0), _hit("b", "h1", 3.0), _hit("c", "h1", 3.0)],
        limit=9,
        collapse_copies=True,
    )[0]
    assert str(_format_file_label(group)).endswith("note.md +2 copies")


def test_search_lists_one_result_with_its_copies(tmp_path: Path, tmp_index_dir: Path) -> None:
    root = tmp_path / "notes"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    (root / "a" / "note.md").write_text("# Note\n\nshared words here.\n")
    (root / "b" / "note.md").write_text("# Note\n\nshared words here.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")

    searcher = Searcher(index_dir=tmp_index_dir)
    assert len(search_layered(searcher, query="shared", limit=10)) == 2, "collapsing is opt-in"
    groups = search_layered(searcher, query="shared", limit=10, collapse_copies=True)
    assert len(groups) == 1
    assert len(groups[0].copies) == 1
    assert {Path(groups[0].path).parent.name, Path(groups[0].copies[0]).parent.name} == {"a", "b"}


def test_the_cli_prints_where_the_copies_are(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import textwrap

    from typer.testing import CliRunner

    from fnd.cli import app

    root = tmp_path / "notes"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    for sub in ("a", "b"):
        (root / sub / "note.md").write_text("# Note\n\nshared words here.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.c.sources]]
            path = "{root.as_posix()}"
        """)
    )
    monkeypatch.setattr("fnd.cli.default_index_dir", lambda: tmp_index_dir)
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.migrate.prompt_and_rebuild_or_exit", lambda **kw: None)

    out = CliRunner().invoke(app, ["search", "shared"]).stdout
    assert out.count("note.md") == 2
    assert "        also at: " in out


@pytest.mark.asyncio
async def test_the_preview_header_names_a_copy_with_brackets_in_its_path(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy path is text: read as markup, "[old]" would vanish as a style tag."""
    import textwrap

    from fnd.config import load
    from fnd.tui import FNDApp
    from tests._pilot_wait import run_search, wait_until

    root = tmp_path / "notes"
    for name in ("a/note.md", "b/x [old].md"):
        (root / name).parent.mkdir(parents=True)
        (root / name).write_text("# Note\n\nshared words here.\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.c.sources]]
            path = "{root.as_posix()}"
        """)
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    app = FNDApp(index_dir=tmp_index_dir, config=load(cfg_path))
    async with app.run_test(size=(200, 40)) as pilot:
        await run_search(pilot, app, "shared")
        pane = app.query_one("#preview_pane")
        await wait_until(pilot, lambda: "also at" in str(pane.border_title or ""))
        title = pane._border_title
        assert title is not None
        assert "x [old].md" in title.plain, title.plain


def test_an_edited_file_is_hashed_afresh(tmp_path: Path) -> None:
    """The hash is memoised per (mtime, size), so an edit must still change it."""
    import os

    from fnd.cache import sha256_file

    f = tmp_path / "note.md"
    f.write_text("first")
    before = sha256_file(f)
    f.write_text("other")
    os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 1_000_000))
    assert sha256_file(f) != before
