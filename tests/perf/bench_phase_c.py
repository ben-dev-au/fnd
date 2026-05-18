"""Phase C re-bench at cap=4 / radius=3 / prefetch=4.

Drives the same standalone scenario as bench_cap_scaling.py for three
corpora — vault heavy, vault random, synthetic — and adds a new
intra-file match-navigation metric: warm a file, click 10 different
section nodes within it, measure click-to-paint latency.

Run with:
    ./.venv/bin/python tests/perf/bench_phase_c.py
"""

from __future__ import annotations

import asyncio
import os
import random
import shutil
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

os.environ.pop("FND_PREVIEW_DIAG", None)
os.environ["FND_REVEAL_FIRST"] = "1"

from fnd.config import Config, Defaults, RankingProfileConfig  # noqa: E402
from fnd.index import build_index  # noqa: E402
from fnd.tui import FNDApp  # noqa: E402
from tests.perf import _corpus  # noqa: E402

MATCH_TOKEN = _corpus.MATCH_TOKEN
VAULT_ROOT = Path(
    "/Users/BenDavidson/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault"
)
VAULT_QUERY = "the"


@dataclass
class Result:
    label: str
    n_clicked: int
    total_widgets: int
    preview_pane_descendants: int
    containers: int
    chunks_in_containers: int
    pause_idle: list[float] = field(default_factory=list)
    pause_between: list[float] = field(default_factory=list)
    pause_after_all: list[float] = field(default_factory=list)
    intra_file_latency: list[float] = field(default_factory=list)


def build_synthetic_corpus(root: Path, *, n_files: int) -> Path:
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    specs = [_corpus.HEAVY, _corpus.TABLE_HEAVY, _corpus.FENCE_HEAVY]
    for i in range(n_files):
        spec = specs[i % len(specs)]
        (corpus / f"file_{i:03d}_{spec.profile}.md").write_text(
            _corpus.render(spec), encoding="utf-8"
        )
    return corpus


def build_vault_heavy(root: Path, *, n_files: int) -> Path:
    if not VAULT_ROOT.exists():
        raise SystemExit(f"vault root not found: {VAULT_ROOT}")
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    md_files: list[tuple[int, Path]] = []
    for p in VAULT_ROOT.rglob("*.md"):
        try:
            md_files.append((p.stat().st_size, p))
        except OSError:
            continue
    md_files.sort(key=lambda t: t[0], reverse=True)
    for _size, src in md_files[:n_files]:
        shutil.copy2(src, corpus / src.name.replace("/", "_"))
    return corpus


def build_vault_random(root: Path, *, n_files: int, seed: int = 42) -> Path:
    if not VAULT_ROOT.exists():
        raise SystemExit(f"vault root not found: {VAULT_ROOT}")
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    md_files = [p for p in VAULT_ROOT.rglob("*.md") if p.is_file()]
    rng = random.Random(seed)
    rng.shuffle(md_files)
    for src in md_files[:n_files]:
        shutil.copy2(src, corpus / src.name.replace("/", "_"))
    return corpus


async def measure_pause(pilot: Any, *, n: int) -> list[float]:
    out: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        await pilot.pause()
        out.append((time.perf_counter() - t0) * 1000.0)
    return out


async def measure_intra_file(app: Any, tree: Any, pilot: Any, n_clicks: int = 10) -> list[float]:
    """Time click-to-paint for n_clicks distinct section nodes under
    the currently-active file (i.e. matches within the same file)."""
    active = app._active_preview
    if active is None and app._active_flat_buffer is None:
        return []
    parent_id = (
        app._active_preview.parent_doc_id
        if app._active_preview is not None
        else app._preview_parent_id
    )
    from textual.widgets.tree import TreeNode  # pyright: ignore[reportMissingImports]

    file_node = None
    for n in list(tree.root.children):
        data = n.data if isinstance(n, TreeNode) else None
        if isinstance(data, dict) and data.get("kind") == "file":
            from fnd.query import FileGroup

            grp: FileGroup = data["group"]
            if grp.parent_id == parent_id:
                file_node = n
                break
    if file_node is None:
        return []
    file_node.expand()
    for _ in range(20):
        await pilot.pause()
        await asyncio.sleep(0.05)
        if file_node.children:
            break
    sections = list(file_node.children)[:n_clicks]
    samples: list[float] = []
    for s in sections:
        t0 = time.perf_counter()
        tree.cursor_line = s.line
        await pilot.pause()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return samples


