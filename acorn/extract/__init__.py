"""Format-specific text extractors.

Each extractor takes a path and yields :class:`Chunk` instances with structural
metadata (page / slide / heading_path) so the index can rank passages within a
file, and the TUI can deep-link.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from acorn.extract.base import Chunk

__all__ = ["Chunk", "extract", "supported_suffixes"]

# Map suffix → extractor module attribute name. Lazily imported to keep
# startup time small (pymupdf is the heaviest dep).
_DISPATCH: dict[str, str] = {
    ".txt": "plain",
    ".md": "markdown",
    ".markdown": "markdown",
    ".pdf": "pdf",
    ".pptx": "pptx",
    ".docx": "docx",
}


def supported_suffixes() -> frozenset[str]:
    return frozenset(_DISPATCH)


def extract(path: Path) -> Iterator[Chunk]:
    suffix = path.suffix.lower()
    mod_name = _DISPATCH.get(suffix)
    if mod_name is None:
        return iter(())
    import importlib

    mod = importlib.import_module(f"acorn.extract.{mod_name}")
    return mod.extract(path)  # type: ignore[no-any-return]
