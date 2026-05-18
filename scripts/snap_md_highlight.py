"""Snapshot the markdown preview with a matched chunk + minimap visible.

Run via: ``uv run python scripts/snap_md_highlight.py [output.svg]``

The corpus has multi-chunk markdown files so the chunk-match minimap
strip on the right of the preview has something to paint.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

from textual.widgets import Tree

from fnd.config import load
from fnd.index import build_index
from fnd.tui import FNDApp


async def _snap(out_path: Path) -> None:
    work = Path("/tmp/__fnd_snap_md")
    work.mkdir(parents=True, exist_ok=True)
    cfg_path = work / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.DPC.sources]]
            path = "/tmp/__fnd_snap_md/dpc"
        """),
        encoding="utf-8",
    )
    dpc = work / "dpc"
    dpc.mkdir(exist_ok=True)
    body_lines: list[str] = ["# Templates", ""]
    body_lines.append("## Strategy pattern")
    body_lines.append("The strategy pattern lets templates vary independently of the algorithm.")
    body_lines.append("")
    body_lines.append("## Iterators")
    body_lines.append("Iterators decouple traversal from the templates collection.")
    body_lines.append("")
    body_lines.append("## Notes without matches")
    body_lines.append("This block has nothing relevant to mention.")
    body_lines.append("")
    for i in range(20):
        body_lines.append(f"Line {i}: filler text to force multiple chunks.")
    body_lines.append("## Closing thoughts on templates")
    body_lines.append("Templates support compile-time polymorphism cleanly.")
    (dpc / "Templates - Notes.md").write_text("\n".join(body_lines), encoding="utf-8")

    idx = work / "index"
    if idx.exists():
        import shutil

        shutil.rmtree(idx)
    idx.mkdir()
    build_index(roots=[dpc], index_dir=idx, collection="DPC")
    cfg = load(cfg_path)

    app = FNDApp(index_dir=idx, config=cfg, collection="DPC", initial_query="templates")
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        await pilot.pause()
        # Drive a real cursor move via the keyboard so NodeHighlighted
        # fires and the preview pane mounts the file's chunks.
        tree = app.query_one("#results_pane", Tree)
        tree.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        await pilot.pause()
        app.save_screenshot(filename=str(out_path))


def main(argv: list[str]) -> int:
    out = Path(argv[1] if len(argv) > 1 else "/tmp/fnd_md_highlight.svg")
    asyncio.run(_snap(out))
    print(f"saved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
