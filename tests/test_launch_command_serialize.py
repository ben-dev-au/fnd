"""Serialising a search into a runnable ``fnd`` launch command.

Pure unit tests over ``SearchSnapshot`` — no app, no index.
"""

from __future__ import annotations

from fnd.launch_command import LaunchCommandSerializer, SearchSnapshot


def _cmd(**kwargs: object) -> str:
    return LaunchCommandSerializer(SearchSnapshot(**kwargs)).serialize().command  # type: ignore[arg-type]


def test_plain_query() -> None:
    assert _cmd(query="cabernet") == "fnd cabernet"


def test_query_with_spaces_is_quoted() -> None:
    assert _cmd(query="cabernet aging") == "fnd 'cabernet aging'"


def test_empty_query_is_bare_and_flagged_empty() -> None:
    result = LaunchCommandSerializer(SearchSnapshot(query="")).serialize()
    assert result.command == "fnd"
    assert result.is_empty is True


def test_single_collection() -> None:
    assert _cmd(query="q", full_collections=("wine",)) == "fnd q -c wine"


def test_multiple_collections_comma_joined() -> None:
    # -c splits on commas, so several full collections join into one value.
    assert _cmd(query="q", full_collections=("wine", "notes")) == "fnd q -c wine,notes"


def test_partial_collection_widens_with_caveat() -> None:
    result = LaunchCommandSerializer(
        SearchSnapshot(query="q", full_collections=("wine",), partial_collections=("notes",))
    ).serialize()
    assert result.command == "fnd q -c wine,notes"
    assert result.caveats == ["partial source selections widened to full collection(s)"]


def test_created_and_modified() -> None:
    assert (
        _cmd(query="q", filter_created="week", filter_date="month")
        == "fnd q --created week --modified month"
    )


def test_any_dates_are_omitted() -> None:
    assert _cmd(query="q", filter_created="any", filter_date="any") == "fnd q"


def test_kinds_repeat() -> None:
    assert _cmd(query="q", filter_kinds=("pdf", "md")) == "fnd q --kind pdf --kind md"


def test_tags_include_exclude_sorted() -> None:
    assert (
        _cmd(
            query="q",
            tag_include={"frontmatter": frozenset({"white", "red"})},
            tag_exclude={"os": frozenset({"draft"})},
        )
        == "fnd q --tag red --tag white --not-tag draft"
    )


def test_tag_match_any_only_with_includes() -> None:
    assert (
        _cmd(query="q", tag_include={"f": frozenset({"red"})}, tag_match_all=False)
        == "fnd q --tag red --tag-match any"
    )
    # match_all is a mode — meaningless (and omitted) without include tags.
    assert _cmd(query="q", tag_match_all=False) == "fnd q"


def test_tags_union_across_sources() -> None:
    # The CLI has no per-source tag flag, so provenance collapses to one set.
    assert (
        _cmd(query="q", tag_include={"frontmatter": frozenset({"red"}), "os": frozenset({"red"})})
        == "fnd q --tag red"
    )


def test_special_characters_are_shell_quoted() -> None:
    cmd = _cmd(query="a & b", full_collections=("my wine",), tag_include={"f": frozenset({"a'b"})})
    assert cmd == "fnd 'a & b' -c 'my wine' --tag 'a'\"'\"'b'"


def test_full_command_ordering() -> None:
    assert (
        _cmd(
            query="cabernet aging",
            full_collections=("wine",),
            filter_created="week",
            filter_date="month",
            filter_kinds=("pdf",),
            tag_include={"frontmatter": frozenset({"red"})},
            tag_exclude={"os": frozenset({"draft"})},
            tag_match_all=False,
        )
        == "fnd 'cabernet aging' -c wine --created week --modified month "
        "--kind pdf --tag red --not-tag draft --tag-match any"
    )
