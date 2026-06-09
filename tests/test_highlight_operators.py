"""Highlighting must mark exactly the words the search surfaced — across every
operator. Regression guard for the wildcard/regex/fuzzy highlight expansion
(``MatchSpec`` carries prefix/regex patterns and uses OSA fuzzy, mirroring the
search-side resolvers).
"""

from __future__ import annotations

import re

import pytest
from rich.text import Text

from fnd.matching import MatchSpec, word_matches
from fnd.render import HIGHLIGHT_STYLE, MISMATCH_STYLE, apply_match_highlights


def _highlighted(query: str, text: str) -> set[str]:
    spec = MatchSpec.from_query(query, auto_fuzzy=True, min_term_chars=0)
    return {m.group(0) for m in re.finditer(r"\w+", text) if word_matches(m.group(0), spec)}


def _coloured(query: str, text: str) -> list[tuple[str, str]]:
    """Return (substring, 'Y'|'O') spans — Y = yellow match, O = orange variance.
    Captures the convention: matching/literal chars yellow, fuzzy/wildcard-filled
    chars orange."""
    spec = MatchSpec.from_query(query, auto_fuzzy=True, min_term_chars=0)
    t = Text(text)
    apply_match_highlights(t, spec)
    out: list[tuple[str, str]] = []
    for sp in sorted(t.spans, key=lambda s: s.start):
        tag = "Y" if sp.style == HIGHLIGHT_STYLE else ("O" if sp.style == MISMATCH_STYLE else "?")
        out.append((text[sp.start : sp.end], tag))
    return out


_TEXT = (
    "the discount and discounts were discounted on discovery; cryptography uses "
    "crypto while a cryptid stays mythical and cryptographic too; the mitochondria "
    "is the powerhouse; gray and grey, not greasy"
)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # stemming: one term lights up its inflections
        ("discount", {"discount", "discounts", "discounted"}),
        # trailing wildcard: prefix on the stem, not the bare residual
        ("discoun*", {"discount", "discounts", "discounted"}),
        ("crypto*", {"crypto", "cryptography", "cryptographic"}),  # NOT cryptid
        # regex over the term dictionary
        ("/crypt(o|id)/", {"crypto", "cryptid"}),
        # single-char ? glob
        ("gr?y", {"gray", "grey"}),
        # fuzzy incl. an adjacent transposition (ir <-> ri) at distance 1
        ("mitochondira~1", {"mitochondria"}),
        # combined: plain term + wildcard both highlight
        ("powerhouse discoun*", {"powerhouse", "discount", "discounts", "discounted"}),
    ],
)
def test_operator_highlights(query: str, expected: set[str]) -> None:
    assert _highlighted(query, _TEXT) == expected


def test_wildcard_excludes_non_prefix() -> None:
    # discoun* must not light up "discovery" (shares "disco", not "discoun").
    assert "discovery" not in _highlighted("discoun*", _TEXT)


@pytest.mark.parametrize(
    ("query", "text", "expected"),
    [
        # wildcard with another term: the all-orange regression — discount must be
        # discoun(yellow) + t(orange), NOT aligned against "strategy".
        (
            "strategy discoun*",
            "the strategy gave a discount",
            [("strategy", "Y"), ("discoun", "Y"), ("t", "O")],
        ),
        ("discoun*", "a discount", [("discoun", "Y"), ("t", "O")]),
        # infix / leading / single-char globs: literal chars yellow, fill orange
        ("cr*to", "crypto here", [("cr", "Y"), ("yp", "O"), ("to", "Y")]),
        ("*graph", "the cryptograph", [("crypto", "O"), ("graph", "Y")]),
        ("gr?y", "gray", [("gr", "Y"), ("a", "O"), ("y", "Y")]),
        # fuzzy transposition: matching chars yellow, transposed orange
        ("mitochondira~1", "mitochondria", [("mitochond", "Y"), ("ri", "O"), ("a", "Y")]),
        # stem suffix: typed term yellow, inflection orange
        ("discount", "discounts", [("discount", "Y"), ("s", "O")]),
        # regex: whole word yellow (no clean char attribution)
        ("/crypt(o|id)/", "crypto", [("crypto", "Y")]),
    ],
)
def test_match_colouring(query: str, text: str, expected: list[tuple[str, str]]) -> None:
    assert _coloured(query, text) == expected


def test_phrase_highlights_as_span() -> None:
    # Quoted phrase highlights via the phrase-span path, contiguous in order.
    from fnd.matching import phrase_char_spans

    spec = MatchSpec.from_query('"powerhouse"', auto_fuzzy=False)
    # single-word quote == bare word; ensure the term still highlights
    assert _highlighted("powerhouse", _TEXT) == {"powerhouse"}
    spec = MatchSpec.from_query('"the mitochondria"', auto_fuzzy=False)
    spans = phrase_char_spans(_TEXT, spec)
    assert spans, "expected a contiguous phrase span for the quoted phrase"
