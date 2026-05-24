"""Verify that pilot.pause cost scales with mounted-widget count.

If the hypothesis "asyncio task count drives pilot.pause latency" is
right, we should see the cost climb monotonically as we vary the
number of cached PreviewContainers (via clicks), regardless of which
one is "active".

Run with:
    ./.venv/bin/python tests/perf/probe_widget_scaling.py
"""

from __future__ import annotations

import asyncio
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

from fnd.config import Config, Defaults, RankingProfileConfig  # noqa: E402
from fnd.index import build_index  # noqa: E402
from fnd.tui import FNDApp  # noqa: E402

VAULT_ROOT = Path(
    "/Users/BenDavidson/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault"
)


def build_vault_subset(root: Path, *, n: int) -> Path:
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    md_files: list[tuple[int, Path]] = []
    for p in VAULT_ROOT.rglob("*.md"):
        try:
            md_files.append((p.stat().st_size, p))
        except OSError:
            continue
    md_files.sort(key=lambda t: t[0], reverse=True)
    for _size, src in md_files[:n]:
        shutil.copy2(src, corpus / src.name.replace("/", "_"))
    return corpus


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="fnd-scaling-") as tmp:
        root = Path(tmp)
        corpus = build_vault_subset(root, n=10)
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
            initial_query="the",
        )
        from textual.widgets import Tree

        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            tree = app.query_one("#results_pane", Tree)
            for _ in range(40):
                await pilot.pause()
                await asyncio.sleep(0.05)
                if len(tree.root.children) >= 8:
                    break
            results = list(tree.root.children)[:8]

            screen = app.screen
            preview_pane = app.query_one("#preview_pane")

            async def settle() -> None:
                await asyncio.sleep(3.0)

            async def sample_pause(n: int = 20) -> list[float]:
                out: list[float] = []
                for _ in range(n):
                    t0 = time.perf_counter()
                    await pilot.pause()
                    out.append((time.perf_counter() - t0) * 1000.0)
                return out

            def dom_counts() -> tuple[int, int, int]:
                total = sum(1 for _ in screen.walk_children(with_self=True))
                pane = sum(1 for _ in preview_pane.walk_children())
                tasks = sum(1 for t in asyncio.all_tasks() if not t.done())
                return total, pane, tasks

            print(
                f"{'cached':>7} {'DOM':>7} {'pane':>7} {'tasks':>7} "
                f"{'pause_med':>11} {'pause_p95':>11} {'pause_max':>11}"
            )
            # Sample at 0, 1, 2, 4, 8 cached files.
            for target in [0, 1, 2, 4, 8]:
                while target > 0 and target > sum(1 for _ in app.query("PreviewContainer")):
                    next_idx = sum(1 for _ in app.query("PreviewContainer"))
                    tree.cursor_line = results[next_idx].line
                    await asyncio.sleep(0.5)
                await settle()
                t, p, tk = dom_counts()
                samples = await sample_pause(n=20)
                med = statistics.median(samples)
                p95 = (
                    statistics.quantiles(samples, n=20)[-1] if len(samples) >= 20 else max(samples)
                )
                mx = max(samples)
                print(f"{target:>7} {t:>7} {p:>7} {tk:>7} {med:>9.1f}ms {p95:>9.1f}ms {mx:>9.1f}ms")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
