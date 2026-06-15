"""Generated number<->word synonym groups (cardinals + ordinals).

Spelled numbers and their digit forms are synonyms: a search for ``4`` should
surface ``four`` and vice versa. Rather than hand-list ~60 groups in the
curated TOML, we build them from two word->digit maps at load time and merge
them into the default :class:`~fnd.synonyms.SynonymTable`. Quoted terms are
left literal by ``expand`` itself, so ``"4"`` never expands.

Scope is bounded to the forms that appear unprefixed in prose: cardinals
0-20, the round tens, hundred, and thousand, plus the matching ordinals.
Compounds (``twenty-four`` / ``24``, ``twenty-first``) need a parser, not a
table, and are out of scope.

Cardinal and ordinal forms are kept in separate groups: ``4`` is not a synonym
of ``fourth``.
"""

from __future__ import annotations

from fnd.synonyms import SynonymTable

# word -> digit. Maps (not loops over parallel lists) so the digit for a name
# is a direct O(1) read and the source stays self-documenting.
CARDINALS: dict[str, str] = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
    "thirty": "30",
    "forty": "40",
    "fifty": "50",
    "sixty": "60",
    "seventy": "70",
    "eighty": "80",
    "ninety": "90",
    "hundred": "100",
    "thousand": "1000",
}

# word -> digit-ordinal (``first`` -> ``1st``).
ORDINALS: dict[str, str] = {
    "first": "1st",
    "second": "2nd",
    "third": "3rd",
    "fourth": "4th",
    "fifth": "5th",
    "sixth": "6th",
    "seventh": "7th",
    "eighth": "8th",
    "ninth": "9th",
    "tenth": "10th",
    "eleventh": "11th",
    "twelfth": "12th",
    "thirteenth": "13th",
    "fourteenth": "14th",
    "fifteenth": "15th",
    "sixteenth": "16th",
    "seventeenth": "17th",
    "eighteenth": "18th",
    "nineteenth": "19th",
    "twentieth": "20th",
    "thirtieth": "30th",
    "fortieth": "40th",
    "fiftieth": "50th",
    "sixtieth": "60th",
    "seventieth": "70th",
    "eightieth": "80th",
    "ninetieth": "90th",
    "hundredth": "100th",
    "thousandth": "1000th",
}


def number_synonym_groups() -> list[list[str]]:
    """Bidirectional ``[digit, word]`` groups for every cardinal and ordinal.

    Digit-first so ``expand`` renders ``(4 OR four)`` — the original surface
    form leads regardless, this just fixes the alternative's order."""
    return [[digit, word] for word, digit in (*CARDINALS.items(), *ORDINALS.items())]


def build_number_table() -> SynonymTable:
    """The number groups as a standalone :class:`SynonymTable`."""
    return SynonymTable.from_groups(number_synonym_groups())
