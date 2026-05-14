"""Click-to-display benchmark.

Generates a synthetic corpus (4 md files at different complexity
profiles), indexes it, drives the app via Pilot, and captures
click_to_display_start → click_to_display_end deltas.

Run with:

    ACORN_PERF=1 ./.venv/bin/python tests/perf/bench_reveal.py \\
        --profile heavy --warm cold --runs 5

Output: JSON to stdout (or to ``--out PATH``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Ensure we can import acorn from the worktree.
_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

os.environ.setdefault("ACORN_PERF", "1")  # auto-enable for this entry point

from acorn.config import Config, Defaults, RankingProfileConfig  # noqa: E402
from acorn.index import _path_parent_id, build_index  # noqa: E402
from acorn.tui import AcornApp, _perf  # noqa: E402
from tests.perf import _corpus  # noqa: E402

WarmState = str  # "cold" | "warm"


@dataclass
class BenchResult:
    profile: str
    warm: WarmState
    run: int
    click_to_display_ms: float | None
    path: str | None
    n_marks: int
    notes: str = ""


def _build_corpus(root: Path) -> dict[str, Path]:
    """Write all four profiles into ``root/corpus`` and index them."""
    corpus_root = root / "corpus"
    paths = _corpus.write_corpus(
        corpus_root,
        list(_corpus.PROFILES.values()),
    )
    return {p.name: p for p in paths.values()}


def _build_index(corpus_root: Path, index_dir: Path) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    build_index(roots=[corpus_root], index_dir=index_dir, collection="default")


def _extract_click_to_display(records: list[dict[str, Any]]) -> tuple[float | None, str | None]:
    """Pair the latest start/end marks. Returns (ms, path)."""
    start_ms = None
    end_ms = None
    path = None
    for r in records:
        if r.get("kind") != "mark":
            continue
        if r.get("name") == "click_to_display_start":
            start_ms = r.get("t_ms")
        elif r.get("name") == "click_to_display_end":
            end_ms = r.get("t_ms")
            path = r.get("path")
    if start_ms is None or end_ms is None:
        return None, path
    return end_ms - start_ms, path


async def _wait_for_results(app: AcornApp, pilot: Any, timeout: float = 5.0) -> None:
    """Pump the loop until ``app._groups`` is non-empty or timeout."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        await pilot.pause()
        await asyncio.sleep(0.02)
        if app._groups:
            return
    raise TimeoutError("search returned no results within timeout")


async def _wait_for_display_end(timeout: float = 10.0) -> bool:
    """Pump until at least one click_to_display_end mark is present."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        for r in _perf.records():
            if r.get("kind") == "mark" and r.get("name") == "click_to_display_end":
                return True
        await asyncio.sleep(0.02)
    return False


async def _run_one(
    *,
    index_dir: Path,
    corpus_root: Path,
    profile: str,
    warm: WarmState,
    run: int,
) -> BenchResult:
    """Run a single click-to-display measurement.

    Avoids ``_run_query``'s auto-load by computing ``parent_id``
    directly and firing ``_render_full_doc`` once the searcher is
    ready. For "warm", we run the query first to let prefetch run,
    then reset the perf log and fire a fresh load.
    """
    prefetch_count = 5 if warm == "warm" else 0
    cfg = Config(
        defaults=Defaults(
            preview_prefetch_count=prefetch_count,
            preview_load_debounce_ms=0,
        ),
        ranking={"default": RankingProfileConfig()},
    )
    app = AcornApp(index_dir=index_dir, config=cfg, collection="default")
    target_md = corpus_root / f"{profile}.md"
    if not target_md.exists():
        return BenchResult(
            profile=profile,
            warm=warm,
            run=run,
            click_to_display_ms=None,
            path=None,
            n_marks=0,
            notes=f"corpus file missing: {target_md}",
        )
    parent_id = _path_parent_id(target_md)

    async with app.run_test() as pilot:
        await pilot.pause()
        if warm == "warm":
            # Fire a query so prefetch runs against this file.
            app._run_query(_corpus.MATCH_TOKEN)
            await _wait_for_results(app, pilot)
            # Wait for chunk cache + structural pre-mount (a hidden
            # PreviewContainer for the target with all chunks mounted).
            sig = app._current_query_signature()
            for _ in range(80):
                await pilot.pause()
                await asyncio.sleep(0.05)
                cont = app._preview_cache.get(parent_id, sig)
                if cont is not None and getattr(cont, "is_complete", False):
                    break
            # Also wait for the auto-load triggered by _run_query to
            # settle so its end mark doesn't pollute our measurement.
            await _wait_for_display_end(timeout=10.0)
            await pilot.pause()
            await asyncio.sleep(0.2)

        # Reset marks; we measure ONLY this load.
        _perf.reset()

        # Fire the load directly. focus_chunk_seq=0 is fine — the file
        # has at least one chunk, and the match is somewhere inside it.
        app._render_full_doc(parent_id, focus_chunk_seq=0)

        landed = await _wait_for_display_end(timeout=15.0)
        await pilot.pause()
        await asyncio.sleep(0.1)

        records = _perf.records()
        if not landed:
            return BenchResult(
                profile=profile,
                warm=warm,
                run=run,
                click_to_display_ms=None,
                path=None,
                n_marks=len(records),
                notes="timed out waiting for click_to_display_end",
            )
        delta_ms, path = _extract_click_to_display(records)
        return BenchResult(
            profile=profile,
            warm=warm,
            run=run,
            click_to_display_ms=delta_ms,
            path=path,
            n_marks=len(records),
        )


async def _amain(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="acorn-bench-") as tmp:
        tmp_path = Path(tmp)
        corpus_paths = _build_corpus(tmp_path)
        _build_index(tmp_path / "corpus", tmp_path / "index")

        results: list[BenchResult] = []
        for profile in args.profiles:
            for warm in args.warm:
                for run in range(args.runs):
                    r = await _run_one(
                        index_dir=tmp_path / "index",
                        corpus_root=tmp_path / "corpus",
                        profile=profile,
                        warm=warm,
                        run=run,
                    )
                    results.append(r)
                    # Small breather between runs.
                    await asyncio.sleep(0.1)

        out = {
            "corpus": {k: str(v) for k, v in corpus_paths.items()},
            "config": {"profiles": args.profiles, "warm": args.warm, "runs": args.runs},
            "results": [asdict(r) for r in results],
        }
        text = json.dumps(out, indent=2)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"wrote {args.out}", file=sys.stderr)
        else:
            print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profiles",
        nargs="+",
        default=["small", "heavy", "table_heavy", "fence_heavy"],
        choices=["small", "heavy", "table_heavy", "fence_heavy"],
    )
    parser.add_argument("--warm", nargs="+", default=["cold", "warm"], choices=["cold", "warm"])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out", type=str, default=None)
    args = parser.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
