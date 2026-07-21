"""Extractors stamp created/inode-change time alongside mtime."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from fnd.extract.base import Chunk
from fnd.extract.markdown import extract as extract_md
from fnd.extract.plain import extract as extract_txt


@pytest.mark.skipif(sys.platform != "darwin", reason="birthtime is Darwin-only")
@pytest.mark.parametrize(
    ("suffix", "extractor"),
    [(".md", extract_md), (".txt", extract_txt)],
)
def test_extractors_stamp_all_times(
    tmp_path: Path, suffix: str, extractor: Callable[[Path], Iterator[Chunk]]
) -> None:
    f = tmp_path / f"doc{suffix}"
    f.write_text("# Title\n\nSome body text.\n", encoding="utf-8")
    chunks = list(extractor(f))
    assert chunks, "extractor produced no chunks"
    for c in chunks:
        assert c.mtime > 0
        assert c.created > 0
        assert c.inode_changed > 0


def test_cached_chunk_without_new_keys_defaults_to_zero() -> None:
    """Durable cache entries predate these fields; loading must not KeyError."""
    from fnd.cache import _chunk_from_dict

    legacy = {
        "parent_id": "p",
        "path": "/tmp/x.pdf",
        "mtime": 123,
        "kind": "pdf",
        "body": "text",
    }
    chunk = _chunk_from_dict(legacy)
    assert chunk.mtime == 123
    assert chunk.created == 0
    assert chunk.inode_changed == 0
