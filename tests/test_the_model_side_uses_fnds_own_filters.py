"""Sections the model proposes pass exactly the scope and filters fnd's own hits pass."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import tantivy

from fnd.index import build_index
from fnd.query import FilteredSearcher, Searcher, _light_hit
from fnd.tag_query import TagFilter

# name, collection, text: md files carry frontmatter status and tags.
_FILES = {
    "plan.md": (
        "notes",
        "---\nstatus: done\ntags: [work]\n---\n# Plan\n\n## One\n\nalpha\n\n## Two\n\nbeta\n",
    ),
    "draft.md": ("notes", "---\nstatus: open\ntags: [home]\n---\n# Draft\n\n## One\n\ngamma\n"),
    "log.txt": ("notes", "delta epsilon\n"),
    "book.md": ("books", "---\nstatus: done\ntags: [home]\n---\n# Book\n\n## Ch1\n\nzeta\n"),
    "list.txt": ("books", "eta theta\n"),
}


@dataclass
class Corpus:
    searcher: Searcher
    pids: dict[str, str]
    every: list[tuple[str, int]]
    source: dict[str, str]

    def sections_of(self, names: set[str]) -> set[tuple[str, int]]:
        wanted = {self.pids[n] for n in names}
        return {k for k in self.every if k[0] in wanted}


@pytest.fixture
def corpus(tmp_path: Path, tmp_index_dir: Path) -> Corpus:
    for col in ("notes", "books"):
        root = tmp_path / col
        root.mkdir()
        for name, (c, text) in _FILES.items():
            if c == col:
                (root / name).write_text(text, encoding="utf-8")
        build_index(roots=[root], index_dir=tmp_index_dir, collection=col)
    s = Searcher(index_dir=tmp_index_dir)
    raw = s._searcher
    hits = [
        _light_hit(raw.doc(a), 0.0)
        for _, a in raw.search(tantivy.Query.all_query(), limit=1000).hits
    ]
    pids = {Path(h.path).name: h.parent_id for h in hits}
    source = {Path(h.path).name: h.memberships[0][1] for h in hits}
    every = sorted({(h.parent_id, h.chunk_seq) for h in hits})
    return Corpus(s, pids, every, source)


def _call(searcher: object, keys: list[tuple[str, int]], **scope: object) -> set[tuple[str, int]]:
    args: dict[str, object] = {
        "collection": None,
        "source_scope": None,
        "metadata_filter": None,
        "tag_filter": None,
    }
    args.update(scope)
    return {(h.parent_id, h.chunk_seq) for h in searcher.sections(keys, **args)}  # type: ignore[attr-defined]


def test_no_filter_admits_every_section(corpus: Corpus) -> None:
    """Unscoped, every asked section the index holds comes back."""
    assert _call(corpus.searcher, corpus.every) == set(corpus.every)


def test_a_collection_scope_admits_only_its_files(corpus: Corpus) -> None:
    """A collection scope keeps that collection's sections only."""
    got = _call(corpus.searcher, corpus.every, collection="books")
    assert got == corpus.sections_of({"book.md", "list.txt"})


def test_a_source_scope_admits_only_that_source(corpus: Corpus) -> None:
    """A partial scope of one collection's source keeps that source's sections."""
    scope = {"notes": [corpus.source["plan.md"]]}
    got = _call(corpus.searcher, corpus.every, collection=[], source_scope=scope)
    assert got == corpus.sections_of({"plan.md", "draft.md", "log.txt"})


def test_filter_clauses_apply(corpus: Corpus) -> None:
    """A Filters-pane clause narrows the model's sections as it narrows fnd's."""
    got = _call(FilteredSearcher(corpus.searcher, clauses=["kind:txt"]), corpus.every)
    assert got == corpus.sections_of({"log.txt", "list.txt"})


def test_a_metadata_filter_applies(corpus: Corpus) -> None:
    """An inline frontmatter rule keeps only files it admits."""
    got = _call(corpus.searcher, corpus.every, metadata_filter="status == 'done'")
    assert got == corpus.sections_of({"plan.md", "book.md"})


def test_a_tag_filter_applies(corpus: Corpus) -> None:
    """A tag selection keeps only tagged files."""
    tags = TagFilter(include={"frontmatter": frozenset({"home"})})
    got = _call(corpus.searcher, corpus.every, tag_filter=tags)
    assert got == corpus.sections_of({"draft.md", "book.md"})


def test_combined_filters_intersect(corpus: Corpus) -> None:
    """Scope, clause, rule and tags together keep what passes all of them."""
    tags = TagFilter(include={"frontmatter": frozenset({"home"})})
    got = _call(
        FilteredSearcher(corpus.searcher, clauses=["kind:md"]),
        corpus.every,
        collection="books",
        metadata_filter="status == 'done'",
        tag_filter=tags,
    )
    assert got == corpus.sections_of({"book.md"})


def test_only_the_asked_sections_come_back(corpus: Corpus) -> None:
    """Other sections of the same file are not returned."""
    one = [k for k in corpus.every if k[0] == corpus.pids["plan.md"]][:1]
    assert _call(corpus.searcher, one) == set(one)


def test_a_deleted_file_returns_nothing(corpus: Corpus) -> None:
    """A key whose file the index no longer holds is not returned."""
    assert _call(corpus.searcher, [("gone", 0)]) == set()


def test_only_the_asked_sections_are_read(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking for one section of a long file reads that section, not the whole file."""
    root = tmp_path / "book"
    root.mkdir()
    (root / "long.md").write_text(
        "# Long\n\n" + "".join(f"## S{i}\n\nwords {i}\n\n" for i in range(200)), encoding="utf-8"
    )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="book")
    s = Searcher(index_dir=tmp_index_dir)
    raw = s._searcher
    hit = _light_hit(raw.doc(raw.search(tantivy.Query.all_query(), limit=1).hits[0][1]), 0.0)
    reads = {"n": 0}

    class Counting:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        def doc(self, addr: object) -> object:
            reads["n"] += 1
            return self._inner.doc(addr)  # type: ignore[attr-defined]

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    monkeypatch.setattr(s, "_searcher", Counting(raw))
    found = _call(s, [(hit.parent_id, hit.chunk_seq)])
    assert found == {(hit.parent_id, hit.chunk_seq)}
    assert reads["n"] <= 2
