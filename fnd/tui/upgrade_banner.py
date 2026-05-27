"""Count PDF Texture Cache entries left on a previous extractor version.

When the texturising engine improves (new version, docling enabled, a
config change), already-textured PDFs keep their value — they just don't
yet benefit from the improvement. Rather than interrupt the user with a
startup popup, fnd surfaces the count passively in Settings → Indexing &
PDF Texture, where they can choose to re-texturise the outdated docs.

This module is just that count; the Settings row and the re-texturise
action live in ``fnd.tui.menu``.
"""

from __future__ import annotations


def count_pre_upgrade_entries() -> tuple[int, str | None]:
    """Walk the on-disk PDF Texture Cache. Return ``(count, sample)``
    where ``count`` is the number of cache entries whose signature is
    different from the current ``texture_signature()`` and ``sample``
    is one of those legacy signatures (None when count==0)."""
    try:
        from fnd.cache import default_cache_dir
        from fnd.extract.pdf import texture_signature

        root = default_cache_dir()
        if not root.exists():
            return 0, None
        # Coarse texture signature, not the fine-grained extractor signature:
        # a minor app update (a flag tweak, a patch-level pymupdf4llm bump)
        # must NOT flag the whole corpus as outdated. Only a TEXTURE_VERSION
        # bump (a meaningful engine change) does.
        current = texture_signature()
        sample: str | None = None
        n = 0
        for shard in root.iterdir():
            if not shard.is_dir():
                continue
            for entry in shard.glob("*.json"):
                _, _, sig = entry.stem.partition("--")
                if sig and sig != current:
                    n += 1
                    if sample is None:
                        sample = sig
        return n, sample
    except Exception:
        return 0, None


__all__ = ["count_pre_upgrade_entries"]
