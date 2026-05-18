"""Probe — reproduce the user's report: searching 'Cyber kill chain' in
SFO Case Study 2 Talking Points.md, sections 4/6/9 show no highlight.

For each section under the file, click it and capture: where the pane
landed, whether the first_match_block is set on the focused chunk, and
whether the chunk widget actually contains highlight spans.

Run with:
    ./.venv/bin/python tests/perf/probe_cyber_kill_chain.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

DIAG_PATH = Path("/tmp/fnd-preview-diag.log")
if DIAG_PATH.exists():
    DIAG_PATH.unlink()
os.environ["FND_PREVIEW_DIAG"] = "1"

from fnd.config import Config, Defaults, RankingProfileConfig  # noqa: E402
from fnd.index import build_index  # noqa: E402
from fnd.tui import FNDApp  # noqa: E402

VAULT_ROOT = Path(
    "/Users/BenDavidson/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault"
)
TARGET_FILE = "SFO Case Study 2 Talking Points.md"
QUERY = "Cyber kill chain"


async def main() -> int:
    src = VAULT_ROOT / TARGET_FILE
    if not src.exists():
        raise SystemExit(f"file not found: {src}")
    with tempfile.TemporaryDirectory(prefix="fnd-ckc-") as tmp:
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
        app = FNDApp(index_dir=index_dir, config=cfg, collection="default", initial_query=QUERY)
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
            if not results:
                print("no results")
                return 1
            file_node = results[0]
            sections = list(file_node.children)
            print(f"file: {file_node.label}  n_sections={len(sections)}")

            from fnd.tui.app import FNDMarkdown

            for i, sec in enumerate(sections):
                data = sec.data
                if not isinstance(data, dict) or data.get("kind") != "section":
                    continue
                hit = data["hit"]
                tree.cursor_line = sec.line
                # Wait for finalize task.
                for _ in range(60):
                    await pilot.pause()
                    await asyncio.sleep(0.1)
                    ap = app._active_preview
                    ft = getattr(ap, "_finalize_task", None) if ap is not None else None
                    if ft is not None and ft.done():
                        break
                await pilot.pause()
                await asyncio.sleep(0.2)
                pane = app.query_one("#preview_pane")
                scroll_y = int(pane.scroll_offset.y)
                chunk_md = app._chunk_widgets.get(hit.chunk_seq)
                fmb_type = None
                fmb_y = None
                fmb_h = None
                if isinstance(chunk_md, FNDMarkdown):
                    inner = chunk_md.first_match_block
                    if inner is not None:
                        fmb_type = type(inner).__name__
                        try:
                            fmb_y = int(inner.region.y)
                            fmb_h = int(inner.region.height)
                        except Exception:
                            pass
                print(
                    f"  #{i:2d} seq={hit.chunk_seq:3d} title={hit.title[:35]!r}"
                    f"  scroll_y={scroll_y:4d}  fmb={fmb_type} y={fmb_y} h={fmb_h}"
                )

    if DIAG_PATH.exists():
        print("\n=== diag log ===")
        for line in DIAG_PATH.read_text().splitlines():
            if "do_scroll" in line or "finalize" in line:
                print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
