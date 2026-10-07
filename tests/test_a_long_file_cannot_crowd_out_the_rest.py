"""Files are cut to the limit after grouping, so a long file cannot hide the files after it."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.layered import search_layered
from fnd.matching import MatchSpec
from fnd.query import Searcher, materialise
from fnd.synonyms import load_default_synonyms


def _corpus(root: Path, *, small: int) -> None:
    root.mkdir()
    long_md = "\n\n".join(f"## Part {i}\n\nsaffron saffron saffron crocus." for i in range(80))
    (root / "book.md").write_text(f"# Book\n\n{long_md}\n", encoding="utf-8")
    filler = " ".join(["meadow"] * 60)
    for i in range(small):
        (root / f"note{i:02d}.md").write_text(
            f"# Note {i}\n\n{filler} saffron {filler}\n", encoding="utf-8"
        )


@pytest.fixture
def crowded(tmp_path: Path, tmp_index_dir: Path) -> Searcher:
    root = tmp_path / "notes"
    _corpus(root, small=12)
    build_index(roots=[root], index_dir=tmp_index_dir, collection="notes")
    return Searcher(index_dir=tmp_index_dir)


def test_a_long_file_cannot_crowd_out_the_rest(crowded: Searcher) -> None:
    """80 high-scoring sections of one file still leave room for the next four."""
    groups, trace = search_layered(crowded, query="saffron", limit=5, with_trace=True)

    assert len(groups) == 5, [g.path for g in groups]
    assert trace.files_truncated


def test_the_count_is_exact_when_every_file_fits(tmp_path: Path, tmp_index_dir: Path) -> None:
    """The control: with room for every file the flag stays down."""
    root = tmp_path / "notes"
    _corpus(root, small=3)
    build_index(roots=[root], index_dir=tmp_index_dir, collection="notes")

    groups, trace = search_layered(
        Searcher(index_dir=tmp_index_dir), query="saffron", limit=10, with_trace=True
    )

    assert len(groups) == 4
    assert not trace.files_truncated


def test_hits_stay_light_until_a_caller_shows_them(crowded: Searcher) -> None:
    """The search pays for snippets only on the hits a caller materialises."""
    groups = search_layered(crowded, query="saffron", limit=5)
    hit = groups[0].hits[0]

    assert not hit.materialised
    assert hit.snippet == ""
    shown = materialise(hit, MatchSpec.from_query("saffron"))
    assert shown.materialised
    assert "saffron" in shown.snippet.lower()
    assert all(h.materialised and h.snippet for h in crowded.search("saffron", limit=5))


def test_a_snippet_anchors_where_the_preview_paints(tmp_path: Path, tmp_index_dir: Path) -> None:
    """A synonym-only hit is anchored on the synonym the painting spec lights up."""
    root = tmp_path / "notes"
    root.mkdir()
    lead = " ".join(["preamble"] * 80)
    (root / "auth.md").write_text(
        f"# Auth\n\n{lead} multi-factor authentication stops replay.\n", encoding="utf-8"
    )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="notes")
    synonyms = load_default_synonyms()
    groups = search_layered(
        Searcher(index_dir=tmp_index_dir), query="MFA", limit=5, synonyms=synonyms
    )

    shown = materialise(groups[0].hits[0], MatchSpec.from_query("MFA", synonyms=synonyms))

    assert "multi-factor" in shown.snippet


def test_a_pass_stopped_at_its_ceiling_marks_the_count_a_floor(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paging stops at exactly the ceiling, and files it never reached raise the marker."""
    monkeypatch.setattr("fnd.query._PAGE_CEILING", 2)
    root = tmp_path / "notes"
    _corpus(root, small=3)
    for name in ("tome", "volume"):
        body = "\n\n".join(f"## Part {i}\n\nsaffron saffron saffron crocus." for i in range(80))
        (root / f"{name}.md").write_text(f"# {name}\n\n{body}\n", encoding="utf-8")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="notes")
    searcher = Searcher(index_dir=tmp_index_dir)

    pool = searcher._candidates("saffron", window=50, collection=None, min_files=5)
    _, trace = search_layered(searcher, query="saffron", limit=5, with_trace=True)

    assert len(pool.hits) == 100
    assert not pool.all_files
    assert trace.files_truncated
