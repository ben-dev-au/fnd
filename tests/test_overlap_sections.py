"""A match an extractor's window overlap repeats is listed as one section, the
higher-ranked of the two that hold it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fnd.index import build_index
from fnd.layered import search_layered
from fnd.query import FileGroup, Searcher


def _groups(
    tmp_path: Path, index_dir: Path, files: dict[str, str], query: str, **kwargs: Any
) -> list[FileGroup]:
    docs = tmp_path / "docs"
    docs.mkdir()
    for name, text in files.items():
        (docs / name).write_text(text, encoding="utf-8")
    build_index(roots=[docs], index_dir=index_dir, collection="default")
    out = search_layered(Searcher(index_dir=index_dir), query=query, limit=10, **kwargs)
    assert isinstance(out, list)
    return out


def _rows(marks: dict[int, str], *, long_from: int = 400) -> str:
    """400 code rows; rows from ``long_from`` on are padded so their windows rank lower."""
    pad = " ".join(f"pad{j}" for j in range(4))
    rows = [f"x_{i} = {i}" + (f"  # {pad}" if i >= long_from else "") for i in range(400)]
    for i, word in marks.items():
        rows[i] += f"  # {word}"
    return "\n".join(rows)


def _sections(groups: list[FileGroup], name: str) -> list[int]:
    (group,) = [g for g in groups if g.path.endswith(name)]
    return sorted(h.chunk_seq for h in group.hits)


def test_a_code_match_in_an_overlap_is_one_section(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Row 150 sits in windows 0 and 1; equal ranks keep the window that shows it."""
    groups = _groups(tmp_path, tmp_index_dir, {"p.py": _rows({150: "zebra"})}, "zebra")
    assert _sections(groups, "p.py") == [1]


def test_a_txt_match_in_an_overlap_is_one_section(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Plain text's 200-character overlap repeats a match the same way."""
    text = "word " * 170 + "zebra " + "word " * 400
    groups = _groups(tmp_path, tmp_index_dir, {"n.txt": text}, "zebra")
    assert len(_sections(groups, "n.txt")) == 1


def test_the_higher_ranked_window_is_the_one_kept(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Padding window 1 ranks it lower, so window 0 stands for the match."""
    text = _rows({150: "zebra"}, long_from=160)
    groups = _groups(tmp_path, tmp_index_dir, {"p.py": text}, "zebra")
    assert _sections(groups, "p.py") == [0]


def test_a_window_with_a_match_of_its_own_stays(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Window 0 also holds row 10's match, so both windows are sections."""
    text = _rows({10: "zebra", 150: "zebra"})
    groups = _groups(tmp_path, tmp_index_dir, {"p.py": text}, "zebra")
    assert _sections(groups, "p.py") == [0, 1]


@pytest.mark.parametrize("row", [150, 300])
def test_a_fuzzy_match_in_an_overlap_is_one_section(
    tmp_path: Path, tmp_index_dir: Path, row: int
) -> None:
    """A near-miss the fuzzy pass found folds alike."""
    files = {"p.py": _rows({row: "zebre"}), "other.txt": "a zebra here"}
    groups = _groups(tmp_path, tmp_index_dir, files, "zebra")
    assert len(_sections(groups, "p.py")) == 1


def test_a_folded_section_is_not_counted_as_unshown(tmp_path: Path, tmp_index_dir: Path) -> None:
    """The header's "+" means sections went unshown; a fold leaves none behind."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "p.py").write_text(_rows({150: "zebra"}), encoding="utf-8")
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="default")
    out = search_layered(
        Searcher(index_dir=tmp_index_dir), query="zebra", limit=10, with_trace=True
    )
    assert isinstance(out, tuple)
    groups, trace = out
    assert [len(g.hits) for g in groups] == [1]
    assert not trace.sections_truncated


def test_a_respelt_query_keeps_the_sections_its_spelling_would(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """The fold judges by the respelling the search ran, so a typo keeps both sections."""
    rows = [f"q{i}" for i in range(500)]
    rows[200] += " polymorphism"
    rows[300] += " invoice"
    files = {"p.py": "\n".join(rows), "o.txt": "polymorphism invoice\n" * 2, "o2.txt": "nothing"}
    groups = _groups(tmp_path, tmp_index_dir, files, "polymorphsim invoice")
    assert _sections(groups, "p.py") == [1, 2]


def test_a_phrase_the_cut_splits_keeps_the_window_that_holds_it(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Rows 147 to 148 hold a phrase only window 0 has whole, so it stays beside window 1."""
    pad = " ".join(f"pad{j}" for j in range(8))
    rows = [f"x_{i} = {i}  # {pad}" for i in range(140)] + [f"x_{i} = {i}" for i in range(140, 400)]
    rows[147] += "  # alpha"
    rows[148] = "beta = 148"
    rows[150] += "  # alpha beta"
    groups = _groups(tmp_path, tmp_index_dir, {"p.py": "\n".join(rows)}, '"alpha beta"')
    (group,) = groups
    ranks = {h.chunk_seq: h.rank_key for h in group.hits}
    assert ranks[0] < ranks[1], "window 0 must rank lower, or the fold keeps it anyway"
    assert _sections(groups, "p.py") == [0, 1]


def test_a_fold_frees_a_capped_slot(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Two slots: the overlap pair takes one, row 300's distinct match the other."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "p.py").write_text(_rows({150: "zebra", 300: "zebra"}, long_from=290), encoding="utf-8")
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="default")
    out = search_layered(
        Searcher(index_dir=tmp_index_dir),
        query="zebra",
        limit=10,
        sections_per_file=2,
        with_trace=True,
    )
    assert isinstance(out, tuple)
    groups, trace = out
    assert _sections(groups, "p.py") == [1, 2]
    assert not trace.sections_truncated
