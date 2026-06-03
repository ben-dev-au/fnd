"""Texture-cache maintenance: which content is still live.

The texture cache is content-addressed and shared across collections, so
an entry for a file that's been removed, renamed (to different bytes), or
de-configured can't be cleaned by a per-collection Rebuild. This module
computes the set of content hashes still reachable under the current
config; the cache's own ``count_orphans``/``prune_orphans`` then act on
everything else, so the cache reflects reality rather than an
accumulation of dead entries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fnd.config import Config


def live_content_shas(config: Config) -> set[str]:
    """Content hashes of every PDF currently reachable under any
    collection's sources. Hashes file bytes, so it's O(corpus) — only
    call from an explicit maintenance action, never a hot path."""
    import contextlib

    from fnd.cache import sha256_file
    from fnd.walk import walk_sources

    shas: set[str] = set()
    for col in config.collections.values():
        for path in walk_sources(sources=list(col.sources)):
            if path.suffix.lower() != ".pdf":
                continue
            with contextlib.suppress(OSError):
                shas.add(sha256_file(path))
    return shas
