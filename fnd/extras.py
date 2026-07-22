"""Opt-in extras: structured PDF rendering, future image previews, etc.

Each extra wraps two install operations:

1. A package installed into fnd's runtime environment. In a uv-managed
   project checkout that's a PEP 735 group from ``[dependency-groups]``
   installed via ``uv sync --group <name>``; for a standalone (pipx /
   Homebrew) install it's a ``uv pip install`` into fnd's interpreter.
2. Optional ``uv tool install`` packages that live in their own isolated
   venv on PATH (used when a transitive version conflict would otherwise
   wedge fnd's environment).

The user invokes the extras via ``fnd extras install|uninstall|list|status``.
The CLI surface lives in :mod:`fnd.cli`; this module holds the data
structures, detection logic, disk accounting, and subprocess wrappers.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fnd import paths

InstallVia = Literal["pip-extra", "uv-tool"]


@dataclass(frozen=True)
class Package:
    """One installable unit within an extra."""

    install_via: InstallVia
    # For pip-extra: name of the [project.optional-dependencies] group.
    # For uv-tool: full tool spec, e.g. 'docling-slim[standard]'.
    spec: str
    # User-friendly display name (what the install summary shows).
    display: str
    # Rough estimate of installed disk in MB, for the disclosure prompt.
    disk_mb: int
    # How to detect this package is currently installed.
    detect: str  # "module:NAME" or "cli:NAME"
    # Explicit pip package names to pass to ``uv pip uninstall`` for
    # pip-extra packages. ``uv sync`` (no extras) was previously used
    # to remove these, but it doesn't reliably clean partial
    # installations (e.g. when macOS Finder leaves "name 2.py"
    # detritus behind). Direct ``uv pip uninstall`` is surgical. For
    # uv-tool packages this field is ignored.
    uninstall_targets: tuple[str, ...] = ()


@dataclass(frozen=True)
class Extra:
    name: str
    description: str
    packages: list[Package] = field(default_factory=list)
    # Optional cache directories whose disk usage we should report and
    # offer to remove on uninstall (e.g. downloaded model weights).
    cache_dirs: list[Path] = field(default_factory=list)


PDF_STRUCTURE = Extra(
    name="pdf-structure",
    description="Structured PDF rendering — headings, lists, tables, bold/italic.",
    packages=[
        Package(
            install_via="pip-extra",
            spec="pdf-structure",
            display="pymupdf4llm + pymupdf-layout (Polyform Noncommercial)",
            disk_mb=200,
            detect="module:pymupdf4llm",
            # ``uv sync`` (no extras) doesn't reliably clean up
            # pymupdf4llm's site-packages dir. Explicitly remove the
            # base package plus its ``[layout]`` extra so the dir is
            # gone after uninstall.
            uninstall_targets=("pymupdf4llm", "pymupdf-layout"),
        ),
        Package(
            install_via="uv-tool",
            spec="docling-slim[standard]",
            display="docling-slim[standard] (Apache-2.0)",
            disk_mb=700,
            detect="cli:docling",
        ),
    ],
    # ML model weights live alongside docling-slim's uv-tool install
    # (``~/.local/share/uv/tools/docling-slim/...``) which is already
    # walked by ``actual_disk_mb`` via ``tool_root``. The ``bakeoff/``
    # path that used to be here was a Phase 0 harness leftover — fnd
    # never wrote there in production. cache_dirs stays empty.
    cache_dirs=[],
)

EXTRAS: dict[str, Extra] = {PDF_STRUCTURE.name: PDF_STRUCTURE}


# -- Detection ---------------------------------------------------------------


def is_package_installed(pkg: Package) -> bool:
    """Detect installation reality, not Python's import cache.

    Multiple layers of stale state can fool naive detection:

    - The path-importer cache keeps the FileFinder for a directory
      after one module has been resolved — even when the files have
      been removed by an out-of-process tool. ``invalidate_caches``
      clears that.
    - A regular package whose ``__init__.py`` has been removed but
      whose parent directory still exists (macOS Finder occasionally
      leaves ``__init__ 2.py`` after a sync conflict; uv's atomic
      rename has been observed to do similar) is matched as a
      *namespace* package — origin is None, submodule_search_locations
      points at an empty husk.
    - For ``cli:NAME`` detection, ``shutil.which`` returns ANY binary
      with that name on ``PATH``. A user with a system-wide
      ``docling`` (from another Python install) would be detected as
      "installed" even after ``uv tool uninstall docling-slim``
      removed fnd's copy. For uv-tool-installed packages, check the
      uv tool install dir directly — that's what fnd manages.

    A "really installed" module needs either a real ``origin`` file
    or at least one submodule search location containing
    ``__init__.{py,so}``. Anything else is detritus.
    """
    importlib.invalidate_caches()
    kind, name = pkg.detect.split(":", 1)
    if kind == "module":
        spec = importlib.util.find_spec(name)
        if spec is None:
            return False
        origin = getattr(spec, "origin", None)
        if origin and origin not in ("built-in", "frozen"):
            return Path(origin).exists()
        # Namespace-package path: only really installed if at least
        # one search location actually has an init module.
        for path in getattr(spec, "submodule_search_locations", None) or []:
            d = Path(path)
            if not d.exists():
                continue
            if (d / "__init__.py").exists():
                return True
            if any(d.glob("__init__.*.so")):
                return True
        return False
    if kind == "cli":
        # For uv-tool packages, ignore PATH — a system-wide binary
        # with the same name isn't what fnd installed. Check the uv
        # tool root specifically.
        if pkg.install_via == "uv-tool":
            tool_name = pkg.spec.split("[", 1)[0]
            tool_root = paths.uv_tool_root() / tool_name
            return tool_root.exists()
        return shutil.which(name) is not None
    return False


def is_extra_installed(extra: Extra) -> bool:
    return all(is_package_installed(p) for p in extra.packages)


def installed_packages(extra: Extra) -> list[Package]:
    return [p for p in extra.packages if is_package_installed(p)]


# -- Disk accounting ---------------------------------------------------------


def _du_mb(path: Path) -> int:
    """Approximate disk usage of a path in MB. Returns 0 if missing."""
    if not path.exists():
        return 0
    total = 0
    for root, _dirs, files in __import__("os").walk(path):
        for f in files:
            with contextlib.suppress(OSError):
                total += (Path(root) / f).stat().st_size
    return total // (1024 * 1024)


def actual_disk_mb(extra: Extra) -> int:
    """Sum disk used by installed packages + cache dirs of `extra`."""
    total = 0
    for c in extra.cache_dirs:
        total += _du_mb(c)
    # For uv-tool installs, walk uv's tool root (uv tool dir)/<pkg>
    tool_root = paths.uv_tool_root()
    for pkg in extra.packages:
        if pkg.install_via == "uv-tool":
            tool_name = pkg.spec.split("[", 1)[0]
            total += _du_mb(tool_root / tool_name)
    # pip-extra packages live in fnd's venv; harder to attribute.
    # Use the disk_mb estimate for those.
    for pkg in extra.packages:
        if pkg.install_via == "pip-extra" and is_package_installed(pkg):
            total += pkg.disk_mb
    return total


# -- Install / uninstall actions --------------------------------------------


def install_commands(extra: Extra) -> list[list[str]]:
    """Return the subprocess argv for each install step.

    Strategy depends on whether fnd's runtime ``sys.executable`` lives
    inside a uv-managed project venv:

    - **uv-managed project venv** (the developer case — ``sys.executable``
      is ``<project>/.venv/bin/python3.X`` and the project's
      ``pyproject.toml`` declares a matching ``[dependency-groups].<name>``):
      install via ``uv sync --group <name>``. The caller is also
      expected to flip the group into ``[tool.uv] default-groups`` via
      :func:`enable_pdf_structure_default_group` so subsequent
      ``uv sync`` calls (without a ``--group`` flag) don't wipe the
      install. This is the persistence fix for "I installed but fnd
      says not installed after restart" — without the default-group
      flip, any ``uv run`` re-syncs the venv and removes the extra.

    - **uv-tool install / standalone** (``sys.executable`` is in a
      uv tool venv or a system Python): install via
      ``uv pip install --python <sys.executable> <req>``. Tool venvs
      aren't managed by ``uv sync`` so the install persists.
    """
    import sys

    cmds: list[list[str]] = []
    py = sys.executable
    project_pyproject = _project_pyproject_for_python(py)

    for p in extra.packages:
        if p.install_via == "pip-extra":
            if project_pyproject is not None and _group_exists_in_pyproject(
                project_pyproject, _group_name_for_pkg(p)
            ):
                # Project venv path: use --group; persistence relies on
                # the caller flipping default-groups before sync runs.
                cmds.append(["uv", "sync", "--group", _group_name_for_pkg(p)])
            else:
                for req in _pip_install_specs(p):
                    cmds.append(["uv", "pip", "install", "--python", py, req])
        elif p.install_via == "uv-tool":
            cmds.append(["uv", "tool", "install", p.spec])
    return cmds


def _group_name_for_pkg(pkg: Package) -> str:
    """The PEP 735 dependency-group name fnd uses for the package's
    runtime extras. For pdf-structure the spec is already the group
    name. Other pip-extras would follow the same convention."""
    return pkg.spec


def _project_pyproject_for_python(py: str) -> Path | None:
    """Walk up from ``sys.executable``'s venv to find the owning
    pyproject.toml. Returns the path if found, else None (e.g.,
    fnd is installed via uv tool / system python and there's no
    project to manage)."""
    bin_dir = Path(py).parent
    venv_root = bin_dir.parent
    parent = venv_root.parent
    candidate = parent / "pyproject.toml"
    return candidate if candidate.exists() else None


def _group_exists_in_pyproject(pyproject: Path, group_name: str) -> bool:
    """Cheap check: does ``[dependency-groups].<group_name>`` appear
    in the file? Avoids importing a TOML parser at hot-path import
    time; the substring match is good enough because the section
    name is unambiguous in fnd's pyproject."""
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return False
    return group_name in text and "[dependency-groups]" in text


