"""Opt-in extras: structured PDF rendering, future image previews, etc.

Each extra wraps two install operations:

1. A pip extra group in `pyproject.toml` (`[project.optional-dependencies]`)
   installed via ``uv sync --extra <group>`` into fnd's project venv.
2. Optional ``uv tool install`` packages that live in their own isolated
   venv on PATH (used when a transitive version conflict would otherwise
   wedge fnd's project venv).

The user invokes the extras via ``fnd extras install|uninstall|list|status``.
The CLI surface lives in :mod:`fnd.cli`; this module holds the data
structures, detection logic, disk accounting, and subprocess wrappers.
"""

from __future__ import annotations

import contextlib
import importlib.util
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from platformdirs import user_cache_dir

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
            display="pymupdf4llm[layout] (Polyform Noncommercial)",
            disk_mb=200,
            detect="module:pymupdf4llm",
        ),
        Package(
            install_via="uv-tool",
            spec="docling-slim[standard]",
            display="docling-slim[standard] (Apache-2.0)",
            disk_mb=700,
            detect="cli:docling",
        ),
    ],
    cache_dirs=[
        Path(user_cache_dir("fnd")) / "bakeoff" / "docling",
        Path(user_cache_dir("fnd")) / "docling-models",
    ],
)

EXTRAS: dict[str, Extra] = {PDF_STRUCTURE.name: PDF_STRUCTURE}


# -- Detection ---------------------------------------------------------------


def is_package_installed(pkg: Package) -> bool:
    kind, name = pkg.detect.split(":", 1)
    if kind == "module":
        return importlib.util.find_spec(name) is not None
    if kind == "cli":
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
    # For uv-tool installs, walk ~/.local/share/uv/tools/<pkg>
    tool_root = Path.home() / ".local" / "share" / "uv" / "tools"
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
    """Return the subprocess argv for each install step. Pure function;
    the caller decides when (and whether) to run them."""
    cmds: list[list[str]] = []
    pip_extras = [p.spec for p in extra.packages if p.install_via == "pip-extra"]
    if pip_extras:
        extra_args = [arg for spec in pip_extras for arg in ("--extra", spec)]
        cmds.append(["uv", "sync", *extra_args])
    for p in extra.packages:
        if p.install_via == "uv-tool":
            cmds.append(["uv", "tool", "install", p.spec])
    return cmds


def uninstall_commands(extra: Extra) -> list[list[str]]:
    """Return the subprocess argv for each uninstall step."""
    cmds: list[list[str]] = []
    for p in extra.packages:
        if p.install_via == "uv-tool":
            cmds.append(["uv", "tool", "uninstall", p.spec.split("[", 1)[0]])
    pip_extras = [p.spec for p in extra.packages if p.install_via == "pip-extra"]
    if pip_extras:
        # `uv sync` without --extra removes the pip-extras from the env.
        cmds.append(["uv", "sync"])
    return cmds


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
