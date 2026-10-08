"""Format-specific text extractors.

Each extractor takes a path and yields :class:`Chunk` instances with structural
metadata (page / slide / heading_path) so the index can rank passages within a
file, and the TUI can deep-link.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fnd.extract.base import Chunk, ExtractError, no_text_reason
from fnd.kinds import SUFFIX_TO_MODULE, supported_suffixes

__all__ = ["Chunk", "ExtractError", "extract", "no_text_reason", "supported_suffixes"]

# Suffix → extractor module, derived from the central registry (fnd.kinds).
# Modules are lazily imported below to keep startup time small (pymupdf is the
# heaviest dep). ``supported_suffixes`` is re-exported from the registry.
_DISPATCH: dict[str, str] = SUFFIX_TO_MODULE


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
    if not str(path).isascii() and not _encodes(str(path)):
        return _refused(path)
    import importlib
    import inspect

    mod = importlib.import_module(f"fnd.extract.{mod_name}")
    extractor = mod.extract  # type: ignore[attr-defined]
    if kwargs:
        accepted = set(inspect.signature(extractor).parameters)
        kwargs = {k: v for k, v in kwargs.items() if k in accepted}
    from fnd.extract._bound import bounded

    # Every kind, one guarantee: see fnd/extract/_bound.py.
    return bounded(extractor(path, **kwargs))


def _encodes(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _refused(path: Path) -> Iterator[Chunk]:
    """The index stores a path as text, and a name that is not UTF-8 has none."""
    raise ExtractError(str(path), "the file name is not valid UTF-8; rename it to index it")
    yield  # pragma: no cover
