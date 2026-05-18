"""Probe — investigate whether wide table cells wrap or truncate
in the preview pane.

Loads 2025-04-15.md (which has a wide rubric table), opens the file
in the preview pane, and inspects the rendered DataTable widget. Logs:
 - Column widths the DataTable computed
 - Row heights (auto-height should produce >1 line for wrapped cells)
 - The actual rendered cell-line count via render_lines
 - A small ASCII snapshot of the pane region

Run with:
    ./.venv/bin/python tests/perf/probe_table_wrap.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

from fnd.config import Config, Defaults, RankingProfileConfig  # noqa: E402
from fnd.index import build_index  # noqa: E402
from fnd.tui import FNDApp  # noqa: E402

VAULT_ROOT = Path(
    "/Users/BenDavidson/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault"
)
TARGET_FILE = "2025-04-15.md"
QUERY = "diagram"


async def main() -> int:
    src = VAULT_ROOT / TARGET_FILE
    if not src.exists():
        raise SystemExit(f"file not found: {src}")
    with tempfile.TemporaryDirectory(prefix="fnd-table-") as tmp:
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
        from textual.widgets import DataTable, Tree  # pyright: ignore[reportMissingImports]
        from textual.widgets._markdown import MarkdownTable  # pyright: ignore[reportMissingImports]

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
            if not sections:
                print("no sections — selecting file row")
                tree.cursor_line = file_node.line
            else:
                tree.cursor_line = sections[0].line
            await pilot.pause()
            for _ in range(80):
                await pilot.pause()
                await asyncio.sleep(0.1)
                ap = app._active_preview
                ft = getattr(ap, "_finalize_task", None) if ap is not None else None
                if ft is not None and ft.done():
                    break
            await pilot.pause()

            pane = app.query_one("#preview_pane")
            pane_w = int(pane.region.width)
            pane_h = int(pane.region.height)
            print(f"pane region: {pane_w}x{pane_h}")

            tables = list(pane.query(MarkdownTable))
            dts = list(pane.query(DataTable))
            print(f"#MarkdownTable={len(tables)}  #DataTable={len(dts)}")

            for i, dt in enumerate(dts):
                col_widths = [int(c.content_width) for c in dt.columns.values()]
                col_widths_render = [int(c.get_render_width(dt)) for c in dt.columns.values()]
                row_heights = [int(r.height) for r in dt.rows.values()]
                print(
                    f"  DataTable #{i}: region={dt.region.width}x{dt.region.height}"
                    f"  cols={len(dt.columns)} rows={len(dt.rows)}"
                )
                print(f"    column content_width: {col_widths}")
                print(f"    column render_width:  {col_widths_render}")
                print(
                    f"    row heights (auto):   {row_heights[:6]}{'...' if len(row_heights) > 6 else ''}"
                )
                wide_row_idx = next((i for i, h in enumerate(row_heights) if h > 2), None)
                if wide_row_idx is None:
                    print("    NO row exceeds height=2 — content likely truncated")
                else:
                    print(
                        f"    row {wide_row_idx} height={row_heights[wide_row_idx]} ✓ wrapping active"
                    )

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
