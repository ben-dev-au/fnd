"""Input-lag benchmark.

Measures how long ``pilot.pause()`` actually takes during different
phases of app lifecycle:

- IDLE: app sitting at query results, no recent click
- ACTIVE LOAD: just clicked a result, mount task running
- BACKGROUND FILL: clicked a while ago, tail-mount in progress
- POST-FILL: tail mount done, app should be fully idle

A healthy loop should let ``pilot.pause()`` return in a few ms.
Anything >50 ms is perceptible key lag.

Run with: ./.venv/bin/python tests/perf/bench_input_lag.py
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

DIAG_PATH = Path("/tmp/fnd-preview-diag.log")
if DIAG_PATH.exists():
    DIAG_PATH.unlink()
os.environ["FND_PREVIEW_DIAG"] = "1"
os.environ["FND_REVEAL_FIRST"] = "1"

from fnd.config import Config, Defaults, RankingProfileConfig  # noqa: E402
from fnd.index import build_index  # noqa: E402
from fnd.tui import FNDApp  # noqa: E402
from tests.perf import _corpus  # noqa: E402

MATCH_TOKEN = _corpus.MATCH_TOKEN


def build_corpus(root: Path, *, n_md: int = 6, n_txt: int = 6) -> Path:
    """Same corpus as auto_test.py — mixed md + txt."""
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    md_specs = [_corpus.SMALL, _corpus.HEAVY, _corpus.TABLE_HEAVY, _corpus.FENCE_HEAVY]
    for i in range(n_md):
        spec = md_specs[i % len(md_specs)]
        path = corpus / f"md_{i:02d}_{spec.profile}.md"
        path.write_text(_corpus.render(spec), encoding="utf-8")
    for i in range(n_txt):
        path = corpus / f"txt_{i:02d}.txt"
        lines = []
        target_line = 200 + i * 20
        for ln in range(500 + i * 100):
            if ln == target_line:
                lines.append(f"Line {ln}: contains {MATCH_TOKEN} here.")
            else:
                lines.append(
                    f"Line {ln}: filler content with some words to make this look "
                    f"like a real document with paragraphs of reasonable length."
                )
        path.write_text("\n".join(lines), encoding="utf-8")
    return corpus


@dataclass
class LagSample:
    phase: str
    pause_ms: float


async def measure_phase(pilot: Any, phase: str, *, n: int = 20) -> list[LagSample]:
    """Take ``n`` measurements of both pilot.pause() and asyncio.sleep(0).
    asyncio.sleep(0) is a near-zero-cost yield; pilot.pause() walks the
    DOM and waits for messages. Comparing reveals whether lag is in the
    event loop itself or in Textual's screen-settle path."""
    samples: list[LagSample] = []
    for _ in range(n):
        t0 = time.perf_counter()
        await pilot.pause()
        ms = (time.perf_counter() - t0) * 1000.0
        samples.append(LagSample(phase=phase, pause_ms=ms))
        t0 = time.perf_counter()
        await asyncio.sleep(0)
        ms = (time.perf_counter() - t0) * 1000.0
        samples.append(LagSample(phase=f"{phase}_sleep0", pause_ms=ms))
    return samples


async def drive(corpus_root: Path) -> dict[str, list[float]]:
    import os as _os

    from textual.widgets import Tree

    prefetch = 0 if _os.environ.get("BENCH_NO_PREFETCH") == "1" else 10
    cfg = Config(
        defaults=Defaults(
            preview_prefetch_count=prefetch,
            preview_load_debounce_ms=0,
        ),
        ranking={"default": RankingProfileConfig()},
    )

    index_dir = corpus_root.parent / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    build_index(roots=[corpus_root], index_dir=index_dir, collection="default")

    app = FNDApp(
        index_dir=index_dir,
        config=cfg,
        collection="default",
        initial_query=MATCH_TOKEN,
    )

    phase_samples: dict[str, list[float]] = {
        "boot_idle": [],
        "boot_idle_sleep0": [],
        "click_immediate": [],
        "click_immediate_sleep0": [],
        "click_settle": [],
        "click_settle_sleep0": [],
        "between_clicks_idle": [],
        "between_clicks_idle_sleep0": [],
        "all_done": [],
        "all_done_sleep0": [],
    }

    async with app.run_test(size=(140, 40)) as pilot:
        # Boot — measure lag while app initializes and prefetch may fire.
        await pilot.pause()
        for s in await measure_phase(pilot, "boot_idle", n=20):
            phase_samples[s.phase].append(s.pause_ms)

        tree = app.query_one("#results_pane", Tree)
        for _ in range(30):
            await pilot.pause()
            await asyncio.sleep(0.05)
            if len(tree.root.children) > 0:
                break

        results = list(tree.root.children)
        if not results:
            return phase_samples

        # Click each result; measure lag IMMEDIATELY after click and again
        # after tail-mount should have settled.
        for _i, node in enumerate(results[:6]):
            tree.cursor_line = node.line
            # Right after the click: this is when the mount task is busy.
            for s in await measure_phase(pilot, "click_immediate", n=10):
                phase_samples[s.phase].append(s.pause_ms)
            # After a few seconds, mount should have settled.
            await asyncio.sleep(1.5)
            for s in await measure_phase(pilot, "click_settle", n=10):
                phase_samples[s.phase].append(s.pause_ms)
            # Between clicks — app is idle but cache has many containers.
            for s in await measure_phase(pilot, "between_clicks_idle", n=10):
                phase_samples[s.phase].append(s.pause_ms)

        # Long idle — everything should be done.
        await asyncio.sleep(3.0)
        for s in await measure_phase(pilot, "all_done", n=30):
            phase_samples[s.phase].append(s.pause_ms)

        # Diagnose: count widgets in the tree.
        screen = app.screen
        widget_count = sum(1 for _ in screen.walk_children(with_self=True))
        preview_pane = app.query_one("#preview_pane")
        pane_descendants = sum(1 for _ in preview_pane.walk_children())
        from fnd.tui.app import PreviewContainer

        containers = list(app.query(PreviewContainer))
        hidden_containers = [c for c in containers if c.has_class("-hidden")]
        chunks_in_containers = sum(len(c.children) for c in containers)
        print(
            f"\nDOM stats: total_widgets={widget_count} "
            f"preview_pane_descendants={pane_descendants} "
            f"containers={len(containers)} "
            f"hidden_containers={len(hidden_containers)} "
            f"chunks_in_containers={chunks_in_containers}"
        )

    return phase_samples


def summarize(samples: dict[str, list[float]]) -> str:
    out = ["", "=" * 70, "Input-lag (pilot.pause wall-time) by phase", "=" * 70]
    for phase, vals in samples.items():
        if not vals:
            out.append(f"{phase:24s} no samples")
            continue
        out.append(
            f"{phase:24s} n={len(vals):3d}  "
            f"min={min(vals):6.2f}ms  "
            f"med={statistics.median(vals):6.2f}ms  "
            f"p95={statistics.quantiles(vals, n=20)[-1] if len(vals) >= 20 else max(vals):6.2f}ms  "
            f"max={max(vals):7.2f}ms"
        )
    out.append("=" * 70)
    out.append("Healthy: med <10 ms, max <50 ms. >50 ms = perceptible lag.")
    return "\n".join(out)


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="fnd-lag-") as tmp:
        root = Path(tmp)
        corpus = build_corpus(root)
        samples = await drive(corpus)
    print(summarize(samples))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
