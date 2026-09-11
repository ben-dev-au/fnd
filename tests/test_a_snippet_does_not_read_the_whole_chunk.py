"""One query took 34 seconds because 364 pooled chunks were 33,000 words each.

A StackExchange dump holds one post per line, the data extractor windows by
line, and `_make_snippet` walked every word of every pooled chunk to pick a
window: 12.1 million words per query, in Python. A snippet is 240 characters.
It needs a bounded region around a match, not a pass over the chunk.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from fnd import query as q
from fnd.matching import MatchSpec


def _huge(match_at_word: int, total_words: int = 40_000) -> str:
    words = ["filler"] * total_words
    words[match_at_word] = "quokka"
    return " ".join(words)


def test_a_match_deep_in_a_huge_chunk_is_still_found() -> None:
    body = _huge(match_at_word=35_000)

    snippet = q._make_snippet(body, "quokka")

    assert "quokka" in snippet, snippet


def test_a_huge_chunk_is_not_scanned_in_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bound: the matcher sees a region, never the chunk."""
    from fnd import render

    seen: list[int] = []
    real = render.match_word_spans

    def spy(text: str, spec: MatchSpec) -> Any:
        seen.append(len(text))
        return real(text, spec)

    monkeypatch.setattr(render, "match_word_spans", spy)
    body = _huge(match_at_word=35_000)

    q._make_snippet(body, "quokka")

    assert seen, "the matcher never ran"
    assert max(seen) <= q._SNIPPET_REGION_CHARS, f"scanned {max(seen):,} chars of {len(body):,}"


def test_five_hundred_huge_chunks_cost_well_under_a_second() -> None:
    """The measured shape: a 500-chunk pool of 33,000-word chunks took 34 s."""
    body = _huge(match_at_word=20_000, total_words=33_000)

    t = time.perf_counter()
    for _ in range(500):
        q._make_snippet(body, "quokka")
    elapsed = time.perf_counter() - t

    # 16 s before the cap, under 2 s with it. Generous so a loaded machine
    # does not flake; the deterministic guard is the scan-bound test above.
    assert elapsed < 5.0, f"{elapsed:.1f}s for 500 snippets"


def test_a_normal_chunk_is_unchanged() -> None:
    """The control: a chunk under the cap is scanned whole, as before, so the
    best-window choice over all its anchors is untouched."""
    body = "quokka " + "filler " * 60 + "quokka delta " + "filler " * 60

    snippet = q._make_snippet(body, "quokka delta")

    assert "quokka delta" in snippet, snippet
