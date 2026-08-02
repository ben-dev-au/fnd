"""A filter value the user got wrong is reported, not silently obeyed.

Before this, ``-c dpc2`` against a ``DPC2`` collection failed two ways:
``fnd search`` built a hard term filter that matched nothing, and the TUI
launch path dropped the unknown name and widened the search to every
collection. Both were silent. So was ``--kind pdff``.

Every problem on one command line is now collected and reported together, so
two typos cost one re-run rather than two.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fnd.cli import _rewrite_default_command, app
from fnd.config import CollectionConfig, SourceConfig
from fnd.index import build_index_from_config

runner = CliRunner()

# Kwargs each stubbed FNDApp construction was called with — empty means the
# TUI never got as far as being built.
_built: list[dict[str, object]] = []


@pytest.fixture
def corpus(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two real collections — ``DPC2`` (mixed case, the reported bug) and
    ``papers`` — indexed, with a config TOML the CLI actually loads."""
    for name in ("DPC2", "papers"):
        root = tmp_path / name
        root.mkdir()
        (root / "a.md").write_text(f"# {name}\nlightning rod\n", encoding="utf-8")
        build_index_from_config(
            config=CollectionConfig(sources=[SourceConfig(path=root, includes=["**/*.md"])]),
            collection=name,
            index_dir=tmp_index_dir,
        )

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.DPC2.sources]]
            path = "{(tmp_path / "DPC2").as_posix()}"
            includes = ["**/*.md"]

            [[collections.papers.sources]]
            path = "{(tmp_path / "papers").as_posix()}"
            includes = ["**/*.md"]
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.cli.default_index_dir", lambda: tmp_index_dir)
    monkeypatch.setattr("fnd.cli.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return tmp_index_dir


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the interactive branch — same escape hatch fnd.migrate uses."""
    monkeypatch.setenv("_FND_FORCE_TTY", "1")


@pytest.fixture
def stub_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record FNDApp construction instead of spinning up Textual."""
    _built.clear()

    class _Stub:
        def __init__(self, **kwargs: object) -> None:
            _built.append(kwargs)

        def run(self, **_: object) -> None: ...

    monkeypatch.setattr("fnd.tui.FNDApp", _Stub)
    monkeypatch.setattr("fnd.extract._worker.warm_pool", lambda: None)


def _search(*argv: str, input: str = "") -> tuple[int, str]:
    result = runner.invoke(app, ["search", *argv], input=input, catch_exceptions=False)
    return result.exit_code, result.output


# ── one bad value ─────────────────────────────────────────────────────────


def test_wrong_case_collection_is_offered_and_accepted(corpus: Path, tty: None) -> None:
    code, out = _search("lightning", "-c", "dpc2", input="y\n")
    assert code == 0, out
    assert "no collection named 'dpc2'" in out
    assert "Did you mean 'DPC2'?" in out
    # Accepting scopes the search — the papers copy must not appear.
    assert "DPC2" in out
    assert "papers" not in out


def test_declining_the_suggestion_exits_without_searching(corpus: Path, tty: None) -> None:
    code, out = _search("lightning", "-c", "dpc2", input="n\n")
    assert code == 2, out
    assert "aborted" in out
    assert "a.md" not in out


def test_non_tty_reports_instead_of_prompting(corpus: Path) -> None:
    """Piped or scripted, the prompt would hang — print the fix instead."""
    code, out = _search("lightning", "-c", "dpc2")
    assert code == 2, out
    assert "no collection named 'dpc2'" in out
    assert "--collection DPC2" in out
    assert "a.md" not in out


def test_unknown_with_no_near_match_lists_what_exists(corpus: Path, tty: None) -> None:
    code, out = _search("lightning", "-c", "zzzzzzzz")
    assert code == 2, out
    assert "no collection named 'zzzzzzzz'" in out
    assert "DPC2" in out
    assert "papers" in out
    assert "did you mean" not in out.lower()


def test_unknown_kind_is_rejected_with_a_suggestion(corpus: Path) -> None:
    code, out = _search("lightning", "--kind", "pdff")
    assert code == 2, out
    assert "no file kind named 'pdff'" in out
    assert "'pdf'" in out


def test_kind_case_is_canonicalised_silently(corpus: Path) -> None:
    """``--kind PDF`` is unambiguous — resolve it, don't interrogate the user."""
    code, out = _search("lightning", "--kind", "PDF")
    assert code == 0, out
    assert "did you mean" not in out.lower()


def test_in_query_collection_token_reports_but_never_prompts(corpus: Path, tty: None) -> None:
    """A ``c:`` token inside the query is reported, not rewritten — silently
    editing text the user typed is worse than telling them what to change."""
    code, out = _search("c:dpc2 lightning", input="y\n")
    assert code == 2, out
    assert "no collection named 'dpc2'" in out
    assert "[Y/n]" not in out
    assert "a.md" not in out


# ── several bad values at once ────────────────────────────────────────────


def test_two_bad_flags_share_one_confirmation(corpus: Path, tty: None) -> None:
    code, out = _search("lightning", "-c", "dpc2", "--kind", "pdff", input="y\n")
    assert code == 0, out
    assert "2 filter values weren't recognised" in out
    assert "--collection dpc2" in out
    assert "--kind pdff" in out
    # One prompt, not one per problem.
    assert out.count("Use these instead?") == 1


def test_declining_a_batch_names_every_problem(corpus: Path, tty: None) -> None:
    code, out = _search("lightning", "-c", "dpc2", "--kind", "pdff", input="n\n")
    assert code == 2, out
    assert "dpc2" in out
    assert "pdff" in out


def test_two_unknown_names_in_one_flag_are_both_corrected(corpus: Path, tty: None) -> None:
    code, out = _search("lightning", "-c", "dpc2,PAPERS", input="y\n")
    assert code == 0, out
    assert "2 filter values weren't recognised" in out


def test_one_unfixable_problem_suppresses_the_prompt(corpus: Path, tty: None) -> None:
    """Confirming would still leave a broken command, so don't offer."""
    code, out = _search("lightning", "-c", "dpc2", "--kind", "zzzzzzzz")
    assert code == 2, out
    assert "dpc2" in out
    assert "zzzzzzzz" in out
    assert "[Y/n]" not in out
    # A partial "re-run with" would still leave a broken command.
    assert "Re-run with" not in out


def test_bad_date_and_bad_tag_match_report_together(corpus: Path) -> None:
    """Previously the date token exited before tag-match was even checked."""
    code, out = _search("lightning", "--created", "fortnight", "--tag-match", "some")
    assert code != 0, out
    assert "fortnight" in out
    assert "some" in out


# ── the default path must not change ──────────────────────────────────────


def test_no_collection_flag_searches_everything(corpus: Path) -> None:
    code, out = _search("lightning")
    assert code == 0, out
    assert "DPC2" in out
    assert "papers" in out


@pytest.mark.parametrize("flag", ["all", "All"])
def test_dash_c_all_still_searches_everything(corpus: Path, flag: str) -> None:
    code, out = _search("lightning", "-c", flag)
    assert code == 0, out
    assert "DPC2" in out
    assert "papers" in out


def test_correct_name_is_unaffected(corpus: Path) -> None:
    code, out = _search("lightning", "-c", "DPC2")
    assert code == 0, out
    assert "did you mean" not in out.lower()
    assert "papers" not in out


def test_comma_list_scopes_to_both(corpus: Path) -> None:
    """Regression: ``-c a,b`` used to become one phantom term matching nothing."""
    code, out = _search("lightning", "-c", "DPC2,papers")
    assert code == 0, out
    assert "DPC2" in out
    assert "papers" in out


def test_a_collection_literally_named_all_still_wins(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "all"
    root.mkdir()
    (root / "a.md").write_text("# all\nlightning rod\n", encoding="utf-8")
    build_index_from_config(
        config=CollectionConfig(sources=[SourceConfig(path=root, includes=["**/*.md"])]),
        collection="all",
        index_dir=tmp_index_dir,
    )
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.all.sources]]
            path = "{root.as_posix()}"
            includes = ["**/*.md"]
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.cli.default_index_dir", lambda: tmp_index_dir)
    monkeypatch.setattr("fnd.cli.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    code, out = _search("lightning", "-c", "all")
    assert code == 0, out
    assert "did you mean" not in out.lower()


# ── other command surfaces ────────────────────────────────────────────────


def _launch(*argv: str, input: str = "") -> tuple[int, str, list[dict[str, object]]]:
    """Drive the bare-``fnd`` path. CliRunner doesn't go through ``sys.argv``,
    so feed it the rewriter's output the way tests/test_cli_routing.py does."""
    result = runner.invoke(
        app, _rewrite_default_command(list(argv)), input=input, catch_exceptions=False
    )
    return result.exit_code, result.output, _built


def test_tui_launch_declined_never_builds_the_app(corpus: Path, tty: None, stub_app: None) -> None:
    """The prompt must land on a plain terminal, before Textual takes over."""
    code, out, built = _launch("-c", "dpc2", "lightning", input="n\n")
    assert code == 2, out
    assert "Did you mean 'DPC2'?" in out
    assert built == []


def test_tui_launch_accepted_opens_on_the_real_collection(
    corpus: Path, tty: None, stub_app: None
) -> None:
    """The reported bug: this used to widen the scope to every collection."""
    code, out, built = _launch("-c", "dpc2", "lightning", input="y\n")
    assert code == 0, out
    assert built, out
    assert built[0]["collection"] == "DPC2"


def test_tui_launch_unknown_collection_does_not_widen_to_everything(
    corpus: Path, stub_app: None
) -> None:
    code, out, built = _launch("-c", "zzzzzzzz", "lightning")
    assert code == 2, out
    assert built == []


def test_reindex_typo_gets_a_suggestion_not_a_traceback(corpus: Path) -> None:
    result = runner.invoke(app, ["collection", "reindex", "dpc2"], catch_exceptions=False)
    assert result.exit_code == 2, result.output
    assert "no collection named 'dpc2'" in result.output
    assert "DPC2" in result.output


def test_config_validate_flags_a_dangling_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [defaults]
            collection = "dpc2"

            [[collections.DPC2.sources]]
            path = "{tmp_path.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.cli.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)

    result = runner.invoke(app, ["config", "validate"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "warning" in result.output.lower()
    assert "DPC2" in result.output
