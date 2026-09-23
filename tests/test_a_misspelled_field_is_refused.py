"""A dotted name the fact registry does not define is a typo, not a field.

`RESERVED_FACTS` says so in its own comment: "Anything else dotted is a typo,
not a frontmatter key, so callers can reject it at parse time instead of
strict-nulling to False".

Unrejected, `file.kinds == 'pdf'` (one letter from `file.kind`) validates with
a ✓ in the live editor, matches nothing when it runs, and the tree quietly
drops the clause it cannot place; only the save refuses it, after the user has
been told twice that the rule is fine.
"""

from __future__ import annotations

import pytest

from fnd.file_facts import RESERVED_FACTS
from fnd.filters.text_form import parse_or_error


@pytest.mark.parametrize(
    "text",
    [
        "file.kinds == 'pdf'",
        "file.totally_bogus_field == 'x'",
        "file.Name == 'a.md'",
        "file.tags.finder == 'x'",
    ],
)
def test_an_unknown_field_is_refused(text: str) -> None:
    spec, err = parse_or_error(text)

    assert spec is None, f"{text!r} validated"
    assert err is not None
    assert "unknown field" in str(err), err


def test_the_message_names_the_field_and_the_alternatives() -> None:
    """One letter out is the whole point, so the message has to show the set."""
    _spec, err = parse_or_error("file.kinds == 'pdf'")

    assert err is not None
    assert "file.kinds" in str(err), err
    assert "file.kind" in str(err), "it did not offer what the user meant"


@pytest.mark.parametrize(
    "text",
    [
        "file.kind == 'pdf'",
        "file.size <= 10",
        "file.path == 'a/b.md'",
        "'x' in file.tags.all",
        "'x' in file.tags.os",
        "Course == 'Unstructured Data'",
        "file.size <= 10 AND Course == 'x'",
    ],
)
def test_every_real_field_still_parses(text: str) -> None:
    """The control, and it has to include a frontmatter key beside a fact:
    frontmatter keys cannot contain a dot, which is exactly why rejecting
    dotted unknowns catches nothing legitimate."""
    spec, err = parse_or_error(text)

    assert err is None, f"{text!r} was refused: {err}"
    assert spec is not None


def test_every_reserved_fact_is_accepted() -> None:
    """Derived from the registry, so a fact added there cannot be rejected by
    a guard that forgot about it."""
    for fact in sorted(RESERVED_FACTS):
        text = f"'x' in {fact}" if fact.startswith("file.tags") else f"{fact} == 'x'"
        _spec, err = parse_or_error(text)
        assert err is None or "unknown field" not in str(err), (fact, err)
