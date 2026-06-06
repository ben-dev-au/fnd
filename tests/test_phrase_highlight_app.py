"""The live preview span builder is phrase-aware.

``_build_match_spans`` drives both the visible highlight overlay and the
scroll-to-first-match jump (via ``_record_first_match``). For a quoted
phrase it must span the phrase and NOT light up the phrase's stopwords
elsewhere.
"""

from __future__ import annotations

from fnd.matching import MatchSpec
from fnd.tui.app import _build_match_spans


def test_phrase_span_built_for_phrase_line() -> None:
    spec = MatchSpec.from_query('"defence in depth"', auto_fuzzy=False)
    line = "Our defence in depth plan"
    spans = _build_match_spans(line, spec)
    p_start = line.index("defence")
    p_end = line.index("depth") + len("depth")
    assert any(s.start <= p_start and s.end >= p_end for s in spans)


def test_no_span_for_lone_stopword_line() -> None:
    spec = MatchSpec.from_query('"defence in depth"', auto_fuzzy=False)
    assert _build_match_spans("stay in the loop and relax", spec) == []
