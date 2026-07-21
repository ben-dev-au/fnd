"""The filter flags shared by ``fnd search`` and ``fnd tui``.

``parse_filter_flags`` is the one validator both commands use, and the
``copy_query_command`` action is wired into the keymap.
"""

from __future__ import annotations

import pytest
import typer

from fnd.cli import parse_filter_flags
from fnd.launch_command import LaunchScope
from fnd.tui.actions import REGISTRY, load_keymap


def test_parses_into_launch_scope() -> None:
    scope = parse_filter_flags(
        created="week",
        modified="month",
        kind=["pdf", "md"],
        tag=["red"],
        not_tag=["draft"],
        tag_match="any",
    )
    assert scope == LaunchScope(
        created="week",
        modified="month",
        kinds=("pdf", "md"),
        tags=("red",),
        not_tags=("draft",),
        tag_match_all=False,
    )


def test_empty_scope_is_falsy() -> None:
    scope = parse_filter_flags(
        created=None, modified=None, kind=[], tag=[], not_tag=[], tag_match="all"
    )
    assert not scope


@pytest.mark.parametrize("bad", ["bogus", "fortnight", "2024"])
def test_bad_date_token_exits(bad: str) -> None:
    with pytest.raises(typer.Exit) as exc:
        parse_filter_flags(created=bad, modified=None, kind=[], tag=[], not_tag=[], tag_match="all")
    assert exc.value.exit_code == 1


def test_bad_tag_match_exits() -> None:
    with pytest.raises(typer.Exit) as exc:
        parse_filter_flags(
            created=None, modified=None, kind=[], tag=[], not_tag=[], tag_match="nope"
        )
    assert exc.value.exit_code == 1


def test_copy_command_action_is_registered() -> None:
    action = next((a for a in REGISTRY if a.id == "copy_query_command"), None)
    assert action is not None
    assert action.default_key == "ctrl+y"
    assert action.priority is True
    assert load_keymap().bindings.get("ctrl+y") == "copy_query_command"
