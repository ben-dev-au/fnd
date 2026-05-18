"""Probe — break down per-field BM25 contributions for the SFO/Cyber
Kill Chain query. Runs the same query against each searchable field
individually (no boost), then against the combined boosted search,
so we can see exactly where the 49.21 score comes from for chunks
whose body has no match.

Run with:
    ./.venv/bin/python tests/perf/probe_score_breakdown.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

from fnd.index import build_index  # noqa: E402
from fnd.schema import (  # noqa: E402
    F_BODY,
    F_CHUNK_SEQ,
    F_HEADING_PATH,
    F_PATH_TOKENS,
    F_TITLE,
)

VAULT_ROOT = Path(
    "/Users/BenDavidson/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault"
)
TARGET_FILE = "SFO Case Study 2 Talking Points.md"
QUERY = "cyber kill chain"


def main() -> int:
    import tantivy  # pyright: ignore[reportMissingImports]

    src = VAULT_ROOT / TARGET_FILE
    with tempfile.TemporaryDirectory(prefix="fnd-scoreb-") as tmp:
        root = Path(tmp)
        corpus = root / "corpus"
        corpus.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, corpus / src.name)
        index_dir = root / "index"
        index_dir.mkdir(parents=True, exist_ok=True)
        build_index(roots=[corpus], index_dir=index_dir, collection="default")

        index = tantivy.Index.open(str(index_dir))
        searcher = index.searcher()

        fields_to_test = [
            (F_BODY, 1.0),
            (F_HEADING_PATH, 1.0),
            (F_TITLE, 1.0),
            (F_PATH_TOKENS, 1.0),
        ]
        print(f"query: {QUERY!r}")
        print("\n--- combined (current default with boosts) ---")
        # Mirror how the app builds the multi-field query.
        combined = index.parse_query(
            QUERY,
            default_field_names=[F_BODY, F_TITLE, F_HEADING_PATH, F_PATH_TOKENS],
            field_boosts={F_HEADING_PATH: 3.0, F_TITLE: 2.5, F_PATH_TOKENS: 1.5, F_BODY: 1.0},
        )
        results = searcher.search(combined, limit=30)
        for score, addr in results.hits:
            doc = searcher.doc(addr)
            seq = doc.get_first(F_CHUNK_SEQ)
            heading = doc.get_first(F_HEADING_PATH) or ""
            print(f"  score={score:6.2f}  seq={seq:3d}  {heading[-80:]}")

        # Per-field unboosted contribution
        for fname, _boost in fields_to_test:
            print(f"\n--- {fname} alone (boost=1.0) ---")
            try:
                q = index.parse_query(QUERY, default_field_names=[fname])
            except Exception as e:
                print(f"  parse failed: {e}")
                continue
            results = searcher.search(q, limit=30)
            if not results.hits:
                print("  (no hits)")
                continue
            for score, addr in results.hits:
                doc = searcher.doc(addr)
                seq = doc.get_first(F_CHUNK_SEQ)
                heading = doc.get_first(F_HEADING_PATH) or ""
                print(f"  score={score:6.2f}  seq={seq:3d}  {heading[-80:]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