async def run_one(label: str, corpus: Path, *, query: str, n_clicks: int = 4) -> Result:
    cfg = Config(
        defaults=Defaults(
            preview_prefetch_count=4,
            preview_load_debounce_ms=0,
        ),
        ranking={"default": RankingProfileConfig()},
    )
    index_dir = corpus.parent / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    build_index(roots=[corpus], index_dir=index_dir, collection="default")
    app = FNDApp(index_dir=index_dir, config=cfg, collection="default", initial_query=query)
    from textual.widgets import Tree  # pyright: ignore[reportMissingImports]

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        for _ in range(80):
            await pilot.pause()
            await asyncio.sleep(0.05)
            if len(tree.root.children) >= n_clicks:
                break
        results = list(tree.root.children)
        actual = min(n_clicks, len(results))
        if actual == 0:
            return Result(label, 0, 0, 0, 0, 0)

        pause_idle = await measure_pause(pilot, n=15)
        pause_between: list[float] = []
        for node in results[:actual]:
            tree.cursor_line = node.line
            await asyncio.sleep(0.5)
            pause_between.extend(await measure_pause(pilot, n=10))
        await asyncio.sleep(2.0)
        pause_after_all = await measure_pause(pilot, n=20)

        # Intra-file metric: stay on the last-clicked file, click 10 of its sections.
        intra = await measure_intra_file(app, tree, pilot, n_clicks=10)

        screen = app.screen
        total = sum(1 for _ in screen.walk_children(with_self=True))
        preview_pane = app.query_one("#preview_pane")
        pane_desc = sum(1 for _ in preview_pane.walk_children())
        from fnd.tui.app import PreviewContainer

        containers = list(app.query(PreviewContainer))
        chunks = sum(len(c.children) for c in containers)
        return Result(
            label=label,
            n_clicked=actual,
            total_widgets=total,
            preview_pane_descendants=pane_desc,
            containers=len(containers),
            chunks_in_containers=chunks,
            pause_idle=pause_idle,
            pause_between=pause_between,
            pause_after_all=pause_after_all,
            intra_file_latency=intra,
        )


def fmt_stats(label: str, samples: list[float]) -> str:
    if not samples:
        return f"{label}: no samples"
    n = len(samples)
    med = statistics.median(samples)
    p95 = statistics.quantiles(samples, n=20)[-1] if len(samples) >= 20 else max(samples)
    mx = max(samples)
    return f"{label}: n={n} med={med:6.1f}ms p95={p95:6.1f}ms max={mx:7.1f}ms"


def print_result(r: Result) -> None:
    print(
        f"\n--- {r.label} ---\n"
        f"  DOM total={r.total_widgets} preview_pane={r.preview_pane_descendants} "
        f"containers={r.containers} chunks={r.chunks_in_containers} clicked={r.n_clicked}\n"
        f"  {fmt_stats('idle           ', r.pause_idle)}\n"
        f"  {fmt_stats('between_clicks ', r.pause_between)}\n"
        f"  {fmt_stats('after_all      ', r.pause_after_all)}\n"
        f"  {fmt_stats('intra_file     ', r.intra_file_latency)}"
    )


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="fnd-phc-vh-") as t1:
        corpus = build_vault_heavy(Path(t1), n_files=12)
        r = await run_one("cap=4 vault heavy", corpus, query=VAULT_QUERY, n_clicks=4)
        print_result(r)
    with tempfile.TemporaryDirectory(prefix="fnd-phc-vr-") as t2:
        corpus = build_vault_random(Path(t2), n_files=12)
        r = await run_one("cap=4 vault random", corpus, query=VAULT_QUERY, n_clicks=4)
        print_result(r)
    with tempfile.TemporaryDirectory(prefix="fnd-phc-syn-") as t3:
        corpus = build_synthetic_corpus(Path(t3), n_files=12)
        r = await run_one("cap=4 synthetic", corpus, query=MATCH_TOKEN, n_clicks=4)
        print_result(r)
    print("\nGate: idle med <10 ms, max <50 ms. intra_file should be sub-50 ms.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
