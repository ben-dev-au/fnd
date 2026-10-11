"""Excluded folders as paths relative to a source root: normalising, coverage and ticking.

A stored list holds only the top-most folders; a folder is excluded when it or an ancestor is listed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

_BACKSLASH_SEPARATES = os.sep == "\\"
_DRIVE = re.compile(r"^[A-Za-z]:")


def normalise_dir(text: str) -> str:
    """``text`` as a stored folder path, or ``ValueError`` saying why it cannot be one."""
    raw = text.replace("\\", "/") if _BACKSLASH_SEPARATES else text
    if not raw.strip():
        raise ValueError("a folder path must not be empty")
    if raw.startswith("/") or _DRIVE.match(raw):
        raise ValueError(f"{text!r} must be relative to the source folder")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts:
        raise ValueError("that is the source folder itself")
    if ".." in parts:
        raise ValueError(f"{text!r} must stay inside the source folder")
    return "/".join(parts)


def fold(dirs: Iterable[str]) -> tuple[str, ...]:
    """The top-most folders of ``dirs``, in first-seen order."""
    unique = tuple(dict.fromkeys(dirs))
    return tuple(d for d in unique if not any(d.startswith(k + "/") for k in unique))


def state(dirs: Sequence[str], path: str) -> Literal["on", "part", "off"]:
    """Whether ``path`` is excluded, partly excluded below, or indexed."""
    if any(path == d or path.startswith(d + "/") for d in dirs):
        return "on"
    prefix = path + "/"
    return "part" if any(d.startswith(prefix) for d in dirs) else "off"


def toggle(
    dirs: Sequence[str], path: str, known: Iterable[str], read: Iterable[str]
) -> tuple[str, ...] | None:
    """``dirs`` with ``path`` flipped, or ``None`` when flipping it needs folders never listed.

    ``known`` is every folder the picker shows; ``read`` the ones whose children were all listed.
    """
    if path in dirs:
        return tuple(d for d in dirs if d != path)
    covering = next((d for d in dirs if path.startswith(d + "/")), None)
    if covering is None:
        return (*(d for d in dirs if not d.startswith(path + "/")), path)
    listed = set(read)
    parts = path.split("/")
    keep: list[str] = []
    for depth in range(covering.count("/") + 1, len(parts)):
        parent = "/".join(parts[:depth])
        if parent not in listed:
            return None
        on_chain = "/".join(parts[: depth + 1])
        keep += [k for k in known if k.rpartition("/")[0] == parent and k != on_chain]
    return (*(d for d in dirs if d != covering), *keep)


def exists_exactly(root: Path, rel: str) -> bool:
    """Whether ``rel`` is a folder under ``root``, matching the case as written."""
    current = root
    for part in rel.split("/"):
        try:
            names = os.listdir(current)
        except OSError:
            return False
        if part not in names:
            return False
        current = current / part
    return current.is_dir()
