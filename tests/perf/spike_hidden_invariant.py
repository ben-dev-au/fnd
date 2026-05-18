"""Probe — runtime check that cached PreviewContainers are display:none.

Hypothesis H2 from the diagnostic prompt: if cached containers stay
display:block, every cached subtree gets walked by the compositor on
every tick. Textual's _arrange.py:61 filters display:none widgets out
of layout, so display:none should mean ~zero per-tick cost.

This probe loads the bench's HEAVY synthetic corpus, clicks 3 files,
then snapshots the live `styles.display` of every PreviewContainer.

Run with:
    ./.venv/bin/python tests/perf/spike_hidden_invariant.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

from acorn.config import Config, Defaults, RankingProfileConfig  # noqa: E402
from acorn.index import build_index  # noqa: E402
from acorn.tui import AcornApp  # noqa: E402
from tests.perf import _corpus  # noqa: E402

MATCH_TOKEN = _corpus.MATCH_TOKEN


def build_corpus(root: Path, *, n: int = 6) -> Path:
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    specs = [_corpus.HEAVY, _corpus.TABLE_HEAVY, _corpus.FENCE_HEAVY]
    for i in range(n):
        spec = specs[i % len(specs)]
        (corpus / f"file_{i:02d}_{spec.profile}.md").write_text(
            _corpus.render(spec), encoding="utf-8"
        )
    return corpus


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="acorn-hidden-") as tmp:
        root = Path(tmp)
        corpus = build_corpus(root, n=6)
        index_dir = root / "index"
        index_dir.mkdir(parents=True, exist_ok=True)
        build_index(roots=[corpus], index_dir=index_dir, collection="default")
        cfg = Config(
            defaults=Defaults(preview_prefetch_count=0, preview_load_debounce_ms=0),
            ranking={"default": RankingProfileConfig()},
        )
        app = AcornApp(
            index_dir=index_dir,
            config=cfg,
            collection="default",
            initial_query=MATCH_TOKEN,
        )
        from textual.widgets import Tree

        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            tree = app.query_one("#results_pane", Tree)
            for _ in range(30):
                await pilot.pause()
                await asyncio.sleep(0.05)
                if len(tree.root.children) >= 3:
                    break
            results = list(tree.root.children)[:3]
            for n in results:
                tree.cursor_line = n.line
                await asyncio.sleep(1.0)
                await pilot.pause()

            from acorn.tui.app import PreviewContainer

            containers = list(app.query(PreviewContainer))
            print(f"\ncached PreviewContainers: {len(containers)}")
            print(
                f"active_preview parent_id: "
                f"{getattr(app._active_preview, 'parent_doc_id', None)}"
            )
            for c in containers:
                is_active = c is app._active_preview
                hidden_class = "-hidden" in c.classes
                pre_reveal = "-pre-reveal" in c.classes
                display = str(c.styles.display)
                visibility = str(c.styles.visibility)
                walks = "WALKS" if c.display else "filtered"
                print(
                    f"  {c.parent_doc_id[:8]} active={is_active!s:<5} "
                    f"hidden_cls={hidden_class!s:<5} pre_reveal={pre_reveal!s:<5} "
                    f"display={display:<8} visibility={visibility:<8} "
                    f"compositor={walks}"
                )

            # Snapshot DOM size
            screen = app.screen
            total = sum(1 for _ in screen.walk_children(with_self=True))
            preview_pane = app.query_one("#preview_pane")
            pane_desc = sum(1 for _ in preview_pane.walk_children())
            walked = sum(1 for w in preview_pane.walk_children() if w.display)
            print(f"\nDOM total={total} pane_desc={pane_desc} pane_displayed={walked}")
            print("Reach goal: pane_displayed should be ~size of active container only.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
