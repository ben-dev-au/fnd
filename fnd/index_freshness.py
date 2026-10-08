"""Whether a collection's index still matches its configuration.

A successful run records what it indexed with in one sidecar beside the shared
index, keyed by collection, and the config now is compared with that record. It
survives restarts, cannot go stale, and clears itself when a change is reverted;
a wiped index takes the sidecar with it, which correctly reads as not indexed.

Two remedies. Selection (which files belong) converges with an Update, which
prunes files the walk no longer reaches, index-time filters included. Extraction
(what is read out of each file) needs a rebuild, since an Update skips unchanged
files. The PDF engine is neither: its drift is owned by the texture stamps.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fnd.config import CollectionConfig, Config, Defaults

#: Beside `.fnd-schema-version`, so a schema wipe takes it with the documents.
SIDECAR_NAME = ".fnd-indexed-with.json"
#: Not a collection name: marks that the one-time adoption has run.
_ADOPTED = "__adopted__"

#: A source's selection inputs, named as the source form names them.
_SOURCE_FIELDS: dict[str, str] = {
    "path": "Path",
    "includes": "Restrict to these paths",
    "follow_symlinks": "Follow symlinks",
    "filters": "Index filters",
}


class State(Enum):
    """What the index is, against the config now: a state, never the work it wants."""

    CURRENT = "up to date"
    NEEDS_UPDATE = "outdated"
    NEEDS_REBUILD = "tags outdated"
    NOT_INDEXED = "not indexed"


@dataclass(frozen=True)
class Verdict:
    state: State
    reasons: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return self.state.value.capitalize()

    @property
    def because(self) -> str:
        """The settings changed since the last index, as Settings names them, or ""."""
        if not self.reasons:
            return ""
        names = list(self.reasons)
        joined = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
        return f"{joined} changed since the last index"

    @property
    def summary(self) -> str:
        return f"{self.label}: {self.because}" if self.reasons else self.label


def indexed_with(collection: CollectionConfig, defaults: Defaults) -> dict[str, Any]:
    """The inputs a run of ``collection`` indexes with, as plain JSON values."""
    from fnd.tags import frontmatter_key
    from fnd.walk import resolve_skip_dirs

    sources = [
        {
            "path": str(s.path),
            "includes": list(s.includes),
            "follow_symlinks": bool(s.follow_symlinks),
            "filters": {
                **s.effective_filters.model_dump(mode="json"),
                # Order never changes what the walk prunes.
                "excludes": sorted(set(s.effective_filters.excludes)),
                "legacy_frontmatter": s.legacy_frontmatter or "",
            },
        }
        for s in collection.sources
    ]
    inputs = {
        "selection": {"sources": sources, "junk_dirs": sorted(resolve_skip_dirs(defaults))},
        "extraction": {
            # Keys are matched case-insensitively, so case alone is no change.
            "tag_frontmatter_keys": sorted(
                {frontmatter_key(k) for k in defaults.tag_frontmatter_keys}
            ),
            "tag_sources": sorted(defaults.tag_sources),
        },
    }
    return json.loads(json.dumps(inputs))


def compare(now: dict[str, Any], recorded: dict[str, Any] | None) -> Verdict:
    """How the index stands against ``now``: a rebuild also converges the selection."""
    if recorded is None:
        return Verdict(State.NOT_INDEXED)
    rebuild = _extraction_reasons(now["extraction"], recorded.get("extraction") or {})
    if rebuild:
        return Verdict(State.NEEDS_REBUILD, rebuild)
    update = _selection_reasons(now["selection"], recorded.get("selection") or {})
    if update:
        return Verdict(State.NEEDS_UPDATE, update)
    return Verdict(State.CURRENT)


def _extraction_reasons(now: dict[str, Any], then: dict[str, Any]) -> tuple[str, ...]:
    reasons: list[str] = []
    if now["tag_frontmatter_keys"] != then.get("tag_frontmatter_keys"):
        reasons.append("Extra frontmatter tag keys")
    # Turning a source off hides its tags at search time; only one turned on is unread.
    if set(now["tag_sources"]) - set(then.get("tag_sources") or ()):
        reasons.append("Tag sources")
    return tuple(reasons)


def _as_today(source: dict[str, Any]) -> dict[str, Any]:
    """A recorded source in today's shape: excludes were a source field before
    they were a filter, so an upgrade alone must not read as a change."""
    filters = dict(source.get("filters") or {})
    filters.setdefault("excludes", source.get("excludes") or [])
    filters["excludes"] = sorted(set(filters["excludes"]))
    return {**{k: v for k, v in source.items() if k != "excludes"}, "filters": filters}


def _selection_reasons(now: dict[str, Any], then: dict[str, Any]) -> tuple[str, ...]:
    reasons: dict[str, None] = {}
    old = [_as_today(s) for s in then.get("sources") or []]
    if len(now["sources"]) != len(old):
        reasons["Sources"] = None
    for new_source, old_source in zip(now["sources"], old, strict=False):
        for key, word in _SOURCE_FIELDS.items():
            if new_source.get(key) != old_source.get(key):
                reasons[word] = None
    if now["junk_dirs"] != then.get("junk_dirs"):
        reasons["Skipped folders"] = None
    return tuple(reasons)


class Ledger:
    """What each collection was last indexed with, read once per change on disk."""

    def __init__(self, index_dir: Path) -> None:
        self._dir = index_dir
        self._path = index_dir / SIDECAR_NAME
        self._cache: tuple[tuple[int, int, int], dict[str, Any]] | None = None

    def recorded(self, name: str) -> dict[str, Any] | None:
        entry = self._read().get(name)
        return entry if isinstance(entry, dict) else None

    def verdict(self, name: str, collection: CollectionConfig, defaults: Defaults) -> Verdict:
        return compare(indexed_with(collection, defaults), self.recorded(name))

    def record(self, name: str, inputs: dict[str, Any]) -> None:
        data = dict(self._read())
        data[name] = inputs
        self._write(data)

    def mark_adopted(self) -> None:
        """Nothing to adopt: called when an index is created after the record existed."""
        data = dict(self._read())
        data[_ADOPTED] = True
        self._write(data)

    def forget(self, name: str) -> None:
        data = dict(self._read())
        if data.pop(name, None) is not None:
            self._write(data)

    def adopt(self, config: Config, is_empty: Callable[[str], bool]) -> list[str]:
        """Record the config now for collections indexed before the sidecar existed.

        Once it has run, a name missing from it is an unfinished run, so nothing is adopted."""
        data = dict(self._read())
        if data.get(_ADOPTED):
            return []
        adopted = [n for n in sorted(config.collections) if n not in data and not is_empty(n)]
        for name in adopted:
            data[name] = indexed_with(config.collections[name], config.defaults)
        data[_ADOPTED] = True
        self._write(data)
        return adopted

    def _read(self) -> dict[str, Any]:
        try:
            st = self._path.stat()
        except OSError:
            return {}
        # The inode too: a coarse clock gives two writes one mtime, and each write replaces the file.
        stamp = (st.st_mtime_ns, st.st_ino, st.st_size)
        if self._cache is not None and self._cache[0] == stamp:
            return self._cache[1]
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        data: dict[str, Any] = loaded if isinstance(loaded, dict) else {}
        self._cache = (stamp, data)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        from fnd._perms import secure_write_text

        if not self._dir.is_dir():
            return
        secure_write_text(self._path, json.dumps(data, indent=1, sort_keys=True), atomic=True)
        self._cache = None
