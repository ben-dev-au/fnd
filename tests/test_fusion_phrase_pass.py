"""Fusion derives an exact-phrase pass alongside the bag-of-words lex pass.

The exact-phrase pass (``"q"``) is what surfaces an in-order match; RRF fuses
on rank, so a low-BM25 exact match still rises above scattered partials. Graded
slop passes were removed (measured no-op on the real corpus, see
dev/research/SEARCH_RANKING_DESIGN.md §4): proximity is only meaningful when a
near-miss document exists to promote, and Tantivy already grades distance inside
a single ``"q"~N`` clause when that is ever wanted.
"""

from __future__ import annotations

from pathlib import Path

from fnd.fusion import auto_subqueries, fusion_search
from fnd.index import build_index
from fnd.query import Searcher


def test_unquoted_multiword_emits_phrase_and_lex() -> None:
    subs = auto_subqueries("alpha beta gamma", synonyms=None)
    by = {s.source: s for s in subs}
    assert by["phrase"].query == '"alpha beta gamma"'  # exact, in order
    assert "lex" in by
    # No graded slop passes — exactly the phrase + lex pair.
    assert {s.source for s in subs} == {"phrase", "lex"}
    assert by["phrase"].weight > by["lex"].weight


def test_quoted_query_keeps_exact_intent_no_broadening() -> None:
    """A user-quoted phrase stays exact — the lex pass already carries it as a
    PhraseQuery, so we don't add a second phrase pass that would double-quote."""
    subs = auto_subqueries('"alpha beta gamma"', synonyms=None)
    assert [s.source for s in subs] == ["lex"]


def test_single_word_has_no_phrase_pass() -> None:
    assert [s.source for s in auto_subqueries("alpha", synonyms=None)] == ["lex"]


def test_exact_phrase_beats_scattered(tmp_path: Path) -> None:
    """End-to-end, length-controlled: every doc has ``alpha`` and ``beta`` once
    and the SAME total length, so BM25/fieldnorm is equal. The doc with the
    terms adjacent matches the exact-phrase pass and so outranks the docs whose
    terms are scattered (matched only by the bag-of-words lex pass)."""
    corpus = tmp_path / "c"
    corpus.mkdir()

    def doc(gap: int, total: int = 60) -> str:
        mid = " ".join(f"w{i}" for i in range(gap))
        tail = " ".join(f"t{i}" for i in range(total - gap - 2))
        body = f"alpha {mid} beta {tail}".replace("  ", " ").strip()
        return f"# H\n\n{body}\n"

    (corpus / "adj.md").write_text(doc(0), encoding="utf-8")  # adjacent → phrase pass
    (corpus / "gap.md").write_text(doc(8), encoding="utf-8")  # scattered → lex only
    (corpus / "far.md").write_text(doc(40), encoding="utf-8")  # scattered → lex only
    idx = tmp_path / "i"
    build_index(roots=[corpus], index_dir=idx, collection="t", rebuild=True)

    s = Searcher(index_dir=idx)
    hits = fusion_search(s, query="alpha beta", limit=10, collection="t")
    order = [Path(h.path).stem for h in hits]
    # The exact-adjacent doc wins via the phrase pass; the scattered docs follow.
    # We no longer grade gap-vs-far (that was the removed slop passes' job).
    assert order.index("adj") < order.index("gap")
    assert order.index("adj") < order.index("far")
