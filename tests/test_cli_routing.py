"""Default-action routing: bare `fnd` and `fnd <free-text>` go to the TUI."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from fnd.cli import _rewrite_default_command, app


class TestRewriteDefaultCommand:
    def test_empty_argv_routes_to_tui(self) -> None:
        assert _rewrite_default_command([]) == ["tui"]

    def test_single_query_routes_to_tui(self) -> None:
        assert _rewrite_default_command(["pizza"]) == ["tui", "pizza"]

    def test_query_with_collection_flag_routes_to_tui(self) -> None:
        # `--collection` is unknown at the root group; it belongs to `tui`.
        # The rewrite must put `tui` first so Click parses the flag at the
        # subcommand level.
        assert _rewrite_default_command(["--collection", "notes", "pizza"]) == [
            "tui",
            "--collection",
            "notes",
            "pizza",
        ]

    def test_short_collection_flag_also_routes_to_tui(self) -> None:
        assert _rewrite_default_command(["-c", "notes", "pizza"]) == [
            "tui",
            "-c",
            "notes",
            "pizza",
        ]

    def test_known_subcommand_wins(self) -> None:
        # Every registered subcommand passes through unchanged.
        for cmd in ("version", "index", "tui", "search", "config", "collection"):
            assert _rewrite_default_command([cmd, "arg"]) == [cmd, "arg"]

    def test_help_flags_pass_through(self) -> None:
        assert _rewrite_default_command(["--help"]) == ["--help"]
        assert _rewrite_default_command(["-h"]) == ["-h"]
        assert _rewrite_default_command(["--install-completion"]) == ["--install-completion"]
        assert _rewrite_default_command(["--show-completion"]) == ["--show-completion"]

    def test_quoted_multiword_query(self) -> None:
        # The shell collapses `fnd "two words"` into a single argv token.
        assert _rewrite_default_command(["two words"]) == ["tui", "two words"]


class TestEndToEndRouting:
    """`CliRunner` doesn't go through `sys.argv`, so we feed it the
    rewriter's output directly. Mocks `FNDApp` to avoid spinning up Textual."""

    def test_bare_invocation_launches_tui_with_empty_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_app = MagicMock()
        monkeypatch.setattr("fnd.tui.FNDApp", mock_app)
        monkeypatch.setattr("fnd.migrate.prompt_and_rebuild_or_exit", lambda **_: None)

        result = CliRunner().invoke(app, _rewrite_default_command([]))

        assert result.exit_code == 0, result.output
        assert mock_app.called
        assert mock_app.call_args.kwargs["initial_query"] == ""
        assert mock_app.call_args.kwargs["collection"] is None

    def test_positional_query_seeds_tui(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_app = MagicMock()
        monkeypatch.setattr("fnd.tui.FNDApp", mock_app)
        monkeypatch.setattr("fnd.migrate.prompt_and_rebuild_or_exit", lambda **_: None)

        result = CliRunner().invoke(app, _rewrite_default_command(["pizza"]))

        assert result.exit_code == 0, result.output
        assert mock_app.call_args.kwargs["initial_query"] == "pizza"

    def test_collection_and_positional_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_app = MagicMock()
        monkeypatch.setattr("fnd.tui.FNDApp", mock_app)
        monkeypatch.setattr("fnd.migrate.prompt_and_rebuild_or_exit", lambda **_: None)

        result = CliRunner().invoke(
            app, _rewrite_default_command(["--collection", "notes", "pizza"])
        )

        assert result.exit_code == 0, result.output
        assert mock_app.call_args.kwargs["collection"] == "notes"
        assert mock_app.call_args.kwargs["initial_query"] == "pizza"

    def test_hidden_query_flag_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Back-compat: `fnd tui -q pizza` still seeds the query.
        mock_app = MagicMock()
        monkeypatch.setattr("fnd.tui.FNDApp", mock_app)
        monkeypatch.setattr("fnd.migrate.prompt_and_rebuild_or_exit", lambda **_: None)

        result = CliRunner().invoke(app, ["tui", "-q", "pizza"])

        assert result.exit_code == 0, result.output
        assert mock_app.call_args.kwargs["initial_query"] == "pizza"
