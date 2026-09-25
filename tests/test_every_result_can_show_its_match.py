"""A search returns only what the preview can show it matched.

Automatic fuzzy reached two edits while the highlighter paints one, and a
compound's split spelling matched across separators the highlighter never
joined (`Track.Name`, "not found", a block boundary).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.layered import search_layered
from fnd.matching import MatchSpec
from fnd.query import Searcher
from fnd.render import match_word_spans, match_word_spans_multi, text_has_any_match
from fnd.tui.match_evidence import evidence_spec_for_pass, has_paintable_match

QUERIES = ("nameof", "trackname", "customercount", "notfound", "viewmodel", "dropdown", "footer")


@pytest.fixture
def index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    notes = tmp_path / "notes"
    notes.mkdir()
    sections = {
        "names": "Each name is a plain name, and names repeat.",
        "nameof": "Use nameof(Customer) for the key.",
        "track": "Bind `Track.Name` in the view.",
        "count": "Show `Customers.Count` in the card.",
        "found": "The page returns not found here.",
        "blocks": "Build the view\n\nModel binding follows.",
        "drop": "A drop-down picks one.",
        "folder": "Put it in a folder.",
    }
    body = "\n\n".join(f"## {name}\n\n{text}" for name, text in sections.items())
    (notes / "sheet.md").write_text(body, encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def test_every_hit_of_every_query_can_show_its_match(index: Path) -> None:
    searcher = Searcher(index_dir=index)
    unshown: list[str] = []
    for query in QUERIES:
        groups, _ = search_layered(
            searcher, query=query, limit=50, collection="notes", with_trace=True
        )
        strict = MatchSpec.from_query(query, auto_fuzzy=False)
        painting = MatchSpec.from_query(query)
        for group in groups:
            chunks = {c.chunk_seq: c for c in searcher.get_file_chunks(group.parent_id)}
            for hit in group.hits:
                spec = evidence_spec_for_pass(hit.pass_index, strict=strict, painting=painting)
                if not has_paintable_match(chunks[hit.chunk_seq], spec):
                    unshown.append(f"{query}: {hit.heading_path}")
    assert not unshown, unshown


def _paths(index: Path, query: str) -> set[str]:
    groups, _ = search_layered(
        Searcher(index_dir=index), query=query, limit=50, collection="notes", with_trace=True
    )
    return {h.heading_path for g in groups for h in g.hits}


def test_automatic_fuzzy_does_not_reach_two_edits(index: Path) -> None:
    """`footer` has no literal hit, and "folder" is two edits away."""
    assert not any(p.endswith("folder") for p in _paths(index, "footer"))


def test_an_explicit_two_still_reaches_two_edits(index: Path) -> None:
    assert any(p.endswith("folder") for p in _paths(index, "footer~2"))


@pytest.mark.parametrize(
    ("query", "text", "shown"),
    [
        ("trackname", "Bind Track.Name here", "Track.Name"),
        ("customercount", "Show Customers.Count now", "Customers.Count"),
        ("notfound", "It is not found here", "not found"),
        ("dropdown", "A drop-down picks one", "drop-down"),
    ],
)
def test_a_split_spelling_paints_whatever_separates_it(query: str, text: str, shown: str) -> None:
    runs = match_word_spans(text, MatchSpec.from_query(query))
    assert [text[a:b] for a, b, _ in runs] == [shown]
    assert text_has_any_match(text, MatchSpec.from_query(query))


def test_a_split_spelling_across_two_blocks_paints_nothing() -> None:
    first, second = match_word_spans_multi(
        ("Build the view", "Model binding"), MatchSpec.from_query("viewmodel")
    )
    assert first == second == []


@pytest.mark.parametrize(
    ("query", "text"),
    [
        ("isvisible", "it is visible"),
        ("eventhandler", "the event, handler"),
        ("tostring", "go to string"),
    ],
)
def test_a_pair_that_is_not_a_credible_compound_paints_nothing(query: str, text: str) -> None:
    assert match_word_spans(text, MatchSpec.from_query(query)) == []
    assert not text_has_any_match(text, MatchSpec.from_query(query))


def test_a_compound_hit_nothing_can_show_is_not_returned(index: Path) -> None:
    """The split spelling "view model" matches across two blocks, which paints nothing."""
    assert not any(p.endswith("blocks") for p in _paths(index, "viewmodel"))
