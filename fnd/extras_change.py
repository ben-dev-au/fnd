"""One install or removal of an extra, and the way back from a failed install.

An install touches four things besides fnd itself: packages (fnd's venv, or a uv tool), the
project's ``default-groups``, downloaded folders, and the additions record. :meth:`Change.begin`
notes each before anything runs, and :meth:`Change.roll_back` puts them back.

On Windows a loaded extension's files cannot be deleted. Removing packages this process has
loaded, or a folder still open, waits for the next start (:func:`finish_pending_removals`); the
deferring process holds a lock until it exits, so a second fnd started meanwhile leaves the queue.
A uv tool install cannot rebuild the venv fnd runs from at all, so the user is handed the command
to run after quitting (:attr:`Change.after_quit`).
"""

from __future__ import annotations

import importlib
import json
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO

from fnd import extras, os_labels, paths
from fnd.extras import Extra

__all__ = ["Change", "finish_pending_removals", "pending_removal", "remove_folders"]

Runner = Callable[[list[str]], tuple[int, str, str]]
_Pending = dict[str, dict[str, list[list[str]] | list[str]]]

# Held by a process that deferred a removal, for as long as it runs.
_in_use: IO[bytes] | None = None


def _extras_dir() -> Path:
    return paths.app_data_dir() / "extras"


def _pending_path() -> Path:
    return _extras_dir() / "pending-removal.json"


def _in_use_path() -> Path:
    return _extras_dir() / "runtime-in-use.lock"


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _pending() -> _Pending:
    raw = _read(_pending_path())
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _save_pending(pending: _Pending) -> None:
    path = _pending_path()
    if not pending:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pending), encoding="utf-8")


def _queue(
    name: str, *, commands: list[list[str]] | None = None, folders: list[Path] | None = None
) -> None:
    pending = _pending()
    entry = pending.setdefault(name, {"commands": [], "folders": []})
    entry["commands"] = [*entry["commands"], *(commands or [])]  # type: ignore[list-item]
    entry["folders"] = sorted({*map(str, entry["folders"]), *(str(f) for f in folders or [])})
    _save_pending(pending)


def _hold_in_use() -> None:
    global _in_use
    if _in_use is not None:
        return
    _in_use_path().parent.mkdir(parents=True, exist_ok=True)
    handle = _in_use_path().open("a+b")
    if paths.try_lock(handle):
        _in_use = handle
    else:
        handle.close()


def _runtime_in_use() -> bool:
    """Another fnd that deferred a removal still runs, with the packages loaded."""
    path = _in_use_path()
    if not path.exists():
        return False
    with path.open("a+b") as handle:
        if not paths.try_lock(handle):
            return True
        paths.unlock(handle)
    return False


def pending_removal(name: str) -> bool:
    """Whether removing ``name`` waits for fnd's next start."""
    return name in _pending()


def remove_folders(name: str, folders: list[Path]) -> bool:
    """Delete ``folders``; one still open is queued for the next start. True when all went now."""
    left = []
    for folder in folders:
        shutil.rmtree(folder, ignore_errors=True)
        if folder.exists():
            left.append(folder)
    if left:
        _queue(name, folders=left)
    return not left


def finish_pending_removals(
    run: Runner | None = None, *, in_use: Callable[[], bool] = _runtime_in_use
) -> list[str]:
    """Finish removals queued while their files were in use; the extras finished now."""
    pending = _pending()
    if not pending or in_use():
        return []
    run = run or extras.run_command
    done = []
    for name, entry in list(pending.items()):
        commands = [list(c) for c in entry.get("commands", [])]  # type: ignore[union-attr]
        if not all(run(c)[0] == 0 for c in commands):
            continue
        folders = [Path(str(f)) for f in entry.get("folders", [])]
        for folder in folders:
            shutil.rmtree(folder, ignore_errors=True)
        if any(f.exists() for f in folders):
            continue
        del pending[name]
        done.append(name)
    if done:
        _save_pending(pending)
    return done


def _modules(extra: Extra) -> list[str]:
    return [p.detect.split(":", 1)[1] for p in extra.packages if p.detect.startswith("module:")]


