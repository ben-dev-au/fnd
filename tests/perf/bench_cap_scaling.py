"""Cache-cap scaling bench — pilot.pause + DOM widget count at multiple caps.

Drives the same scenario as bench_input_lag (open the app, click N
result rows, measure pilot.pause distributions and DOM size) but
parametrised by `_PREVIEW_CACHE_MAX_FILES`. Used to verify the Stage 1
ship gate at the configured production cap (and beyond), not just at
whatever corpus size the smaller bench happens to fill.

Run with:
    ./.venv/bin/python tests/perf/bench_cap_scaling.py --corpus synthetic
    ./.venv/bin/python tests/perf/bench_cap_scaling.py --corpus vault

Or single-cap measurement:
    ./.venv/bin/python tests/perf/bench_cap_scaling.py --cap 16 --corpus synthetic
"""

from __future__ import annotations

import argparse
import asyncio
import os
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

# Silence diag chatter; we want bench numbers not log spam.
os.environ.pop("ACORN_PREVIEW_DIAG", None)
os.environ["ACORN_REVEAL_FIRST"] = "1"

from acorn.config import Config, Defaults, RankingProfileConfig  # noqa: E402
from acorn.index import build_index  # noqa: E402
from acorn.tui import AcornApp  # noqa: E402
from acorn.tui import app as _app_mod  # noqa: E402
from tests.perf import _corpus  # noqa: E402

MATCH_TOKEN = _corpus.MATCH_TOKEN
VAULT_ROOT = Path(
    "/Users/BenDavidson/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault"
)
VAULT_QUERY = "the"  # common-word query: returns many vault files


@dataclass
class CapResult:
    cap: int
    corpus: str
    n_clicked: int
    total_widgets: int
    preview_pane_descendants: int
    containers: int
    chunks_in_containers: int
    pause: dict[str, list[float]] = field(default_factory=dict)


def build_synthetic_corpus(root: Path, *, n_files: int) -> Path:
    """Mix of HEAVY/TABLE_HEAVY/FENCE_HEAVY md files — every file is structurally heavy."""
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    specs = [_corpus.HEAVY, _corpus.TABLE_HEAVY, _corpus.FENCE_HEAVY]
    for i in range(n_files):
        spec = specs[i % len(specs)]
        (corpus / f"file_{i:03d}_{spec.profile}.md").write_text(
            _corpus.render(spec), encoding="utf-8"
        )
    return corpus


def build_vault_subset(root: Path, *, n_files: int) -> Path:
    """Symlink the n_files largest .md files in the user's Obsidian vault."""
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
        # Avoid awkward filenames; just copy.
        safe = src.name.replace("/", "_")
        shutil.copy2(src, corpus / safe)
    return corpus


async def measure_pause(pilot: Any, *, n: int) -> list[float]:
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        await pilot.pause()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return samples


