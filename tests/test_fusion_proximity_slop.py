"""Fusion derives graduated sloppy-phrase passes so proximity is scored
natively by Tantivy (BM25 phrase_count over the position index), rather
than by a hand-rolled post-rank multiplier.
"""

from __future__ import annotations

from pathlib import Path

from fnd.fusion import auto_subqueries, fusion_search
from fnd.index import build_index
from fnd.query import Searcher


def test_unquoted_multiword_emits_graduated_slop() -> None:
    subs = auto_subqueries("alpha beta gamma", synonyms=None)
    by = {s.source: s for s in subs}
    assert by["phrase"].query == '"alpha beta gamma"'  # exact, slop 0
    assert by["near"].query == '"alpha beta gamma"~5'
    assert by["loose"].query == '"alpha beta gamma"~25'
    # Tighter proximity carries more weight.
    assert by["phrase"].weight > by["near"].weight > by["loose"].weight
    assert "lex" in by


def test_quoted_query_keeps_exact_intent_no_slop_broadening() -> None:
    """A user-quoted phrase stays exact — we don't add looser slop passes
    that would surface non-adjacent docs the user didn't ask for."""
    subs = auto_subqueries('"alpha beta gamma"', synonyms=None)
    assert [s.source for s in subs] == ["lex"]


def test_single_word_has_no_slop_passes() -> None:
    assert [s.source for s in auto_subqueries("alpha", synonyms=None)] == ["lex"]


def test_fusion_proximity_beats_scattered(tmp_path: Path) -> None:
    """End-to-end and length-controlled: every doc has ``alpha`` and ``beta``
    exactly once and the SAME total length, so BM25/fieldnorm is equal and
    the only differentiator is term spread. The docs whose terms fall inside
    a slop window (adjacent, and a 3-word gap) both outrank the doc whose
    terms are 40 apart (matched only by the bag-of-words lex pass)."""
    corpus = tmp_path / "c"
    corpus.mkdir()

    def doc(gap: int, total: int = 60) -> str:
        mid = " ".join(f"w{i}" for i in range(gap))
        tail = " ".join(f"t{i}" for i in range(total - gap - 2))
        body = f"alpha {mid} beta {tail}".replace("  ", " ").strip()
        return f"# H\n\n{body}\n"

    (corpus / "adj.md").write_text(doc(0), encoding="utf-8")  # slop 0
    (corpus / "gap.md").write_text(doc(3), encoding="utf-8")  # within slop 5
    (corpus / "far.md").write_text(doc(40), encoding="utf-8")  # beyond slop 25
    idx = tmp_path / "i"
    build_index(roots=[corpus], index_dir=idx, collection="t", rebuild=True)

    s = Searcher(index_dir=idx)
    hits = fusion_search(s, query="alpha beta", limit=10, collection="t")
    order = [Path(h.path).stem for h in hits]
    # Graded: adjacent (slop 0) > small gap (within slop 5) > far (lex only).
    # Holds even though BM25 is tied, because the position bonus is now
    # weight-scaled so the exact-phrase pass dominates.
    assert order.index("adj") < order.index("gap") < order.index("far")
