"""Probe — dump the rendered preview pane as text so we can see what
our wrapped DataTable looks like versus Textual's default MarkdownTable.

Renders the same fixture twice:
  * Run 1: default (W3 DataTable path, with our wrap fix).
  * Run 2: FND_NO_W3=1 forces MarkdownTableContent (grid layout with
    keyline). text-overflow: ellipsis is still in upstream CSS but
    short cells let us see the surrounding chrome (borders, headers).

Run with:
    ./.venv/bin/python tests/perf/probe_table_visual.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

from fnd.config import Config, Defaults, RankingProfileConfig  # noqa: E402
from fnd.index import build_index  # noqa: E402
from fnd.tui import FNDApp  # noqa: E402

# Use a small synthetic fixture so the rendered output is readable.
TABLE_MD = """\
# Comparison

| Phase | Goal | Score |
|-------|------|-------|
| Recon | Map the target surface | 10 |
| Exploit | Deliver payload | 15 |
| Persist | Stay resident | 20 |
"""


async def render_once(label: str, no_w3: bool) -> None:
    if no_w3:
        os.environ["FND_NO_W3"] = "1"
    else:
        os.environ.pop("FND_NO_W3", None)

    with tempfile.TemporaryDirectory(prefix=f"fnd-vis-{label}-") as tmp:
        root = Path(tmp)
        corpus = root / "corpus"
        corpus.mkdir(parents=True, exist_ok=True)
        (corpus / "table.md").write_text(TABLE_MD, encoding="utf-8")
        index_dir = root / "index"
        index_dir.mkdir(parents=True, exist_ok=True)
        build_index(roots=[corpus], index_dir=index_dir, collection="default")
        cfg = Config(
            defaults=Defaults(preview_prefetch_count=0, preview_load_debounce_ms=0),
            ranking={"default": RankingProfileConfig()},
        )
        app = FNDApp(
            index_dir=index_dir,
            config=cfg,
            collection="default",
            initial_query="phase",
        )
        from textual.widgets import Tree  # pyright: ignore[reportMissingImports]

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            tree = app.query_one("#results_pane", Tree)
            for _ in range(30):
                await pilot.pause()
                await asyncio.sleep(0.05)
                if len(tree.root.children) >= 1:
                    break
            results = list(tree.root.children)
            if not results:
                print(f"{label}: no results")
                return
            tree.cursor_line = results[0].line
            for _ in range(40):
                await pilot.pause()
                await asyncio.sleep(0.1)
                ap = app._active_preview
                ft = getattr(ap, "_finalize_task", None) if ap is not None else None
                if ft is not None and ft.done():
                    break
            await pilot.pause()
            await asyncio.sleep(0.2)
            await pilot.pause()
            from io import StringIO

            screen = app.screen
            compositor = screen._compositor
            strips = compositor.render_strips()
            buf = StringIO()
            for strip in strips:
                buf.write("".join(seg.text for seg in strip._segments))
                buf.write("\n")
            print(f"\n========== {label} ==========")
            print(buf.getvalue())


async def main() -> int:
    await render_once("W3 DataTable (current, with wrap fix)", no_w3=False)
    await render_once("MarkdownTableContent (default upstream, grid+keyline)", no_w3=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
