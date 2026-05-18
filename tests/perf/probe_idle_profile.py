"""Profile what consumes wall time during the supposed-idle phase.

Loads the vault subset (cap=8, 12 files), clicks 8 results, sleeps,
then takes a single `pilot.pause()` call wrapped in cProfile so we can
see exactly which calls dominate the 3-second wait.

Run with:
    ./.venv/bin/python tests/perf/probe_idle_profile.py
"""

from __future__ import annotations

import asyncio
import cProfile
import io
import pstats
import shutil
import sys
import tempfile
import time
from pathlib import Path

_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

from acorn.config import Config, Defaults, RankingProfileConfig  # noqa: E402
from acorn.index import build_index  # noqa: E402
from acorn.tui import AcornApp  # noqa: E402

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
    with tempfile.TemporaryDirectory(prefix="acorn-prof-") as tmp:
        root = Path(tmp)
        corpus = build_vault_subset(root, n=12)
        index_dir = root / "index"
        index_dir.mkdir(parents=True, exist_ok=True)
        build_index(roots=[corpus], index_dir=index_dir, collection="default")
        cfg = Config(
            defaults=Defaults(
                preview_prefetch_count=0,  # H3 was falsified — keep off
                preview_load_debounce_ms=0,
            ),
            ranking={"default": RankingProfileConfig()},
        )
        app = AcornApp(
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
            for n in results:
                tree.cursor_line = n.line
                await asyncio.sleep(0.5)

            # Give everything time to settle.
            await asyncio.sleep(3.0)

            # Count outstanding workers and async tasks before profiling.
            running_tasks = [t for t in asyncio.all_tasks() if not t.done()]
            workers = list(app.workers)
            n_workers_active = sum(1 for w in workers if not w.is_finished)
            print(
                f"PRE-PROFILE: asyncio.all_tasks running={len(running_tasks)} "
                f"app.workers total={len(workers)} active={n_workers_active}"
            )

            # Profile a single pilot.pause() — this is what the bench
            # measures as "after_all".
            profiler = cProfile.Profile()
            t0 = time.perf_counter()
            profiler.enable()
            await pilot.pause()
            profiler.disable()
            wall_ms = (time.perf_counter() - t0) * 1000.0
            print(f"\nsingle pilot.pause() wall-time: {wall_ms:.1f} ms\n")

            buf = io.StringIO()
            stats = pstats.Stats(profiler, stream=buf).sort_stats("cumulative")
            stats.print_stats(30)
            # Also print by tottime to surface bottlenecks.
            stats2 = pstats.Stats(profiler, stream=buf).sort_stats("tottime")
            buf.write("\n\n--- by tottime ---\n")
            stats2.print_stats(20)
            text = buf.getvalue()
            print(text)
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
