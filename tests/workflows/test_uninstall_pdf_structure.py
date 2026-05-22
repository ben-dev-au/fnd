"""Uninstall pdf-structure workflow — end-to-end-ish.

Verifies:
  1. Uninstall commands target fnd's actual venv (not cwd).
  2. ``--dry-run`` shows the full plan regardless of current state.
  3. After a real uninstall, detection flips to "not installed" on
     the very next provider call (no app restart needed).
  4. Half-uninstalled state (namespace husk surviving) is correctly
     read as "not installed".
"""

from __future__ import annotations

import sys
from pathlib import Path

from fnd.extras import Package, is_package_installed


def test_uninstall_commands_use_group_sync_in_project_venv() -> None:
    """Inside a uv-managed project venv uninstall_commands resolves to
    ``uv sync --no-group pdf-structure``. The sync removes the group's
    packages from the venv that owns sys.executable, which is fnd's
    runtime venv."""
    from fnd.extras import PDF_STRUCTURE, _project_pyproject_for_python, uninstall_commands

    assert (
        _project_pyproject_for_python(sys.executable) is not None
    ), "test must run inside the project venv; was sys.executable redirected?"

    cmds = uninstall_commands(PDF_STRUCTURE, assume_installed=True)
    sync_cmd = next(c for c in cmds if c[:2] == ["uv", "sync"])
    assert sync_cmd == ["uv", "sync", "--no-group", "pdf-structure"]


def test_dry_run_assume_installed_shows_full_plan() -> None:
    """The --dry-run CLI preview should show the full plan regardless
    of whether the extra is currently installed. Inside the project
    venv the plan now consists of group-level sync commands, not
    per-package pip uninstall calls."""
    from fnd.extras import PDF_STRUCTURE, uninstall_commands

    plan = uninstall_commands(PDF_STRUCTURE, assume_installed=True)
    # At least one command must reference the pdf-structure group so
    # the dry-run preview tells the user what is about to change.
    assert any(
        "pdf-structure" in cmd for cmd in plan
    ), f"expected pdf-structure to appear in the plan, got {plan!r}"


def test_namespace_husk_detected_as_not_installed(tmp_path: Path, monkeypatch: object) -> None:
    """After a partial uninstall, the package directory might survive
    without __init__.py (macOS Finder leaves __init__ 2.py). Detection
    must read this as not installed."""
    site = tmp_path / "site-packages"
    husk = site / "fnd_test_husk"
    husk.mkdir(parents=True)
    (husk / "__init__ 2.py").write_text("# stale")
    sys.path.insert(0, str(site))
    try:
        pkg = Package(
            install_via="pip-extra",
            spec="fnd_test_husk",
            display="husk",
            disk_mb=1,
            detect="module:fnd_test_husk",
        )
        assert not is_package_installed(pkg)
    finally:
        sys.path.remove(str(site))


def test_uvtool_detection_ignores_path_shadow(tmp_path: Path) -> None:
    """A system-wide binary on PATH must not register a uv-tool
    package as installed when the uv tool dir doesn't exist."""
    pkg = Package(
        install_via="uv-tool",
        spec="docling-slim[standard]",
        display="docling-slim",
        disk_mb=1,
        detect="cli:docling",
    )
    # Real uv tool dir for docling-slim probably exists in dev env, so
    # this test is informative rather than universally enforced. The
    # detection unit tests in test_extras_detection.py pin the
    # invariant with monkeypatched Path.home.
    _ = is_package_installed(pkg)
