"""Three places where the Filters pane contradicted itself or the index.

The title and the clear row describe the same state one row apart and counted
different things; the title dropped the one word that changes the answer by
3.3x; and the Tags branch reported a fact about the INDEX when it only knew a
fact about the current scope.
"""

from __future__ import annotations

from fnd.tui.scope_panel import _tags_summary, filters_title


def test_an_empty_scope_says_scope_not_index() -> None:
    """Ten tags were still indexed; the user had narrowed the scope to empty.

    The neighbouring File type branch says `(1 of 1)` in the same situation.
    """
    said = _tags_summary(0, 0, sources_on=True, compact=False)

    assert "indexed" not in said, said
    assert "scope" in said, said


def test_the_tag_sources_being_off_still_says_so() -> None:
    """The control: a different cause needs a different word, and this one is
    about the user's own setting rather than about the scope."""
    said = _tags_summary(0, 0, sources_on=False, compact=False)

    assert "sources off" in said, said


def test_the_title_says_which_way_two_tags_are_matched() -> None:
    """`Match: all` and `Match: any` gave the identical title and 3.3x the
    results. The mode's own row is inside the Tags branch, so it is invisible
    exactly when the branch is collapsed and the title is all there is."""

    def title(*, match_all: bool) -> str:
        return filters_title(
            n_kinds=0,
            date="any",
            created="any",
            n_included_tags=2,
            n_excluded_tags=0,
            match_all=match_all,
        )

    all_of = title(match_all=True)
    any_of = title(match_all=False)

    assert all_of != any_of, all_of
    assert "2 tags all" in all_of, all_of
    assert "2 tags any" in any_of, any_of


def test_one_tag_says_no_mode() -> None:
    """The control: with one tag the mode changes nothing, and a word that
    changes nothing is clutter on a row built to carry little."""
    said = filters_title(
        n_kinds=0,
        date="any",
        created="any",
        n_included_tags=1,
        n_excluded_tags=0,
        match_all=False,
    )

    assert said == "Filters — 1 tag", said
