"""Focused warm-state benchmark.

Pre-mounts the structural widget tree for a target file via the
existing prefetch path, waits for it to fully complete, then fires
``_render_full_doc`` and measures click-to-display.

Does NOT call ``_run_query`` — avoids the auto-load that pollutes
the bench_reveal warm flow. Manually warms the chunk cache + result
list so the prefetch mount path is the ONLY background work.

Outputs:
- click_to_display_ms
- whether the cache-hit pre-reveal branch fired
- retry count for scroll-to-chunk (if available via diag log)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

os.environ.setdefault("ACORN_PERF", "1")

from acorn.config import Config, Defaults, RankingProfileConfig  # noqa: E402
from acorn.index import _path_parent_id, build_index  # noqa: E402
from acorn.query import FileGroup, Hit  # noqa: E402
from acorn.tui import AcornApp, _perf  # noqa: E402
from tests.perf import _corpus  # noqa: E402


@dataclass
class WarmResult:
    profile: str
    run: int
    pre_mount_ms: float | None
    click_to_display_ms: float | None
    path: str | None
    n_marks: int
    notes: str = ""


async def _wait_until(
    predicate: Callable[[], bool], timeout: float = 10.0, step: float = 0.02
) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return False


async def _run_one(*, index_dir: Path, corpus_root: Path, profile: str, run: int) -> WarmResult:
    cfg = Config(
        defaults=Defaults(
            preview_prefetch_count=0,  # we'll manually trigger pre-mount
            preview_load_debounce_ms=0,
        ),
        ranking={"default": RankingProfileConfig()},
    )
    app = AcornApp(index_dir=index_dir, config=cfg, collection="default")
    target_md = corpus_root / f"{profile}.md"
    parent_id = _path_parent_id(target_md)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Configure the app's match-spec so highlighting + first_match_block
        # resolve as they would after a real _run_query.
        from acorn.matching import MatchSpec

        app._current_query = _corpus.MATCH_TOKEN
        app._current_match_spec = MatchSpec.from_query(_corpus.MATCH_TOKEN)
        # Manually populate _chunk_cache (decode synchronously).
        if app._searcher is None:
            return WarmResult(profile, run, None, None, None, 0, "no searcher")
        chunks = app._searcher.get_file_chunks(parent_id, max_workers=4)
        app._chunk_cache[parent_id] = chunks
        # Find the chunk containing MATCH_TOKEN; that's the realistic
        # focus_chunk_seq the app would pass.
        focus_seq = 0
        for c in chunks:
            body = c.body_md or "\n".join(b.text for b in c.blocks)
            if _corpus.MATCH_TOKEN in body:
                focus_seq = c.chunk_seq
                break
        # Provide a single-group result so prefetch helpers don't choke.
        # We bypass _run_query entirely. _groups is a list of FileGroup.
        if not app._groups:
            app._groups = [
                FileGroup(
                    parent_id=parent_id,
                    path=str(target_md),
                    kind="md",
                    title=target_md.name,
                    top_score=1.0,
                    hits=[
                        Hit(
                            score=1.0,
                            parent_id=parent_id,
                            path=str(target_md),
                            kind="md",
                            page=0,
                            slide=0,
                            heading_path="",
                            title=target_md.name,
                            snippet="",
                            chunk_seq=focus_seq,
                        )
                    ],
                )
            ]
        sig = app._current_query_signature()

        # Trigger structural pre-mount manually at the realistic focus.
        t_pre0 = time.perf_counter()
        app._prefetch_mount_structural(parent_id, sig, chunks, focus_seq)

        # Wait for prefetch_loop_end mark in the perf log (signals the
        # bounded pre-mount window has finished — is_complete is too
        # strict because prefetch intentionally mounts only focused +
        # radius, not the whole file).
        def _premount_done() -> bool:
            for r in _perf.records():
                if (
                    r.get("kind") == "mark"
                    and r.get("name") == "prefetch_loop_end"
                    and r.get("parent_id") == parent_id
                ):
                    return True
            return False

        landed = await _wait_until(_premount_done, timeout=15.0)
        pre_mount_ms = (time.perf_counter() - t_pre0) * 1000.0
        if not landed:
            return WarmResult(profile, run, pre_mount_ms, None, None, 0, "pre-mount timed out")

        await pilot.pause()
        await asyncio.sleep(0.05)
        _perf.reset()

        app._render_full_doc(parent_id, focus_chunk_seq=focus_seq)

        for _ in range(200):
            await pilot.pause()
            await asyncio.sleep(0.01)
            recs = _perf.records()
            if any(
                r.get("name") == "click_to_display_end" for r in recs if r.get("kind") == "mark"
            ):
                break
        recs = _perf.records()
        marks = [r for r in recs if r.get("kind") == "mark"]
        start = next((r["t_ms"] for r in marks if r["name"] == "click_to_display_start"), None)
        end_rec = next((r for r in marks if r["name"] == "click_to_display_end"), None)
        if start is None or end_rec is None:
            return WarmResult(
                profile,
                run,
                pre_mount_ms,
                None,
                None,
                len(recs),
                "timed out waiting for click_to_display_end",
            )
        return WarmResult(
            profile=profile,
            run=run,
            pre_mount_ms=pre_mount_ms,
            click_to_display_ms=end_rec["t_ms"] - start,
            path=end_rec.get("path"),
            n_marks=len(recs),
        )


async def _amain(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="acorn-bench-") as tmp:
        tmp_path = Path(tmp)
        _corpus.write_corpus(tmp_path / "corpus", list(_corpus.PROFILES.values()))
        idx = tmp_path / "index"
        idx.mkdir()
        build_index(roots=[tmp_path / "corpus"], index_dir=idx, collection="default")

        results: list[WarmResult] = []
        for profile in args.profiles:
            for run in range(args.runs):
                r = await _run_one(
                    index_dir=idx,
                    corpus_root=tmp_path / "corpus",
                    profile=profile,
                    run=run,
                )
                results.append(r)
                await asyncio.sleep(0.1)

        out = {
            "config": {"profiles": args.profiles, "runs": args.runs},
            "results": [asdict(r) for r in results],
        }
        text = json.dumps(out, indent=2)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"wrote {args.out}", file=sys.stderr)
        else:
            print(text)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profiles",
        nargs="+",
        default=["small", "heavy", "table_heavy", "fence_heavy"],
        choices=["small", "heavy", "table_heavy", "fence_heavy"],
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out", type=str, default=None)
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
