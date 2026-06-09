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
from fnd.render import MATCH_STYLES, MISMATCH_STYLE, apply_match_highlights

# Tag styles for readable assertions: palette slots Y/C/G/P/B, variance O.
_TAGS = {MATCH_STYLES[0]: "Y", MATCH_STYLES[1]: "C", MATCH_STYLES[2]: "G", MISMATCH_STYLE: "O"}


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
        out.append((text[sp.start : sp.end], _TAGS.get(str(sp.style), "?")))
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
        # wildcard with another term: the all-orange regression is gone, and each
        # term gets its own colour — strategy(slot 0 Y), discoun(slot 1 C) + t(O).
        (
            "strategy discoun*",
            "the strategy gave a discount",
            [("strategy", "Y"), ("discoun", "C"), ("t", "O")],
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


def test_single_colour_mode() -> None:
    """With multicolour off, every term uses slot-0 yellow; variance stays
    orange. (The Settings toggle / config.multicolour_highlights drives this.)"""
    from rich.text import Text

    spec = MatchSpec.from_query("cross entropy loss", auto_fuzzy=True, multicolour=False)
    t = Text("cross entropy loss")
    apply_match_highlights(t, spec)
    tags = [_TAGS.get(str(sp.style), "?") for sp in sorted(t.spans, key=lambda s: s.start)]
    assert tags == ["Y", "Y", "Y"]


def test_per_term_colours() -> None:
    """Each distinct word in a multi-word query highlights in its own colour;
    the variance (orange) is shared. Single-term queries stay slot-0 yellow."""
    assert _coloured("cross entropy loss", "cross entropy loss") == [
        ("cross", "Y"),
        ("entropy", "C"),
        ("loss", "G"),
    ]
    # single term unchanged
    assert _coloured("discount", "a discount") == [("discount", "Y")]
    # a repeated term keeps its first colour
    assert _coloured("cross entropy cross", "cross entropy cross") == [
        ("cross", "Y"),
        ("entropy", "C"),
        ("cross", "Y"),
    ]


def test_field_qualifier_does_not_consume_a_colour_slot() -> None:
    """`kind:pdf` is a filter, not a body term — the real term keeps slot-0
    yellow instead of being pushed to a later colour by `kind`/`pdf`."""
    assert _coloured("kind:pdf strategy", "a strategy doc") == [("strategy", "Y")]


def test_excluded_terms_are_not_highlighted() -> None:
    """`-x` / `NOT x` are prohibited — they must never highlight."""
    for q in ("crypto -wallet", "crypto NOT wallet"):
        spec = MatchSpec.from_query(q)
        assert word_matches("crypto", spec)
        assert not word_matches("wallet", spec)


def test_wildcard_inside_parens_still_highlights() -> None:
    """A grouped wildcard keeps its highlight (the leading `(` used to leak into
    the stored pattern and break the fullmatch)."""
    spec = MatchSpec.from_query("(discoun* OR foo)")
    assert word_matches("discount", spec)


def test_exact_match_not_repainted_by_wildcard_mask() -> None:
    """With `discount discoun*`, the exact word `discount` renders as one clean
    yellow run, not `discoun` + a variance-painted `t` from the wildcard mask."""
    assert _coloured("discount discoun*", "a discount") == [("discount", "Y")]


def test_quoted_phrase_and_boolean_term_get_distinct_colours() -> None:
    """A quoted phrase highlights as one unit in the phrase colour; a loose term
    joined by a boolean gets a *different* colour (not the same slot-0 yellow).
    Regression: the phrase used to be stripped before colours were assigned, so
    the external term landed in slot 0 too and everything looked single-colour.
    The OR keyword itself is never highlighted."""
    spans = _coloured('"defence in depth" OR diverse', "a defence in depth model is diverse")
    assert ("defence in depth", "Y") in spans
    assert ("diverse", "C") in spans
    assert not any(seg.strip() in ("OR", "or") for seg, _ in spans)


def test_phrase_highlights_as_span() -> None:
    # Quoted phrase highlights via the phrase-span path, contiguous in order.
    from fnd.matching import phrase_char_spans

    spec = MatchSpec.from_query('"powerhouse"', auto_fuzzy=False)
    # single-word quote == bare word; ensure the term still highlights
    assert _highlighted("powerhouse", _TEXT) == {"powerhouse"}
    spec = MatchSpec.from_query('"the mitochondria"', auto_fuzzy=False)
    spans = phrase_char_spans(_TEXT, spec)
    assert spans, "expected a contiguous phrase span for the quoted phrase"
