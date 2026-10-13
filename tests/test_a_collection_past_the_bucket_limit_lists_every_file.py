"""A collection holding more files than one tantivy terms aggregation may bucket
still lists every file, so a reindex can prune it instead of failing."""

from __future__ import annotations

import hashlib
from pathlib import Path

import tantivy

from fnd.analysis import register
from fnd.index import indexed_parent_ids
from fnd.schema import F_COLLECTION, F_PARENT_ID, build_schema

_FILES = 70_000


def test_every_file_of_a_collection_past_the_bucket_limit_is_listed(tmp_index_dir: Path) -> None:
    index = tantivy.Index(build_schema(), path=str(tmp_index_dir))
    register(index)
    writer = index.writer()
    ids = {hashlib.sha1(str(i).encode(), usedforsecurity=False).hexdigest() for i in range(_FILES)}
    for parent_id in ids:
        writer.add_document(tantivy.Document(**{F_PARENT_ID: parent_id, F_COLLECTION: "c"}))
    writer.add_document(tantivy.Document(**{F_PARENT_ID: "f" * 40, F_COLLECTION: "other"}))
    writer.commit()
    writer.wait_merging_threads()
    index.reload()

    assert indexed_parent_ids(index, "c") == ids
