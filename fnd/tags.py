"""Tag extraction, one provider per source.

Sources are registered rather than branched on, so adding Windows or Linux is a
new class plus a registry entry instead of an edit to a working function.
Providers never raise: a tag read must not fail an index run.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import plistlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "MAX_TAGS_PER_FILE",
    "MAX_TAG_LEN",
    "TAG_PROVIDERS",
    "FrontmatterTagProvider",
    "MacOSFinderTagProvider",
    "TagContext",
    "TagProvider",
    "expand_ancestors",
    "normalise_tag",
    "providers_for",
    "read_tags",
]

# Bounds. Tag values come from file content, so they are untrusted input.
MAX_TAGS_PER_FILE = 256
MAX_TAG_LEN = 128

_TAG_KEYS = ("tags", "tag")


def normalise_tag(raw: str) -> str:
    """Strip a leading ``#``, collapse whitespace, casefold, bound length.

    Returns ``""`` for anything unusable, which callers drop. Casefolding stops
    ``Recipe`` and ``recipe`` becoming two buckets in the tag pane.
    """
    text = raw.strip()
    if text.startswith("#"):
        text = text[1:]
    text = " ".join(text.split())
    return text.casefold()[:MAX_TAG_LEN]


def expand_ancestors(tag: str) -> set[str]:
    """``a/b/c`` -> ``{a, a/b, a/b/c}``.

    Nested-tag ancestors become real index values so a parent's file count is
    exact by construction (no union query, no summing) and matching a parent is
    a plain term query. Empty segments are dropped, so ``a//b`` and ``/a``
    normalise cleanly.
    """
    parts = [p for p in tag.split("/") if p]
    return {"/".join(parts[: i + 1]) for i in range(len(parts))}


@dataclass(slots=True, frozen=True)
class TagContext:
    """What a provider needs.

    ``frontmatter`` arrives already parsed so the frontmatter provider doesn't
    re-read a file the indexer just read.
    """

    path: Path
    frontmatter: dict[str, object] | None


@runtime_checkable
class TagProvider(Protocol):
    """One tag source. ``id`` doubles as the config key."""

    @property
    def id(self) -> str: ...

    def available_on(self, platform: str) -> bool: ...

    def read(self, ctx: TagContext) -> frozenset[str]: ...


def _collect(values: object, out: set[str]) -> None:
    """Normalise and ancestor-expand into ``out``, bounded, ignoring junk."""
    items = values if isinstance(values, list) else [values]
    for item in items:
        if len(out) >= MAX_TAGS_PER_FILE:
            return
        if not isinstance(item, (str, int, float, bool)):
            continue
        tag = normalise_tag(str(item))
        if tag:
            out |= expand_ancestors(tag)


class FrontmatterTagProvider:
    """YAML frontmatter ``tags:`` / ``tag:``. Portable to every platform."""

    id = "frontmatter"

    def available_on(self, platform: str) -> bool:
        return True

    def read(self, ctx: TagContext) -> frozenset[str]:
        fm = ctx.frontmatter
        if not fm:
            return frozenset()
        out: set[str] = set()
        for key in _TAG_KEYS:
            if key in fm:
                _collect(fm[key], out)
        if len(out) > MAX_TAGS_PER_FILE:
            return frozenset(sorted(out)[:MAX_TAGS_PER_FILE])
        return frozenset(out)


# Finder tags live in an xattr holding a binary plist. os.getxattr is
# Linux-only and absent on macOS, and /usr/bin/xattr costs a process spawn
# per file (measured ~192x slower), so this calls libc directly. macOS's
# getxattr takes two extra arguments (position, options) versus Linux —
# one reason the call stays inside the platform-specific provider.
_XATTR_USER_TAGS = b"com.apple.metadata:_kMDItemUserTags"


def _load_getxattr() -> object | None:
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        fn = libc.getxattr
    except (OSError, AttributeError):
        return None
    fn.restype = ctypes.c_ssize_t
    fn.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    return fn


_GETXATTR = _load_getxattr() if sys.platform == "darwin" else None


def _read_xattr(path: Path, name: bytes) -> bytes | None:
    """Raw xattr value, or None when absent, unreadable, or unsupported."""
    if _GETXATTR is None:
        return None
    try:
        encoded = os.fsencode(path)
    except (ValueError, TypeError):
        return None
    size = _GETXATTR(encoded, name, None, 0, 0, 0)  # type: ignore[operator]
    if size <= 0:
        return None
    buf = ctypes.create_string_buffer(size)
    got = _GETXATTR(encoded, name, buf, size, 0, 0)  # type: ignore[operator]
    if got <= 0:
        return None
    return buf.raw[:got]


class MacOSFinderTagProvider:
    """Finder tags from the ``_kMDItemUserTags`` xattr."""

    id = "os"

    def available_on(self, platform: str) -> bool:
        return platform == "darwin"

    def read(self, ctx: TagContext) -> frozenset[str]:
        blob = _read_xattr(ctx.path, _XATTR_USER_TAGS)
        if not blob:
            return frozenset()
        try:
            values = plistlib.loads(blob)
        except Exception:
            return frozenset()
        if not isinstance(values, list):
            return frozenset()
        out: set[str] = set()
        for item in values:
            if len(out) >= MAX_TAGS_PER_FILE:
                break
            if not isinstance(item, str):
                continue
            # Finder stores "Name\n<colour-index>"; the colour is presentation.
            tag = normalise_tag(item.split("\n", 1)[0])
            if tag:
                out |= expand_ancestors(tag)
        return frozenset(out)


TAG_PROVIDERS: dict[str, TagProvider] = {
    p.id: p for p in (FrontmatterTagProvider(), MacOSFinderTagProvider())
}


def providers_for(platform: str, enabled: Sequence[str]) -> list[TagProvider]:
    """Enabled providers this platform can actually serve.

    Unknown names are ignored rather than raising, so a config naming a source
    from a newer build degrades instead of breaking startup.
    """
    out: list[TagProvider] = []
    for name in enabled:
        provider = TAG_PROVIDERS.get(name)
        if provider is not None and provider.available_on(platform):
            out.append(provider)
    return out


def read_tags(ctx: TagContext, providers: Sequence[TagProvider]) -> dict[str, frozenset[str]]:
    """Tags per provider id.

    A failing provider contributes an empty set — tag reading must never fail
    an index run.
    """
    out: dict[str, frozenset[str]] = {}
    for provider in providers:
        try:
            out[provider.id] = provider.read(ctx)
        except Exception:
            out[provider.id] = frozenset()
    return out
