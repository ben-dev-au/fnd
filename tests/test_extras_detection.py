"""Detection-precision tests for ``fnd.extras.is_package_installed``.

Regression coverage for two real-world failure modes that left the
Settings menu showing "Uninstall pdf-structure" after pdf-structure
had in fact been removed:

1. A pip-extra package whose ``__init__.py`` was removed but whose
   parent directory survives (macOS Finder occasionally leaves
   ``__init__ 2.py`` after a sync race). ``find_spec`` treats this as
   a namespace package, so a bare origin-existence check returns the
   wrong answer.

2. A ``cli:NAME`` detection that resolves a system-wide binary with
   the same name as the uv-tool install. ``shutil.which`` returns the
   shadow binary, fnd assumes its uv-tool copy is still installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.extras import Package, is_package_installed


def _make_module_package(detect_name: str) -> Package:
    return Package(
        install_via="pip-extra",
        spec="something",
        display="something",
        disk_mb=10,
        detect=f"module:{detect_name}",
    )


def _make_uvtool_package(spec: str, detect_cli: str) -> Package:
    return Package(
        install_via="uv-tool",
        spec=spec,
        display=spec,
        disk_mb=10,
        detect=f"cli:{detect_cli}",
    )


# ── Namespace-package detritus ─────────────────────────────────────


def test_module_with_real_init_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regular package with ``__init__.py`` on disk is installed."""
    site = tmp_path / "site-packages"
    pkg = site / "fnd_test_real"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("# real\n")
    monkeypatch.syspath_prepend(str(site))
    assert is_package_installed(_make_module_package("fnd_test_real"))


def test_module_with_only_garbage_files_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A namespace-shaped directory whose ``__init__.py`` has been
    removed (only "__init__ 2.py" or similar detritus remains) is NOT
    installed — find_spec would otherwise treat it as a namespace
    package and the naive check would lie."""
    site = tmp_path / "site-packages"
    pkg = site / "fnd_test_husk"
    pkg.mkdir(parents=True)
    (pkg / "__init__ 2.py").write_text("# macOS Finder duplicate\n")
    (pkg / "helpers 2").mkdir()
    monkeypatch.syspath_prepend(str(site))
    assert not is_package_installed(_make_module_package("fnd_test_husk"))


def test_module_with_compiled_init_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compiled init (e.g. C extension shipping as ``__init__.*.so``)
    counts as installed even though there's no ``__init__.py``."""
    site = tmp_path / "site-packages"
    pkg = site / "fnd_test_compiled"
    pkg.mkdir(parents=True)
    (pkg / "__init__.cpython-313-darwin.so").write_bytes(b"")
    monkeypatch.syspath_prepend(str(site))
    assert is_package_installed(_make_module_package("fnd_test_compiled"))


def test_missing_module_is_not_installed() -> None:
    assert not is_package_installed(_make_module_package("fnd_test_does_not_exist"))


# ── uv-tool path precision ─────────────────────────────────────────


def test_uv_tool_package_uses_uv_dir_not_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For ``install_via='uv-tool'``, detection must look at
    ``~/.local/share/uv/tools/<name>`` — NOT at PATH. A system-wide
    binary with the same name (e.g. user already has a ``docling`` from
    a separate Python install) must not register as fnd's install.

    Implementation: ``Path.home()`` is consulted at call time, so
    monkeypatching ``Path.home`` redirects the lookup to ``tmp_path``."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    pkg = _make_uvtool_package("docling-slim[standard]", "docling")

    # No uv tool installed yet → not installed regardless of PATH.
    assert not is_package_installed(pkg)

    # Simulate uv tool install having created the tool dir.
    tool_dir = tmp_path / ".local" / "share" / "uv" / "tools" / "docling-slim"
    tool_dir.mkdir(parents=True)
    assert is_package_installed(pkg)


def test_uv_tool_ignores_system_binary_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even when ``shutil.which`` would resolve a system-wide binary,
    uv-tool detection must NOT use that — it'd misreport fnd's install
    state when the user has unrelated copies of the same CLI on PATH."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    binary = fake_bin / "docling"
    binary.write_text("#!/bin/sh\necho fake\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))

    pkg = _make_uvtool_package("docling-slim[standard]", "docling")
    # PATH-resolved docling exists but the uv tool dir doesn't.
    # Detection must return False.
    assert not is_package_installed(pkg)
