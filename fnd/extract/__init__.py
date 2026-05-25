"""Format-specific text extractors.

Each extractor takes a path and yields :class:`Chunk` instances with structural
metadata (page / slide / heading_path) so the index can rank passages within a
file, and the TUI can deep-link.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fnd.extract.base import Chunk, ExtractError

__all__ = ["Chunk", "ExtractError", "extract", "supported_suffixes"]

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


def extract(path: Path, **kwargs: object) -> Iterator[Chunk]:
    """Dispatch extraction to the right per-suffix module. Extra
    keyword arguments are forwarded only to extractors that accept
    them (currently the PDF extractor's on_heartbeat); the other
    extractors silently ignore unknown kwargs so callers can pass
    on_heartbeat for every file without branching on suffix."""
    suffix = path.suffix.lower()
    mod_name = _DISPATCH.get(suffix)
    if mod_name is None:
        return iter(())
    import importlib
    import inspect

    mod = importlib.import_module(f"fnd.extract.{mod_name}")
    extractor = mod.extract  # type: ignore[attr-defined]
    if kwargs:
        accepted = set(inspect.signature(extractor).parameters)
        kwargs = {k: v for k, v in kwargs.items() if k in accepted}
    return extractor(path, **kwargs)  # type: ignore[no-any-return]
