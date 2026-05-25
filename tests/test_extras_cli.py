"""Verify the `fnd extras` CLI surface.

Requirements covered:
- F2: `fnd extras list` shows available + installed status
- F3: `fnd extras install pdf-structure` shows disclosure prompt, then runs install
- F4: `fnd extras uninstall pdf-structure` shows prompt, then removes
- NF2: install prompts disclose disk size + network impact before any download
- NF3: aborting the prompt leaves no install artifacts

Tests use Typer's CliRunner and --dry-run mode so they don't actually
mutate the venv. Verifies command shape, prompt content, and exit codes.
"""

from __future__ import annotations

from typer.testing import CliRunner

from fnd.cli import app


def _run(*argv: str) -> tuple[int, str]:
    runner = CliRunner()
    result = runner.invoke(app, list(argv), catch_exceptions=False)
    return result.exit_code, result.output


def test_extras_list_shows_pdf_structure() -> None:
    """F2: list output mentions the pdf-structure extra."""
    code, out = _run("extras", "list")
    assert code == 0
    assert "pdf-structure" in out
    # Status word must be one of the two known states.
    assert ("installed" in out) or ("not installed" in out)


def test_extras_status_runs() -> None:
    """F2 / NF6: status reports per-extra installed state and disk usage
    without crashing."""
    code, out = _run("extras", "status")
    assert code == 0
    assert "pdf-structure" in out


def test_install_dry_run_discloses_disk_and_network() -> None:
    """NF2: install disclosure must mention disk and the no-extras
    fallback so the user knows what they're opting into."""
    code, out = _run("extras", "install", "pdf-structure", "--dry-run")
    assert code == 0
    # Disclosure components:
    assert "MB" in out or "GB" in out, "must show approximate size"
    assert "Will install" in out, "must enumerate packages"
    assert "flat text" in out or "current behaviour" in out, (
        "must remind users of the fallback if they don't opt in"
    )
    # Should print the commands it would run (dry-run).
    assert "would run" in out, "must show planned subprocess commands"
    assert "uv sync" in out or "uv tool install" in out


def test_install_dry_run_discloses_indexing_time_for_pdf_structure() -> None:
    """NF12: the pdf-structure install disclosure must include the
    indexing-time impact so users know the structured-PDF flow
    requires hours of one-time CPU on real corpora."""
    code, out = _run("extras", "install", "pdf-structure", "--dry-run")
    assert code == 0
    assert "Indexing-time impact" in out, (
        "must include the indexing-time section explaining the ~30s/PDF cost"
    )
    assert "30s" in out or "per PDF" in out
    assert "auto-resumes" in out or "background" in out, (
        "must reassure the user that the long indexing is interruptible"
    )


def test_install_unknown_extra_fails_cleanly() -> None:
    """NF3: unknown extra name → exit code != 0, no partial state."""
    code, out = _run("extras", "install", "nonexistent-extra", "--dry-run")
    assert code != 0
    assert "unknown extra" in out.lower() or "unknown extra" in out


def test_uninstall_dry_run_discloses_removed_packages() -> None:
    """F4: uninstall disclosure must enumerate what would be removed."""
    code, out = _run("extras", "uninstall", "pdf-structure", "--dry-run")
    assert code == 0
    assert "Will remove" in out
    assert "would run" in out, "must show planned uninstall commands"
    # F9: tell the user the index is preserved.
    assert "index" in out.lower(), (
        "must explain that the index isn't touched, so previews of "
        "already-indexed PDFs still work after uninstall"
    )


def test_uninstall_unknown_extra_fails_cleanly() -> None:
    code, out = _run("extras", "uninstall", "nonexistent-extra", "--dry-run")
    assert code != 0
    assert "unknown extra" in out.lower()


def test_install_aborted_when_user_declines() -> None:
    """NF3: declining the confirmation prompt aborts before any install
    command runs."""
    runner = CliRunner()
    # input="" answers the typer.confirm prompt with the default (False).
    # No --yes / --dry-run so it goes through the confirmation path.
    result = runner.invoke(
        app, ["extras", "install", "pdf-structure"], input="n\n", catch_exceptions=False
    )
    # Aborted should exit non-zero (Typer.Exit(code=1)).
    assert result.exit_code != 0
    assert "aborted" in result.output.lower()
    # And it should NOT have run any subprocess commands (no "$ uv ..." log).
    assert "$ uv sync" not in result.output
    assert "$ uv tool install" not in result.output
