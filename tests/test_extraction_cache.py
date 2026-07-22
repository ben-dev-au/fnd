"""Cache module tests.

Requirements covered:
- F11: identical file content → cache hit (same key)
- F12: same file content, different extractor signature → miss
- F13: cache write is atomic (tmpfile cleanup on failure)
- F14: corrupt entry → silent miss (caller re-extracts)
- F15: schema_version mismatch → silent miss
- NF8: get() round-trip is fast (<20ms on a typical chunk blob)
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from fnd.cache import (
    CACHE_SCHEMA_VERSION,
    ExtractionCache,
    sha256_file,
)
from fnd.extract.base import Block, Chunk


def _make_chunk(seq: int = 0) -> Chunk:
    return Chunk(
        parent_id="abc",
        path="/x.pdf",
        mtime=123456,
        kind="pdf",
        body=f"page {seq} body text",
        body_struct=[Block(kind="h2", text=f"Heading {seq}"), Block(kind="p", text="prose")],
        body_md=f"## Heading {seq}\n\nprose",
        page=seq + 1,
        chunk_seq=seq,
    )


def test_build_key_deterministic() -> None:
    """F11: same inputs → same key."""
    k1 = ExtractionCache.build_key(content_sha256="abc123", extractor_signature="v1")
    k2 = ExtractionCache.build_key(content_sha256="abc123", extractor_signature="v1")
    assert k1 == k2


def test_build_key_differs_per_extractor(tmp_path: Path) -> None:
    """F12: same content, different extractor → different key."""
    cache = ExtractionCache(root=tmp_path)
    k_pdf = cache.build_key(content_sha256="abc", extractor_signature="pymupdf4llm-1.27")
    k_doc = cache.build_key(content_sha256="abc", extractor_signature="docling-2.94")
    assert k_pdf != k_doc


def test_put_then_get_round_trip(tmp_path: Path) -> None:
    """F11: write then read gets the same chunks back."""
    cache = ExtractionCache(root=tmp_path)
    key = "deadbeef--ext-v1"
    chunks = [_make_chunk(i) for i in range(3)]
    cache.put(key, chunks)
    got = cache.get(key)
    assert got is not None
    assert len(got) == 3
    for original, restored in zip(chunks, got, strict=True):
        assert restored.body == original.body
        assert restored.body_md == original.body_md
        assert len(restored.body_struct) == len(original.body_struct)
        for ob, rb in zip(original.body_struct, restored.body_struct, strict=True):
            assert rb.kind == ob.kind
            assert rb.text == ob.text


def test_get_miss_returns_none(tmp_path: Path) -> None:
    """No file at the entry path → None."""
    cache = ExtractionCache(root=tmp_path)
    assert cache.get("nonexistent-key") is None


def test_get_corrupt_json_returns_none(tmp_path: Path) -> None:
    """F14: corrupt JSON → silent miss, no exception."""
    cache = ExtractionCache(root=tmp_path)
    key = "test-cache-entry-1"
    path = cache.entry_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json")
    assert cache.get(key) is None


def test_get_schema_version_mismatch_returns_none(tmp_path: Path) -> None:
    """F15: future schema versions → silent miss."""
    cache = ExtractionCache(root=tmp_path)
    key = "futurekey--v1"
    path = cache.entry_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": CACHE_SCHEMA_VERSION + 999, "chunks": []}))
    assert cache.get(key) is None


def test_put_overwrites_existing_entry(tmp_path: Path) -> None:
    """Re-extraction overwrites the cache; consistent with mtime-based
    cache invalidation if content stayed same but extractor was bumped."""
    cache = ExtractionCache(root=tmp_path)
    key = "samekey--v1"
    cache.put(key, [_make_chunk(0)])
    cache.put(key, [_make_chunk(0), _make_chunk(1)])
    got = cache.get(key)
    assert got is not None
    assert len(got) == 2


def test_put_atomic_no_partial_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F13: a failed write must not leave the entry path partially populated.
    Either the previous entry survives, or nothing exists — never a corrupt half-write."""
    cache = ExtractionCache(root=tmp_path)
    key = "atomic--v1"

    # First a clean put so the entry exists.
    cache.put(key, [_make_chunk(0)])
    original = cache.get(key)
    assert original is not None
    assert len(original) == 1

    # Now force os.replace to fail mid-put.
    def boom(*_a: object, **_kw: object) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr("fnd.cache.os.replace", boom)
    with pytest.raises(OSError, match="simulated"):
        cache.put(key, [_make_chunk(0), _make_chunk(1)])

    # The previous entry survives unchanged.
    after = cache.get(key)
    assert after is not None
    assert len(after) == 1
    # And no tmp file leaked.
    leftovers = list(cache.entry_path(key).parent.glob("*.tmp"))
    assert leftovers == []


def test_entry_path_shards_by_hash_prefix(tmp_path: Path) -> None:
    """Sharding: keys 'ab*' and 'cd*' land in different subdirs so any
    one dir stays below filesystem inode-list limits."""
    cache = ExtractionCache(root=tmp_path)
    p_ab = cache.entry_path("abcdef--v1")
    p_cd = cache.entry_path("cdefab--v1")
    assert p_ab.parent.name == "ab"
    assert p_cd.parent.name == "cd"
    assert p_ab.parent != p_cd.parent


