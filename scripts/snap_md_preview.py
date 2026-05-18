"""Snap the live TUI with a markdown file selected, to confirm the
preview pane is rendering via ``rich.markdown.Markdown``."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

from fnd.config import load
from fnd.index import build_index
from fnd.tui import FNDApp


async def main() -> None:
    work = Path("/tmp/__fnd_md_preview")
    work.mkdir(parents=True, exist_ok=True)
    cfg_path = work / "config.toml"
    cfg_path.write_text(
        '[collections.demo]\nroots = ["/tmp/__fnd_md_preview/notes"]\n',
        encoding="utf-8",
    )
    notes = work / "notes"
    notes.mkdir(exist_ok=True)
    (notes / "strategy.md").write_text(
        textwrap.dedent("""
            # Strategy Pattern

            The **Strategy pattern** lets an object pick an algorithm at runtime.

            ## When to use it

            - Behaviour varies independently of the host object.
            - You want to switch implementations without `if`/`switch`.

            ## Example

            ```cpp
            class SortStrategy {
            public:
                virtual ~SortStrategy() = default;
                virtual void sort(std::vector<int>& data) = 0;
            };
            ```

            ## Trade-offs

            | Strategy        | Pros                | Cons               |
            |-----------------|---------------------|--------------------|
            | Quick sort      | O(n log n) average  | O(n²) worst case   |
            | Merge sort      | Stable              | O(n) extra memory  |

            > Use this when the *host* shouldn't know which algorithm runs.

            ---

            See also: *Template Method*.
        """).strip()
        + "\n",
        encoding="utf-8",
    )
    idx = work / "index"
    if idx.exists():
        import shutil

        shutil.rmtree(idx)
    idx.mkdir()
    build_index(roots=[notes], index_dir=idx, collection="demo")
    cfg = load(cfg_path)
    app = FNDApp(index_dir=idx, config=cfg, collection="demo", initial_query="strategy")
    async with app.run_test(size=(170, 50)) as pilot:
        await pilot.pause()
        # Result tree auto-expands top file; cursor needs to land on the
        # section row for the preview to populate.
        from textual.widgets import Tree

        tree = app.query_one("#results_pane", Tree)
        tree.focus()
        await pilot.pause()
        await pilot.press("down")  # onto section row
        await pilot.pause()
        app.save_screenshot(filename="/tmp/fnd_md_preview.svg")


if __name__ == "__main__":
    asyncio.run(main())
