"""Probe — verify the wrong-position fix on a single warm-resume click.

Loads a real vault file, lets the initial mount settle, then clicks a
section that maps to a chunk OUTSIDE the initially-mounted window.
Waits for the finalize task to complete, then asserts the pane
scrolled to the new chunk (scroll_y changed).

Run with:
    ./.venv/bin/python tests/perf/probe_scroll_fix.py
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

DIAG_PATH = Path("/tmp/acorn-preview-diag.log")
if DIAG_PATH.exists():
    DIAG_PATH.unlink()
os.environ["ACORN_PREVIEW_DIAG"] = "1"

from acorn.config import Config, Defaults, RankingProfileConfig  # noqa: E402
from acorn.index import build_index  # noqa: E402
from acorn.tui import AcornApp  # noqa: E402

VAULT_ROOT = Path(
    "/Users/BenDavidson/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault"
)


def build_vault_subset(root: Path, *, n: int) -> Path:
    if not VAULT_ROOT.exists():
        raise SystemExit(f"vault root not found: {VAULT_ROOT}")
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
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
    with tempfile.TemporaryDirectory(prefix="acorn-fix-") as tmp:
        root = Path(tmp)
        corpus = build_vault_subset(root, n=2)
        index_dir = root / "index"
        index_dir.mkdir(parents=True, exist_ok=True)
        build_index(roots=[corpus], index_dir=index_dir, collection="default")
        cfg = Config(
            defaults=Defaults(preview_prefetch_count=0, preview_load_debounce_ms=0),
            ranking={"default": RankingProfileConfig()},
        )
        app = AcornApp(index_dir=index_dir, config=cfg, collection="default", initial_query="the")
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
            if len(sections) < 2:
                print(f"need ≥2 sections; got {len(sections)}")
                return 1

            # Click first section — initial cold mount. Wait for it to settle.
            sec0 = sections[0]
            sec0_data = sec0.data
            assert isinstance(sec0_data, dict)
            seq0 = sec0_data["hit"].chunk_seq
            tree.cursor_line = sec0.line
            print(f"cold click → chunk_seq={seq0}")
            for _ in range(60):
                await pilot.pause()
                await asyncio.sleep(0.1)
                ft = getattr(app._active_preview, "_finalize_task", None)
                if ft is not None and ft.done():
                    break
            pane = app.query_one("#preview_pane")
            scroll_after_cold = int(pane.scroll_offset.y)
            print(f"  scroll_after_cold = {scroll_after_cold}")
            mounted = set(app._active_preview.mounted_indices) if app._active_preview else set()
            print(f"  mounted_indices: {sorted(mounted)[:10]}...")

            # Pick a section whose chunk_seq is OUTSIDE the mounted window.
            target_sec = None
            target_seq = None
            for sec in sections[1:]:
                d = sec.data
                if not isinstance(d, dict):
                    continue
                seq = d["hit"].chunk_seq
                if seq not in mounted:
                    target_sec = sec
                    target_seq = seq
                    break
            if target_sec is None:
                print("no out-of-window section found in this file; skipping verification")
                return 0
            assert isinstance(target_seq, int)
            print(f"warm-resume click → chunk_seq={target_seq} (outside window)")
            tree.cursor_line = target_sec.line
            # Wait for finalize task to complete.
            for _ in range(80):  # up to ~8 seconds
                await pilot.pause()
                await asyncio.sleep(0.1)
                ft = getattr(app._active_preview, "_finalize_task", None)
                if ft is not None and ft.done():
                    break
            # Give the scroll one more refresh tick.
            await pilot.pause()
            await asyncio.sleep(0.3)
            await pilot.pause()
            scroll_after_warm = int(pane.scroll_offset.y)
            print(f"  scroll_after_warm = {scroll_after_warm}")
            now_mounted = set(app._active_preview.mounted_indices) if app._active_preview else set()
            print(f"  newly_mounted_indices: {sorted(now_mounted - mounted)[:10]}")

            if scroll_after_warm == scroll_after_cold:
                print("\n❌ FAIL: scroll didn't move after warm-resume click")
                return 2
            print(
                f"\n✅ scroll moved {scroll_after_cold} → {scroll_after_warm} "
                f"(target was chunk {target_seq})"
            )

    if DIAG_PATH.exists():
        print("\n=== diag log (do_scroll + finalize_via_lock lines) ===")
        for line in DIAG_PATH.read_text().splitlines():
            if "do_scroll" in line or "finalize_via_lock" in line:
                print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
