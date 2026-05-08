"""Format-specific text extractors.

Each extractor takes a path and yields :class:`Chunk` instances with structural metadata
(page / slide / heading_path) so the index can rank passages within a file, and the TUI
can deep-link.
"""

from acorn.extract.base import Chunk

__all__ = ["Chunk"]
