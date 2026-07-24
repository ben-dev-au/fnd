"""CLI filter flags reach the searcher as typed state, not query text."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from fnd.cli import app

runner = CliRunner()


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Intercept Searcher.search and record how the CLI called it.

    __init__ is stubbed too: this asserts on argument handling only, and
    opening the real index would couple the test to whatever the developer
    happens to have indexed (and fail outright on a schema-version bump).
    """
    seen: dict[str, Any] = {}

    def fake_search(self: object, query: str, **kwargs: Any) -> list[Any]:
        seen["query"] = query
        seen.update(kwargs)
        return []

    monkeypatch.setattr("fnd.query.Searcher.__init__", lambda self, **kw: None)
    monkeypatch.setattr("fnd.query.Searcher.search", fake_search)
    monkeypatch.setattr("fnd.migrate.prompt_and_rebuild_or_exit", lambda **kw: None)
    return seen


def test_tag_flag_becomes_a_typed_filter(captured: dict[str, Any]) -> None:
    result = runner.invoke(app, ["search", "notes", "--tag", "recipe"])
    assert result.exit_code == 0, result.output
    assert captured["tag_filter"].include["frontmatter"] == frozenset({"recipe"})


def test_tag_flag_is_repeatable(captured: dict[str, Any]) -> None:
    runner.invoke(app, ["search", "notes", "--tag", "a", "--tag", "b"])
    assert captured["tag_filter"].include["frontmatter"] == frozenset({"a", "b"})


def test_not_tag_excludes(captured: dict[str, Any]) -> None:
    runner.invoke(app, ["search", "notes", "--not-tag", "draft"])
    assert captured["tag_filter"].exclude["frontmatter"] == frozenset({"draft"})


def test_tag_match_defaults_to_all(captured: dict[str, Any]) -> None:
    runner.invoke(app, ["search", "notes", "--tag", "a", "--tag", "b"])
    assert captured["tag_filter"].match_all is True


def test_tag_match_any(captured: dict[str, Any]) -> None:
    runner.invoke(app, ["search", "notes", "--tag", "a", "--tag-match", "any"])
    assert captured["tag_filter"].match_all is False


def test_tag_values_are_normalised(captured: dict[str, Any]) -> None:
    """CLI input goes through the same normalisation as indexed tags."""
    runner.invoke(app, ["search", "notes", "--tag", "#Recipe"])
    assert captured["tag_filter"].include["frontmatter"] == frozenset({"recipe"})


def test_hostile_tag_value_never_reaches_the_query(captured: dict[str, Any]) -> None:
    """A shell-supplied hostile value stays a literal in typed state."""
    runner.invoke(app, ["search", "notes", "--tag", 'evil" OR body:x OR "'])
    assert "evil" not in captured["query"]
    values = set().union(*captured["tag_filter"].include.values())
    assert 'evil" or body:x or "' in values


def test_created_flag_becomes_a_query_token(captured: dict[str, Any]) -> None:
    runner.invoke(app, ["search", "notes", "--created", "week"])
    assert "created:week" in captured["query"]


def test_modified_flag_becomes_a_query_token(captured: dict[str, Any]) -> None:
    runner.invoke(app, ["search", "notes", "--modified", "month"])
    assert "mtime:month" in captured["query"]


def test_kind_flag_is_repeatable(captured: dict[str, Any]) -> None:
    runner.invoke(app, ["search", "notes", "--kind", "pdf", "--kind", "md"])
    # Multiple --kind collapse into ONE OR-group so results match ANY of the
    # kinds; separate kind: clauses would AND and match nothing.
    assert "kind:(pdf md)" in captured["query"]


def test_kind_category_flag_expands_to_members(captured: dict[str, Any]) -> None:
    runner.invoke(app, ["search", "notes", "--kind", "code"])
    # A category id expands to an OR-group over its member kinds.
    assert "kind:(" in captured["query"]
    assert "python" in captured["query"]
    assert "cpp" in captured["query"]


def test_parse_filter_flags_expands_categories_for_the_tui_seed() -> None:
    """``parse_filter_flags`` expands category ids to fine-grained kinds in the
    LaunchScope, so the TUI (which seeds ``filter_kinds`` from it) emits
    index-compatible ``kind:`` clauses — a raw ``kind:code`` matches nothing."""
    from fnd.cli import parse_filter_flags
    from fnd.kinds import KINDS_IN_CATEGORY

    scope = parse_filter_flags(
        created=None, modified=None, kind=["code"], tag=[], not_tag=[], tag_match="all"
    )
    assert "code" not in scope.kinds, "category id must be expanded, not passed raw"
    assert set(scope.kinds) == set(KINDS_IN_CATEGORY["code"])
    # De-dup when a category and one of its members are both given.
    scope2 = parse_filter_flags(
        created=None, modified=None, kind=["code", "python"], tag=[], not_tag=[], tag_match="all"
    )
    assert len(scope2.kinds) == len(set(scope2.kinds))


def test_no_flags_passes_no_tag_filter(captured: dict[str, Any]) -> None:
    runner.invoke(app, ["search", "notes"])
    assert captured.get("tag_filter") is None


def test_invalid_date_token_is_rejected(captured: dict[str, Any]) -> None:
    result = runner.invoke(app, ["search", "notes", "--created", "fortnight"])
    assert result.exit_code != 0
    assert "fortnight" in result.output


def test_invalid_tag_match_is_rejected(captured: dict[str, Any]) -> None:
    result = runner.invoke(app, ["search", "notes", "--tag", "a", "--tag-match", "some"])
    assert result.exit_code != 0