def enable_pdf_structure_default_group(pyproject: Path) -> None:
    """Add ``pdf-structure`` to ``[tool.uv] default-groups`` so a
    subsequent ``uv sync`` (without a ``--group`` flag) keeps the
    install. Idempotent — no-op when the group is already in the
    list. Preserves comments + ordering via tomlkit."""
    _ensure_group_membership(pyproject, "pdf-structure", present=True)


def disable_pdf_structure_default_group(pyproject: Path) -> None:
    """Remove ``pdf-structure`` from ``[tool.uv] default-groups`` so
    ``uv sync`` no longer keeps it. Idempotent."""
    _ensure_group_membership(pyproject, "pdf-structure", present=False)


def _ensure_group_membership(pyproject: Path, group: str, *, present: bool) -> None:
    import tomlkit

    if not pyproject.exists():
        return
    doc = tomlkit.parse(pyproject.read_text(encoding="utf-8"))
    tool = doc.setdefault("tool", tomlkit.table())
    uv = tool.setdefault("uv", tomlkit.table())  # type: ignore[union-attr]
    groups = uv.get("default-groups", None)  # type: ignore[union-attr]
    if groups is None:
        groups = tomlkit.array()
        if present:
            groups.append("dev")
            groups.append(group)
        uv["default-groups"] = groups  # type: ignore[union-attr,index]
    else:
        as_list = list(groups)
        if present and group not in as_list:
            groups.append(group)
        elif not present and group in as_list:
            # tomlkit Array exposes remove
            with contextlib.suppress(ValueError):
                groups.remove(group)
    pyproject.write_text(tomlkit.dumps(doc), encoding="utf-8")


