"""Regression: LRU eviction must never remove the currently-active preview.

Root cause of the "preview blank until I select another result and come back"
strand (data-reproduced under overshoot-and-return navigation at ~0.25s cadence):

With ``PREVIEW_CACHE_MAX_FILES == 1`` the cache holds one container. A mount that
a rapid nav CANCELS runs its ``finally`` LATE — after a newer navigation has
already re-activated a *different* container. That late ``finally`` called
``put(its_container, protect=its_container)``, which evicted the now-active
container out of the DOM while ``self.active`` still pointed at it: a detached,
never-self-healing blank pane.

The fix makes ``put`` protect BOTH the just-inserted container and an explicit
``protect`` argument (the caller passes the active container). These tests pin
that invariant at the cache layer, independent of the async timing that surfaces
it.
"""

from __future__ import annotations

from fnd.tui.widgets.preview_container import PreviewCache, PreviewContainer


def _container(doc: str) -> PreviewContainer:
    # total_chunks >= PREVIEW_CACHE_MIN_CHUNKS (1) so the container is cacheable;
    # a bare PreviewContainer needs no app/mount for the cache's bookkeeping.
    return PreviewContainer(parent_doc_id=doc, query_signature="sig", total_chunks=3)


def test_put_never_evicts_the_active_container() -> None:
    cache = PreviewCache(max_files=1)
    active = _container("A")
    cache.put(active)  # cache = {A}, A is the active preview

    # A stale mount's late finally caches its own container B while A is still
    # active. Passing protect=active must keep A: the invariant is that the
    # ACTIVE container is never the one evicted. With max=1 the stale/incoming
    # container B is evicted instead, so the active preview stays in the DOM.
    other = _container("B")
    evicted = cache.put(other, protect=active)

    assert evicted == [other], "the stale/incoming container is evicted, not the active one"
    assert cache.get("A", "sig") is active, "active container must survive the put"


def test_put_never_evicts_the_just_inserted_container() -> None:
    cache = PreviewCache(max_files=1)
    first = _container("A")
    cache.put(first)
    # Insert a second with no explicit protection: the container we just inserted
    # is implicitly protected, so it is the OTHER entry that goes.
    second = _container("B")
    evicted = cache.put(second)
    assert second in cache._cache.values(), "the just-inserted container must remain"
    assert evicted == [first]


def test_eviction_still_happens_for_unprotected_entries() -> None:
    # The protection is targeted, not a blanket "never evict": a normal put with
    # the incoming container == active still bounds the cache to max_files.
    cache = PreviewCache(max_files=1)
    a = _container("A")
    cache.put(a, protect=a)
    b = _container("B")
    evicted = cache.put(b, protect=b)  # b is the new active; a is stale
    assert evicted == [a], "an unprotected stale entry is still evicted"
    assert list(cache._cache.values()) == [b]
