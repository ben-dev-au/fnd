"""Probe — for each section in the cyber-kill-chain result tree, dump
the actual chunk text so we can see whether 'Cyber Kill Chain' is in
the body or only matched via title/heading-boost."""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

from acorn.config import Config, Defaults, RankingProfileConfig  # noqa: E402
from acorn.index import build_index  # noqa: E402
from acorn.tui import AcornApp  # noqa: E402

VAULT_ROOT = Path(
    "/Users/BenDavidson/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault"
)
TARGET_FILE = "SFO Case Study 2 Talking Points.md"
QUERY = "Cyber kill chain"


async def main() -> int:
    src = VAULT_ROOT / TARGET_FILE
    with tempfile.TemporaryDirectory(prefix="acorn-ckc2-") as tmp:
        root = Path(tmp)
        corpus = root / "corpus"
        corpus.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, corpus / src.name)
        index_dir = root / "index"
        index_dir.mkdir(parents=True, exist_ok=True)
        build_index(roots=[corpus], index_dir=index_dir, collection="default")
        cfg = Config(
            defaults=Defaults(preview_prefetch_count=0, preview_load_debounce_ms=0),
            ranking={"default": RankingProfileConfig()},
        )
        app = AcornApp(index_dir=index_dir, config=cfg, collection="default", initial_query=QUERY)
        from textual.widgets import Tree  # pyright: ignore[reportMissingImports]

        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            tree = app.query_one("#results_pane", Tree)
            for _ in range(60):
                await pilot.pause()
                await asyncio.sleep(0.05)
                if len(tree.root.children) >= 1:
                    break
            results = list(tree.root.children)
            file_node = results[0]
            sections = list(file_node.children)
            file_data = file_node.data
            assert isinstance(file_data, dict)
            grp = file_data["group"]
            parent_id = grp.parent_id

            # Load chunks via the same path the preview uses.
            searcher = app._searcher
            assert searcher is not None
            chunks = searcher.get_file_chunks(parent_id)
            print(f"#chunks_total={len(chunks)}")
            print(f"#sections_returned={len(sections)}")

            # Tree section_seqs
            for i, sec in enumerate(sections):
                d = sec.data
                if not isinstance(d, dict) or d.get("kind") != "section":
                    continue
                hit = d["hit"]
                seq = hit.chunk_seq
                title = hit.title if hasattr(hit, "title") else None
                ck_match = next((c for c in chunks if c.chunk_seq == seq), None)
                if ck_match is None:
                    print(f"  #{i} seq={seq}: CHUNK NOT FOUND")
                    continue
                body_md = ck_match.body_md or ""
                blocks_text = "\n".join(b.text for b in ck_match.blocks)
                body = body_md or blocks_text
                has_text = "cyber" in body.lower() and "kill" in body.lower()
                bodylen = len(body)
                first_line = body.split("\n", 1)[0] if body else ""
                print(
                    f"  #{i} seq={seq} body_has_term={has_text} bodylen={bodylen}"
                    f"  hit.title={title!r}"
                )
                print(f"     heading_path={ck_match.heading_path!r}")
                print(f"     first_line={first_line[:80]!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
