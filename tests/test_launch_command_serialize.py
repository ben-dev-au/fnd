"""Serialising a search into a runnable ``fnd`` launch command.

Pure unit tests over ``SearchSnapshot`` — no app, no index.
"""

from __future__ import annotations

import pytest

from fnd.launch_command import LaunchCommandSerializer, SearchSnapshot


def _cmd(**kwargs: object) -> str:
    return LaunchCommandSerializer(SearchSnapshot(**kwargs)).serialize().command  # type: ignore[arg-type]


def test_plain_query() -> None:
    assert _cmd(query="risotto") == "fnd risotto"


def test_query_with_spaces_is_quoted() -> None:
    assert _cmd(query="risotto recipe") == "fnd 'risotto recipe'"


def test_empty_query_is_bare_and_flagged_empty() -> None:
    result = LaunchCommandSerializer(SearchSnapshot(query="")).serialize()
    assert result.command == "fnd"
    assert result.is_empty is True


def test_single_collection() -> None:
    assert _cmd(query="q", full_collections=("home",)) == "fnd q -c home"


def test_multiple_collections_comma_joined() -> None:
    # -c splits on commas, so several full collections join into one value.
    assert _cmd(query="q", full_collections=("home", "notes")) == "fnd q -c home,notes"


def test_partial_collection_widens_with_caveat() -> None:
    result = LaunchCommandSerializer(
        SearchSnapshot(query="q", full_collections=("home",), partial_collections=("notes",))
    ).serialize()
    assert result.command == "fnd q -c home,notes"
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
    cmd = _cmd(query="a & b", full_collections=("my home",), tag_include={"f": frozenset({"a'b"})})
    assert cmd == "fnd 'a & b' -c 'my home' --tag 'a'\"'\"'b'"


def test_every_arg_kind_is_quoted() -> None:
    # Centralised shlex.join quotes kinds/dates too, so an odd value can't
    # silently split the pasted command.
    assert _cmd(query="q", filter_kinds=("we ird",)) == "fnd q --kind 'we ird'"


def test_full_command_ordering() -> None:
    assert (
        _cmd(
            query="risotto recipe",
            full_collections=("home",),
            filter_created="week",
            filter_date="month",
            filter_kinds=("pdf",),
            tag_include={"frontmatter": frozenset({"red"})},
            tag_exclude={"os": frozenset({"draft"})},
            tag_match_all=False,
        )
        == "fnd 'risotto recipe' -c home --created week --modified month "
        "--kind pdf --tag red --not-tag draft --tag-match any"
    )


def _parsed_query(command: str) -> str:
    import shlex

    import typer

    from fnd import cli

    argv = cli._rewrite_default_command(shlex.split(command)[1:])
    tui = typer.main.get_group(cli.app).commands[argv[0]]
    return " ".join(tui.make_context(argv[0], argv[1:]).params["query"])


@pytest.mark.parametrize(
    "query", ["-draft report", "--help", "-c notes", "version", "index", "plain words"]
)
def test_the_command_hands_back_the_query_it_was_given(query: str) -> None:
    assert _parsed_query(_cmd(query=query, full_collections=("notes",))) == query


def test_the_reserved_words_are_the_cli_s() -> None:
    import typer

    from fnd import cli
    from fnd.launch_command import SUBCOMMANDS

    assert set(typer.main.get_group(cli.app).commands) == SUBCOMMANDS