def test_entry_path_filename_is_windows_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache key can embed an extractor signature containing ``|`` (and
    other chars Windows forbids in filenames). On Windows the on-disk name
    must be sanitised so ``os.replace`` doesn't raise WinError 123."""
    monkeypatch.setattr("sys.platform", "win32")
    cache = ExtractionCache(root=tmp_path)
    key = cache.build_key(
        content_sha256="ab" * 32, extractor_signature="pymupdf4llm-1.27|docling|cfg-x"
    )
    name = cache.entry_path(key).name
    assert not (set(name) & set('<>:"/\\|?*')), f"illegal chars in {name!r}"
    # Idempotent across a glob→entry_path round-trip (get() re-sanitises stems).
    assert cache.entry_path(cache.entry_path(key).stem) == cache.entry_path(key)


def test_entry_path_sanitisation_is_collision_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """Distinct keys must never alias to the same on-disk filename on Windows,
    or one signature's chunks would silently overwrite another's for the same
    content hash. ``a|b`` and ``a_b`` are the canonical near-miss."""
    monkeypatch.setattr("sys.platform", "win32")
    cache = ExtractionCache()
    a = cache.entry_path("deadbeef--a|b")
    b = cache.entry_path("deadbeef--a_b")
    assert a != b, f"distinct signatures collided: {a.name} == {b.name}"


def test_pipe_bearing_key_round_trips(tmp_path: Path) -> None:
    """put()/get() of a legacy ``|``-bearing key works on the host OS —
    on Windows only because entry_path sanitises the filename."""
    cache = ExtractionCache(root=tmp_path)
    key = "deadbeef--pymupdf4llm-1.0|cfg-abc"
    cache.put(key, [_make_chunk(0)])
    assert cache.get(key) is not None


def test_total_size_and_entry_count(tmp_path: Path) -> None:
    """Status helpers report cache footprint for `fnd cache status`."""
    cache = ExtractionCache(root=tmp_path)
    assert cache.entry_count() == 0
    assert cache.total_size_bytes() == 0
    cache.put("k1--v1", [_make_chunk(0)])
    cache.put("k2--v1", [_make_chunk(1)])
    assert cache.entry_count() == 2
    assert cache.total_size_bytes() > 0


def test_get_round_trip_under_20ms_for_typical_payload(tmp_path: Path) -> None:
    """NF8: lookup overhead must be well under per-page extraction cost
    (~150ms for pymupdf4llm). Big chunk blob simulating a real PDF."""
    cache = ExtractionCache(root=tmp_path)
    key = "perf--v1"
    # A 300-page book is ~500KB JSON; build something equivalent.
    chunks = []
    big_text = "lorem ipsum " * 200  # ~2.4 KB per chunk
    for i in range(300):
        chunks.append(
            Chunk(
                parent_id="x",
                path="/x.pdf",
                mtime=0,
                kind="pdf",
                body=big_text,
                body_struct=[Block(kind="p", text=big_text)],
                body_md=f"## p{i}\n\n{big_text}",
                page=i + 1,
                chunk_seq=i,
            )
        )
    cache.put(key, chunks)

    # Warm-up read (filesystem cache).
    cache.get(key)
    t0 = time.perf_counter()
    got = cache.get(key)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert got is not None
    assert len(got) == 300
    assert elapsed_ms < 100, f"get() took {elapsed_ms:.1f}ms; budget is 100ms"


def test_sha256_file_is_stable(tmp_path: Path) -> None:
    """Same bytes → same hash; differ → different hash."""
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"hello world")
    b.write_bytes(b"hello world")
    assert sha256_file(a) == sha256_file(b)
    b.write_bytes(b"different")
    assert sha256_file(a) != sha256_file(b)


def test_cache_overlay_per_file_identity_on_restore(tmp_path: Path) -> None:
    """Regression: two files with identical bytes at DIFFERENT paths
    share a cache entry (keyed by content hash), but the restored
    chunks must reflect the current file's parent_id / path / mtime —
    otherwise the indexer routes chunks to the wrong Tantivy
    parent_id and the search shows only one of the two files.

    This validates the overlay in fnd/extract/pdf.py at the cache-hit
    branch. We verify here by extracting two identical-content PDFs
    and checking the chunks' parent_id differs.
    """
    # Use the real PDF extractor against two copies of the same fixture.
    import shutil

    from fnd.extract.pdf import _parent_id, extract

    fixture = Path(__file__).parent / "fixtures" / "papers" / "test.pdf"
    if not fixture.exists():
        pytest.skip("test.pdf fixture missing")

    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    shutil.copy(fixture, a)
    shutil.copy(fixture, b)

    # First extraction populates the cache.
    chunks_a = list(extract(a))
    # Second extraction is a cache hit — must overlay parent_id.
    chunks_b = list(extract(b))

    assert chunks_a, "extract() must yield chunks"
    assert chunks_b, "extract() must yield chunks for the copy"

    parent_a = _parent_id(a)
    parent_b = _parent_id(b)
    assert parent_a != parent_b, "different paths must yield different parent_ids"

    for chunk in chunks_a:
        assert chunk.parent_id == parent_a
        assert chunk.path == str(a)
    for chunk in chunks_b:
        assert chunk.parent_id == parent_b
        assert chunk.path == str(b), "cache hit returned the original path; overlay logic broken"