def _pip_install_specs(pkg: Package) -> list[str]:
    """Pip requirement strings for a pip-extra Package. Defaults to
    the package's ``uninstall_targets`` so install/uninstall pair up.
    Falls back to the bare module name."""
    if pkg.uninstall_targets:
        # Use the bare names; uv pip install resolves them via the
        # lockfile / pyproject. pymupdf-layout rides in as a base
        # requirement of pymupdf4llm at ~=1.27 (the old `[layout]`
        # extra no longer exists), so installing the base package is
        # enough — requesting the extra would only emit a uv warning.
        primary = pkg.detect.split(":", 1)[1]
        return [primary] if primary == "pymupdf4llm" else list(pkg.uninstall_targets)
    return [pkg.detect.split(":", 1)[1]]


def uninstall_commands(extra: Extra, *, assume_installed: bool = False) -> list[list[str]]:
    """Surgical uninstall commands.

    Two robustness rules:

    1. **Skip packages that aren't installed.** A user re-attempting an
       uninstall after a previous partial run shouldn't see "exit 2"
       because one of the chain commands tried to uninstall something
       that's already gone. ``assume_installed=True`` overrides this
       for planning-only callers (``--dry-run`` CLI preview, confirm
       screens) that want to show the FULL plan regardless of state.

    2. **Use ``uv pip uninstall`` for pip-extras.** ``uv sync`` (no
       extras specified) was previously used here, but it has been
       observed to leave the package's site-packages dir behind in
       some half-removed states (the user's case: an
       ``__init__ 2.py`` macOS Finder duplicate survives the sync).
       Direct ``uv pip uninstall <pkg>`` reliably removes the dir.
       Each package's ``uninstall_targets`` lists the actual pip
       package names to remove (base + transitive extras).
    """
    import sys

    cmds: list[list[str]] = []
    py = sys.executable
    project_pyproject = _project_pyproject_for_python(py)

    for p in extra.packages:
        if not assume_installed and not is_package_installed(p):
            continue
        if p.install_via == "uv-tool":
            cmds.append(["uv", "tool", "uninstall", p.spec.split("[", 1)[0]])
        elif p.install_via == "pip-extra":
            group_name = _group_name_for_pkg(p)
            if project_pyproject is not None and _group_exists_in_pyproject(
                project_pyproject, group_name
            ):
                # Project venv path: ``uv sync --no-group <name>``.
                # Caller is expected to drop the group from
                # default-groups via disable_pdf_structure_default_group
                # so the next bare ``uv sync`` keeps it removed.
                cmds.append(["uv", "sync", "--no-group", group_name])
            else:
                targets = list(p.uninstall_targets) or [p.detect.split(":", 1)[1]]
                if assume_installed:
                    installed_targets = targets
                else:
                    installed_targets = [t for t in targets if _pip_target_installed(t)]
                if installed_targets:
                    cmds.append(["uv", "pip", "uninstall", "--python", py, *installed_targets])
    return cmds


def _pip_target_installed(name: str) -> bool:
    """Lightweight ``uv pip show NAME`` probe against fnd's actual
    Python. Used to skip uninstall steps for transitive extras that
    aren't actually installed (e.g. ``pymupdf-layout`` when only the
    base ``pymupdf4llm`` survived)."""
    import sys

    proc = subprocess.run(
        ["uv", "pip", "show", "--python", sys.executable, name],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def run_command(argv: list[str]) -> tuple[int, str, str]:
    """Run an install/uninstall command. Returns (exitcode, stdout, stderr)."""
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


__all__ = [
    "EXTRAS",
    "PDF_STRUCTURE",
    "Extra",
    "Package",
    "actual_disk_mb",
    "install_commands",
    "installed_packages",
    "is_extra_installed",
    "is_package_installed",
    "run_command",
    "uninstall_commands",
]