def _tools_installed(extra: Extra) -> frozenset[str]:
    return frozenset(
        p.spec.split("[", 1)[0]
        for p in extra.packages
        if p.install_via == "uv-tool" and extras.is_package_installed(p)
    )


def _rebuilds_fnd(argv: list[str]) -> bool:
    return argv[:3] == ["uv", "tool", "install"] and any(a.startswith("fndr") for a in argv)


@dataclass(frozen=True)
class Change:
    """A confirmed install or removal: its commands, and the state it started from."""

    extra: Extra
    removing: bool
    commands: list[list[str]]
    # Removing the packages waits for the next start: this process has them loaded.
    deferred: bool
    files: dict[Path, bytes | None]
    folders: frozenset[Path]
    dists: frozenset[str]
    tools: frozenset[str]
    # The project's default-groups membership before, or None outside a project.
    grouped: bool | None = None
    # A command the user must run once fnd has quit.
    after_quit: str = ""

    @classmethod
    def begin(
        cls,
        extra: Extra,
        *,
        removing: bool,
        loaded: Callable[[str], bool] = lambda module: module in sys.modules,
    ) -> Change:
        pyproject = extras.project_pyproject_for_python(sys.executable)
        grouped = None
        if pyproject is not None and extras.group_exists_in_pyproject(pyproject, extra.name):
            grouped = extras.in_default_groups(pyproject, extra.name)
        watched = [extras.record_path(extra.name, p) for p in ("before", "after", "added")]
        watched.append(_pending_path())
        importlib.invalidate_caches()
        change = cls(
            extra=extra,
            removing=removing,
            commands=[],
            deferred=False,
            files={p: _read(p) for p in watched},
            folders=frozenset(f for f in extra.cache_dirs() if f.exists()),
            dists=frozenset(extras.installed_names()),
            tools=_tools_installed(extra),
            grouped=grouped,
        )
        if grouped is not None and pyproject is not None:
            extras.set_default_group(pyproject, extra.name, present=not removing)
        if removing:
            commands = extras.uninstall_commands(extra)
        else:
            pending = _pending()
            if pending.pop(extra.name, None) is not None:
                _save_pending(pending)
            commands = extras.install_commands(extra)
        if paths.loaded_files_are_locked() and any(_rebuilds_fnd(c) for c in commands):
            after = " && ".join(os_labels.command_line(c) for c in commands if _rebuilds_fnd(c))
            kept = [c for c in commands if not _rebuilds_fnd(c)]
            return replace(change, commands=kept, after_quit=after)
        held = any(loaded(m) for m in _modules(extra))
        if removing and commands and paths.loaded_files_are_locked() and held:
            _queue(extra.name, commands=commands)
            _hold_in_use()
            return replace(change, deferred=True)
        return replace(change, commands=commands)

    def roll_back(self, run: Runner | None = None) -> str:
        """Put back what a failed or cancelled install changed; one line saying what."""
        run = run or extras.run_command
        undone: list[str] = []
        failed: list[str] = []
        for folder in self.extra.cache_dirs():
            if folder not in self.folders and folder.exists():
                shutil.rmtree(folder, ignore_errors=True)
                (failed if folder.exists() else undone).append("downloaded files")
        importlib.invalidate_caches()
        added = sorted(extras.installed_names() - self.dists - extras.fnd_requirements() - {"fndr"})
        if added:
            rc, _out, _err = run(["uv", "pip", "uninstall", "--python", sys.executable, *added])
            (failed if rc else undone).append(f"{len(added)} packages")
        for tool in sorted(_tools_installed(self.extra) - self.tools):
            rc, _out, _err = run(["uv", "tool", "uninstall", tool])
            (failed if rc else undone).append(tool)
        pyproject = extras.project_pyproject_for_python(sys.executable)
        if self.grouped is not None and pyproject is not None:
            extras.set_default_group(pyproject, self.extra.name, present=self.grouped)
        for path, data in self.files.items():
            if _read(path) == data:
                continue
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(data)
        if failed:
            return f"Could not remove: {', '.join(dict.fromkeys(failed))}."
        if undone:
            return f"Removed what it added ({', '.join(dict.fromkeys(undone))})."
        return "Nothing was kept."
