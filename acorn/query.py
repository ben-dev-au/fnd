"""Query layer: parse → search → group-by-parent → top-N sections per file.

Stub for Phase 1; real implementation lands in task #6. Phase 7 adds reranking,
phase 8 adds cascading multi-pass, phase 9 adds RRF fusion.
"""

from __future__ import annotations

from collections.abc import Iterator


def search_text(query: str, *, limit: int = 10) -> Iterator[str]:
    """Search the default collection and yield ranked ``file:page snippet`` lines."""
    raise NotImplementedError("acorn.query.search_text — implemented in task #6")
    # Hint to the type checker that this is a generator.
    yield ""  # pragma: no cover
