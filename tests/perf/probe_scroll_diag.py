"""Probe — enable _FND_PREVIEW_DIAG and capture do_scroll log lines.

Runs intra-file navigation, then reads /tmp/fnd-preview-diag.log and
filters for the do_scroll lines. Used to understand WHY the scroll
lands at the wrong position.

Run with:
    ./.venv/bin/python tests/perf/probe_scroll_diag.py
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
os.environ["_FND_PREVIEW_DIAG"] = "1"

from fnd.config import Config, Defaults, RankingProfileConfig  # noqa: E402
from fnd.index import build_index  # noqa: E402
from fnd.tui import FNDApp  # noqa: E402

VAULT_ROOT = Path(
    "/Users/BenDavidson/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault"
)


def build_vault_subset(root: Path, *, n: int) -> Path:
    if not VAULT_ROOT.exists():
        raise SystemExit(f"vault root not found: {VAULT_ROOT}")
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    # Filter out Excalidraw (JSON-like; few real sections).
    md_files: list[tuple[int, Path]] = []
    for p in VAULT_ROOT.rglob("*.md"):
        if "Excalidraw" in str(p):
            continue
        try:
            md_files.append((p.stat().st_size, p))
        except OSError:
            continue
    md_files.sort(key=lambda t: t[0], reverse=True)
    for _size, src in md_files[:n]:
        shutil.copy2(src, corpus / src.name.replace("/", "_"))
    return corpus


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="fnd-diag-") as tmp:
        root = Path(tmp)
        corpus = build_vault_subset(root, n=2)
        index_dir = root / "index"
        index_dir.mkdir(parents=True, exist_ok=True)
        build_index(roots=[corpus], index_dir=index_dir, collection="default")
        cfg = Config(
            defaults=Defaults(preview_prefetch_count=0, preview_load_debounce_ms=0),
            ranking={"default": RankingProfileConfig()},
        )
        app = FNDApp(index_dir=index_dir, config=cfg, collection="default", initial_query="the")
        from textual.widgets import Tree  # pyright: ignore[reportMissingImports]
        from textual.widgets.tree import TreeNode  # pyright: ignore[reportMissingImports]

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
            print(f"first file: {file_node.label}  children={len(file_node.children)}")
            # Click first section to ensure file is loaded.
            sections = list(file_node.children)
            if not sections:
                print("first file has no sections — cannot test intra-file")
                return 1
            tree.cursor_line = sections[0].line
            await asyncio.sleep(3.0)
            # Capture which chunks we're going to hit.
            from fnd.tui.app import FNDMarkdown

            _ = TreeNode  # silence pyright on unused-import
            for i, sec in enumerate(sections[:8]):
                data = sec.data
                if not isinstance(data, dict) or data.get("kind") != "section":
                    continue
                hit = data["hit"]
                print(f"  section #{i}: chunk_seq={hit.chunk_seq} " f"title={hit.title[:40]!r}")
                tree.cursor_line = sec.line
                await asyncio.sleep(0.6)
                # Snapshot post-click state.
                pane = app.query_one("#preview_pane")
                chunk_md = app._chunk_widgets.get(hit.chunk_seq)
                fmb = None
                fmb_y = None
                if isinstance(chunk_md, FNDMarkdown):
                    inner = chunk_md.first_match_block
                    if inner is not None:
                        fmb = type(inner).__name__
                        try:
                            fmb_y = int(inner.region.y)
                        except Exception:
                            pass
                print(
                    f"      after click: scroll_y={int(pane.scroll_offset.y)} "
                    f"first_match_block={fmb} fmb_y={fmb_y}"
                )

    # Now dump the do_scroll diag lines.
    print("\n=== do_scroll diag lines ===")
    if DIAG_PATH.exists():
        for line in DIAG_PATH.read_text().splitlines():
            if "do_scroll" in line or "dispatch_preview cache_check" in line:
                print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