async def drive(corpus_root: Path, *, cap: int, query: str, n_clicks: int) -> CapResult:
    from textual.widgets import Tree

    # Bump the module-level cap BEFORE constructing the app so both
    # _flat_buffer_cache (reads it at every eviction) and the default
    # PreviewCache max_files (binds the module value at class-definition
    # time) see the new value.
    _app_mod._PREVIEW_CACHE_MAX_FILES = cap

    cfg = Config(
        defaults=Defaults(preview_prefetch_count=10, preview_load_debounce_ms=0),
        ranking={"default": RankingProfileConfig()},
    )
    index_dir = corpus_root.parent / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    build_index(roots=[corpus_root], index_dir=index_dir, collection="default")

    app = AcornApp(index_dir=index_dir, config=cfg, collection="default", initial_query=query)
    # PreviewCache binds the cap default at class-definition; override the instance.
    app._preview_cache.max_files = cap

    pause: dict[str, list[float]] = {"idle": [], "between_clicks": [], "after_all": []}
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        # Wait for results.
        tree = app.query_one("#results_pane", Tree)
        for _ in range(80):
            await pilot.pause()
            await asyncio.sleep(0.05)
            if len(tree.root.children) >= n_clicks:
                break
        results = list(tree.root.children)
        if not results:
            return CapResult(
                cap=cap,
                corpus=corpus_root.parent.name,
                n_clicked=0,
                total_widgets=0,
                preview_pane_descendants=0,
                containers=0,
                chunks_in_containers=0,
            )

        # Idle baseline before any click.
        pause["idle"].extend(await measure_pause(pilot, n=15))

        # Click N rows to fill the cache.
        actual = min(n_clicks, len(results))
        for node in results[:actual]:
            tree.cursor_line = node.line
            await asyncio.sleep(0.5)  # let mount settle
            pause["between_clicks"].extend(await measure_pause(pilot, n=10))

        await asyncio.sleep(2.0)
        pause["after_all"].extend(await measure_pause(pilot, n=20))

        screen = app.screen
        total = sum(1 for _ in screen.walk_children(with_self=True))
        preview_pane = app.query_one("#preview_pane")
        pane_desc = sum(1 for _ in preview_pane.walk_children())
        from acorn.tui.app import PreviewContainer

        containers = list(app.query(PreviewContainer))
        chunks_in_containers = sum(len(c.children) for c in containers)
        return CapResult(
            cap=cap,
            corpus=corpus_root.parent.name,
            n_clicked=actual,
            total_widgets=total,
            preview_pane_descendants=pane_desc,
            containers=len(containers),
            chunks_in_containers=chunks_in_containers,
            pause=pause,
        )


def fmt_pause(label: str, samples: list[float]) -> str:
    if not samples:
        return f"{label}: no samples"
    n = len(samples)
    med = statistics.median(samples)
    p95 = statistics.quantiles(samples, n=20)[-1] if len(samples) >= 20 else max(samples)
    mx = max(samples)
    return f"{label}: n={n} med={med:5.1f}ms p95={p95:5.1f}ms max={mx:6.1f}ms"


def print_row(r: CapResult) -> None:
    print(
        f"\n--- corpus={r.corpus} cap={r.cap} clicked={r.n_clicked} ---\n"
        f"  DOM total={r.total_widgets} preview_pane={r.preview_pane_descendants} "
        f"containers={r.containers} chunks_in_containers={r.chunks_in_containers}\n"
        f"  {fmt_pause('idle           ', r.pause.get('idle', []))}\n"
        f"  {fmt_pause('between_clicks ', r.pause.get('between_clicks', []))}\n"
        f"  {fmt_pause('after_all      ', r.pause.get('after_all', []))}\n"
    )


async def run_matrix(corpus: str, caps: list[int]) -> None:
    target_clicks = max(caps)
    n_files = target_clicks + 4
    with tempfile.TemporaryDirectory(prefix=f"acorn-cap-{corpus}-") as tmp:
        root = Path(tmp)
        if corpus == "synthetic":
            corpus_root = build_synthetic_corpus(root, n_files=n_files)
            query = MATCH_TOKEN
        elif corpus == "vault":
            corpus_root = build_vault_subset(root, n_files=n_files)
            query = VAULT_QUERY
        else:
            raise SystemExit(f"unknown corpus: {corpus}")
        for cap in caps:
            r = await drive(corpus_root, cap=cap, query=query, n_clicks=cap)
            r.corpus = corpus
            print_row(r)


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--corpus", choices=["synthetic", "vault", "both"], default="both")
    p.add_argument("--cap", type=int, default=None, help="single cap; default = matrix")
    args = p.parse_args()
    caps = [args.cap] if args.cap else [8, 16, 32, 64]
    corpora = ["synthetic", "vault"] if args.corpus == "both" else [args.corpus]
    for c in corpora:
        # Vault build is slow at large caps; cap that branch at 32.
        eff = [k for k in caps if k <= 32] if c == "vault" else caps
        print(f"\n======= corpus: {c} (caps: {eff}) =======")
        await run_matrix(c, eff)
    print("\nHealthy: med <10 ms (one tick), max <50 ms (gate).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
