"""``PrefetchEngine._mount_chunk_loop`` caching rules, against a stub app."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from fnd.tui.preview.prefetch import PrefetchEngine
from fnd.tui.widgets.preview_container import PreviewCache, PreviewContainer


class _StubApp:
    def __init__(self, signature: str) -> None:
        self.signature = signature
        self.removed: list[PreviewContainer] = []
        self._search = SimpleNamespace(query_signature=lambda: self.signature)
        self._preview = SimpleNamespace(
            preview_cache=PreviewCache(max_files=1),
            active=None,
            user_mount_in_flight=lambda: False,
            mount_chunk_into=self._mount_chunk_into,
        )

    def _diag_log(self, _line: str) -> None:
        pass

    def _mount_chunk_into(
        self, container: PreviewContainer, _chunk: Any, i: int, _all: Any
    ) -> None:
        container.mounted_indices.add(i)
        self.signature = "new-sig"

    def container(self, doc: str, signature: str) -> PreviewContainer:
        c = PreviewContainer(parent_doc_id=doc, query_signature=signature, total_chunks=5)
        c.remove = lambda: self.removed.append(c)  # type: ignore[method-assign]
        return c


@pytest.mark.asyncio
async def test_a_loop_that_outlives_its_query_removes_rather_than_caches() -> None:
    """A stale-signature container is removed and never evicts the fresh one."""
    app = _StubApp("old-sig")
    fresh = app.container("fresh", "new-sig")
    app._preview.preview_cache.put(fresh)
    stale = app.container("stale", "old-sig")
    chunks = [SimpleNamespace(chunk_seq=i) for i in range(5)]

    await PrefetchEngine(app)._mount_chunk_loop("stale", "old-sig", 2, chunks, stale)  # type: ignore[arg-type]

    assert stale.mounted_indices, "setup: a chunk must land before the query changes"
    assert app._preview.preview_cache.get("stale", "old-sig") is None
    assert app._preview.preview_cache.get("fresh", "new-sig") is fresh
    assert app.removed == [stale]
