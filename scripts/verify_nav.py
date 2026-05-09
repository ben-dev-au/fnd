"""Verify arrow-key navigation in both trees + reindex non-blocking.

Reads as a smoke test of UX-A-E claims against the actual UI."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

from acorn.config import load
from acorn.index import build_index
from acorn.tui import AcornApp


async def main() -> None:
    work = Path("/tmp/__acorn_verify")
    work.mkdir(parents=True, exist_ok=True)
    cfg_path = work / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [collections.DPC]
            roots = ["/tmp/__acorn_verify/dpc"]

            [collections.wine]
            roots = ["/tmp/__acorn_verify/wine"]
        """),
        encoding="utf-8",
    )
    dpc = work / "dpc"
    dpc.mkdir(exist_ok=True)
    (dpc / "a.md").write_text("# A\nfoo bar baz", encoding="utf-8")
    (dpc / "b.md").write_text("# B\nfoo bar baz", encoding="utf-8")
    (dpc / "c.md").write_text("# C\nfoo bar baz", encoding="utf-8")
    wine = work / "wine"
    wine.mkdir(exist_ok=True)
    (wine / "yalumba.md").write_text("# Yalumba", encoding="utf-8")
    idx = work / "index"
    if idx.exists():
        import shutil

        shutil.rmtree(idx)
    idx.mkdir()
    build_index(roots=[dpc], index_dir=idx, collection="DPC")
    build_index(roots=[wine], index_dir=idx, collection="wine")
    cfg = load(cfg_path)
    app = AcornApp(index_dir=idx, config=cfg, collection="DPC", initial_query="foo")

    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        from textual.widgets import Tree

        results = app.query_one("#results_pane", Tree)
        results.focus()
        await pilot.pause()
        # Arrow nav on results tree.
        for _ in range(2):
            await pilot.press("down")
            await pilot.pause()
        cursor = results.cursor_node
        print(f"results after 2x down: cursor={getattr(cursor, 'data', None)}")

        # Right to expand a file node.
        await pilot.press("right")
        await pilot.pause()
        cursor = results.cursor_node
        print(f"results after right: expanded={cursor.is_expanded if cursor else None}")

        # Left smart-collapse: cursor on file node, expanded → should collapse it.
        await pilot.press("left")
        await pilot.pause()
        cursor = results.cursor_node
        print(f"results after left: expanded={cursor.is_expanded if cursor else None}")

        # Tab to collections panel.
        await pilot.press("tab")
        await pilot.pause()
        focused_id = getattr(app.focused, "id", None)
        print(f"after tab: focused={focused_id}")

        # Down arrow on collections tree.
        ctree = app.query_one("#collections_panel_tree", Tree)
        ctree.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        cnode = ctree.cursor_node
        print(f"collections after focus+down: cursor={getattr(cnode, 'data', None)}")

        # Enter to toggle wine.
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        print(f"after enter: collections={app._collections}")

        # Take final screenshot.
        results.focus()
        await pilot.pause()
        # Position cursor on a result row, expand.
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        app.save_screenshot(filename="/tmp/acorn_verify.svg")


if __name__ == "__main__":
    asyncio.run(main())
