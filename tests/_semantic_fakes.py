"""A deterministic embedder and a semantic context built straight from an index, for tests.

Vectors come from topic words: a text's vector is the normalised count of each topic word it
holds, so a test can say which section is closest to a query by reading the corpus.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np
import tantivy

from fnd.query import Hit, Searcher, _light_hit, decoded_body
from fnd.semantic import section_key, section_text
from fnd.semantic_search import SemanticContext
from fnd.vectors import RowTable, VectorStore

TOPICS = ("apple", "boat", "cloud", "drum", "eagle", "flute", "gourd")
# Words the fake reads as a topic that word matching cannot see.
SYNONYMS = {"piccolo": "flute"}
HEADER = {"model": "fake", "revision": "0", "max_tokens": 256, "recipe": "test"}


class FakeEmbedder:
    """Topic-count vectors; counts every text it embeds."""

    dim = len(TOPICS)

    def __init__(self) -> None:
        self.embedded = 0
        self.tokenised = 0

    def token_ids(self, text: str) -> list[int]:
        self.tokenised += 1
        return [int(hashlib.md5(w.encode()).hexdigest()[:6], 16) for w in text.lower().split()]

    def embed(self, texts: list[str]) -> np.ndarray:
        self.embedded += len(texts)
        time.sleep(float(os.environ.get("FND_FAKE_EMBED_SLOW_S", "0")))
        out = np.zeros((len(texts), self.dim), np.float32)
        for i, t in enumerate(texts):
            words = t.lower().split()
            seen = [SYNONYMS.get(w.strip(".,"), w.strip(".,")) for w in words]
            out[i] = [sum(w == topic for w in seen) for topic in TOPICS]
            # A small offset unique to the text, so no two texts ever tie.
            seed = int(hashlib.md5(t.encode()).hexdigest()[:8], 16)
            out[i] += 0.01 + np.random.default_rng(seed).random(self.dim) * 1e-3
        return out / np.linalg.norm(out, axis=1, keepdims=True)


def every_section(searcher: Searcher) -> list[Hit]:
    """Each indexed section once, with every collection it belongs to."""
    raw: Any = searcher._searcher
    total = raw.search(tantivy.Query.all_query(), limit=1, count=True).count
    seen: dict[tuple[str, int], Hit] = {}
    for _, addr in raw.search(tantivy.Query.all_query(), limit=max(total, 1)).hits:
        h = _light_hit(raw.doc(addr), 0.0)
        key = (h.parent_id, h.chunk_seq)
        prior = seen.get(key)
        if prior is None:
            seen[key] = h
        else:
            merged = tuple(dict.fromkeys([*prior.memberships, *h.memberships]))
            seen[key] = dataclasses.replace(prior, memberships=merged)
    return list(seen.values())


def build_context(
    searcher: Searcher,
    folder: Path,
    *,
    embedder: FakeEmbedder | None = None,
    opted_in: Iterable[str] = ("notes", "books"),
    only_files: set[str] | None = None,
) -> SemanticContext:
    """Embed every section (or only ``only_files`` by file name) into a fresh store."""
    embedder = embedder or FakeEmbedder()
    opted = tuple(sorted(set(opted_in)))
    store = VectorStore.open(folder, dim=embedder.dim, header=HEADER)
    sections = [
        h
        for h in every_section(searcher)
        if any(c in opted for c, _ in h.memberships)
        and (only_files is None or Path(h.path).name in only_files)
    ]
    texts = [section_text(h.title, h.heading_path, decoded_body(h)[0]) for h in sections]
    keys = [section_key(embedder.token_ids(t), 256) for t in texts]
    missing = [(k, t) for k, t in dict(zip(keys, texts, strict=True)).items() if not store.has(k)]
    if missing:
        store.append([k for k, _ in missing], embedder.embed([t for _, t in missing]))
    snap = store.snapshot()
    rows = RowTable(
        parent_id=np.array([h.parent_id for h in sections]),
        chunk_seq=np.array([h.chunk_seq for h in sections], dtype=np.int32),
        vec_row=np.array([snap.key_row[k] for k in keys], dtype=np.int32),
        collections=opted,
        member=np.array(
            [[c in {m for m, _ in h.memberships} for c in opted] for h in sections], dtype=bool
        ).reshape(len(sections), len(opted)),
        text_key=np.array(keys),
        stamp=searcher.generation,  # type: ignore[arg-type]
    )
    return SemanticContext(embedder=embedder, snapshot=snap, rows=rows, opted_in=frozenset(opted))
