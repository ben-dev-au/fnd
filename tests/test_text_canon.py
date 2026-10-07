"""``fnd.text_canon``: one Unicode form for text entering fnd, so the index and
every query agree on what a character is."""

from __future__ import annotations

import unicodedata

import pytest

from fnd.text_canon import canonical, identity_key
from tests import _hostile_text

ACUTE = "\N{COMBINING ACUTE ACCENT}"


def test_a_decomposed_letter_is_composed() -> None:
    assert canonical(f"cafe{ACUTE}") == "caf\N{LATIN SMALL LETTER E WITH ACUTE}"


@pytest.mark.parametrize(
    ("raw", "folded"),
    [
        ("\N{LATIN SMALL LIGATURE FI}le", "file"),
        ("\N{LATIN SMALL LIGATURE FFL}uent", "ffluent"),
        ("\N{FULLWIDTH LATIN SMALL LETTER F}\N{FULLWIDTH LATIN SMALL LETTER O}x", "fox"),
        ("\N{FULLWIDTH DIGIT TWO}024", "2024"),
    ],
)
def test_a_presentation_form_is_its_plain_spelling(raw: str, folded: str) -> None:
    assert canonical(raw) == folded


@pytest.mark.parametrize(
    "raw",
    [
        "\N{FULLWIDTH COMMA}\N{IDEOGRAPHIC FULL STOP}",
        "x\N{SUPERSCRIPT TWO}",
        "\N{CIRCLED DIGIT ONE}",
        "na\N{LATIN SMALL LETTER I WITH DIAERESIS}ve",
    ],
)
def test_other_compatibility_characters_are_kept(raw: str) -> None:
    assert canonical(raw) == unicodedata.normalize("NFC", raw)


@pytest.mark.parametrize(
    "brk", ["\r\n", "\r", "\v", "\f", "\x85", "\N{LINE SEPARATOR}", "\N{PARAGRAPH SEPARATOR}"]
)
def test_every_line_break_is_one_newline(brk: str) -> None:
    assert canonical(f"a{brk}b") == "a\nb"


@pytest.mark.parametrize(
    "hidden",
    [
        "\x1b",
        "\x00",
        "\x9b",
        "\N{ZERO WIDTH SPACE}",
        "\N{SOFT HYPHEN}",
        "\N{ZERO WIDTH NO-BREAK SPACE}",
        "\N{RIGHT-TO-LEFT OVERRIDE}",
        "\N{LEFT-TO-RIGHT MARK}",
        "\N{TAG LATIN SMALL LETTER A}",
    ],
)
def test_an_invisible_character_cannot_split_a_word(hidden: str) -> None:
    assert canonical(f"pass{hidden}word") == "password"


@pytest.mark.parametrize("shaping", ["\N{ZERO WIDTH JOINER}", "\N{ZERO WIDTH NON-JOINER}"])
def test_a_joiner_that_shapes_a_script_is_kept(shaping: str) -> None:
    assert canonical(f"\N{DEVANAGARI LETTER KA}{shaping}") == f"\N{DEVANAGARI LETTER KA}{shaping}"


def test_tabs_spaces_and_newlines_are_kept() -> None:
    assert canonical("a\tb  c\nd") == "a\tb  c\nd"


@pytest.mark.parametrize("raw", [*_hostile_text.ALL, f"cafe{ACUTE}", "\N{LATIN SMALL LIGATURE FI}"])
def test_canonical_is_idempotent(raw: str) -> None:
    assert canonical(canonical(raw)) == canonical(raw)


def test_identity_ignores_case_composition_and_spacing() -> None:
    assert identity_key(f"  Cafe{ACUTE}   Notes ") == identity_key(
        "caf\N{LATIN SMALL LETTER E WITH ACUTE} notes"
    )
    assert identity_key("Stra\N{LATIN SMALL LETTER SHARP S}e") == identity_key("STRASSE")
    assert identity_key("a\N{ZERO WIDTH SPACE}b") == identity_key("ab")


def test_identity_keeps_distinct_names_distinct() -> None:
    assert identity_key("notes") != identity_key("note s")
