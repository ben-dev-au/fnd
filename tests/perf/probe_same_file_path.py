"""Timed trace of the same-file scroll path under 4 cache-load conditions.

Captures per-method timings AND the refresh-tick gap between
``_scroll_preview_to_chunk`` (which schedules via call_after_refresh)
and the first invocation of ``_do_scroll_to_chunk``. Also captures
the full click-to-paint envelope.

All instrumentation is instance-level (monkey-patched onto the app
object). No production code is changed. The probe uses the user's
real Obsidian vault.

Run with:
    ./.venv/bin/python tests/perf/probe_same_file_path.py
"""

from __future__ import annotations

import asyncio
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

from acorn.config import Config, Defaults, RankingProfileConfig  # noqa: E402
from acorn.index import build_index  # noqa: E402
from acorn.tui import AcornApp  # noqa: E402
from acorn.tui import app as _app_mod  # noqa: E402

VAULT_ROOT = Path(
    "/Users/BenDavidson/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault"
)
STEP_NAMES = [
    "_on_tree_highlight",
    "_schedule_preview_load",
    "_fire_pending_preview_load",
    "_prefetch_top_results",
    "_render_full_doc",
    "_dispatch_preview_mount",
    "_scroll_preview_to_chunk",
    "_do_scroll_to_chunk",
]


def build_vault_subset(root: Path, *, n: int) -> Path:
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
    for _size, src in md_files[:n]:
        shutil.copy2(src, corpus / src.name.replace("/", "_"))
    return corpus


def install_timing(app: AcornApp, sink: dict[str, list[float]], gap_sink: list[float]) -> None:
    """Wrap each tracked method with perf_counter timing. Also records
    the gap between ``_scroll_preview_to_chunk`` exit and the next
    ``_do_scroll_to_chunk`` entry (i.e. the refresh-tick wait)."""
    last_scroll_exit: dict[str, float | None] = {"t": None}

    for name in STEP_NAMES:
        sink.setdefault(name, [])
        orig = getattr(app, name)

        def make(orig_fn: Any, n: str) -> Any:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                if n == "_do_scroll_to_chunk" and last_scroll_exit["t"] is not None:
                    gap_sink.append((time.perf_counter() - last_scroll_exit["t"]) * 1000.0)
                    last_scroll_exit["t"] = None  # only the first entry per click
                t0 = time.perf_counter()
                try:
                    return orig_fn(*args, **kwargs)
                finally:
                    sink[n].append((time.perf_counter() - t0) * 1000.0)
                    if n == "_scroll_preview_to_chunk":
                        last_scroll_exit["t"] = time.perf_counter()

            return wrapper

        setattr(app, name, make(orig, name))


def remove_timing(app: AcornApp) -> None:
    for name in STEP_NAMES:
        if name in app.__dict__:
            delattr(app, name)


def fmt_steps(label: str, samples: dict[str, list[float]], gaps: list[float]) -> str:
    out = [f"\n=== {label} ==="]
    out.append(f"{'step':>28} {'n':>3} {'med':>9} {'p95':>9} {'max':>9}")
    for name in STEP_NAMES:
        v = samples.get(name, [])
        if not v:
            out.append(f"{name:>28} {0:>3} {'-':>9} {'-':>9} {'-':>9}")
            continue
        med = statistics.median(v)
        p95 = statistics.quantiles(v, n=20)[-1] if len(v) >= 20 else max(v)
        mx = max(v)
        out.append(f"{name:>28} {len(v):>3} {med:>7.1f}ms {p95:>7.1f}ms {mx:>7.1f}ms")
    if gaps:
        med = statistics.median(gaps)
        p95 = statistics.quantiles(gaps, n=20)[-1] if len(gaps) >= 20 else max(gaps)
        mx = max(gaps)
        out.append(
            f"{'refresh_tick_wait':>28} {len(gaps):>3} {med:>7.1f}ms "
            f"{p95:>7.1f}ms {mx:>7.1f}ms  (gap _scroll→_do_scroll)"
        )
    else:
        out.append(f"{'refresh_tick_wait':>28}   0      -        -        -")
    return "\n".join(out)


async def warm_to_cap(app: Any, tree: Any, target: int, results: list[Any]) -> int:
    """Click distinct file rows so 'target' containers end up in cache.
    Returns number of files actually warmed."""
    warmed = 0
    for node in results:
        if warmed >= target:
            break
        tree.cursor_line = node.line
        await asyncio.sleep(0.8)
        warmed = sum(1 for _ in app.query("PreviewContainer"))
    return warmed


async def intra_file_clicks(app: Any, tree: Any, n_clicks: int) -> int:
    """Click n_clicks distinct section nodes under the currently-active
    file. Returns number of section clicks fired."""
    active = app._active_preview
    if active is None:
        return 0
    target_parent = active.parent_doc_id
    from textual.widgets.tree import TreeNode  # pyright: ignore[reportMissingImports]

    for file_node in list(tree.root.children):
        data = file_node.data if isinstance(file_node, TreeNode) else None
        if isinstance(data, dict) and data.get("kind") == "file":
            from acorn.query import FileGroup

            grp: FileGroup = data["group"]
            if grp.parent_id != target_parent:
                continue
            file_node.expand()
            for _ in range(20):
                await asyncio.sleep(0.05)
                if file_node.children:
                    break
            sections = list(file_node.children)[:n_clicks]
            fired = 0
            for s in sections:
                tree.cursor_line = s.line
                fired += 1
                await asyncio.sleep(0.4)
            return fired
    return 0


async def run_scenario(
    label: str,
    index_dir: Path,
    cfg: Config,
    *,
    cap: int,
    n_warm: int,
    intra_clicks: int,
    cap_override: int | None = None,
) -> str:
    prior: int | None = None
    if cap_override is not None:
        prior = _app_mod._PREVIEW_CACHE_MAX_FILES
        _app_mod._PREVIEW_CACHE_MAX_FILES = cap_override
    try:
        app = AcornApp(index_dir=index_dir, config=cfg, collection="default", initial_query="the")
        if cap_override is not None:
            app._preview_cache.max_files = cap_override
        from textual.widgets import Tree  # pyright: ignore[reportMissingImports]

        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            tree = app.query_one("#results_pane", Tree)
            for _ in range(40):
                await pilot.pause()
                await asyncio.sleep(0.05)
                if len(tree.root.children) >= n_warm + 1:
                    break
            results = list(tree.root.children)

            # Cold special-case: cursor lands on results[0] by default,
            # so re-setting it is a no-op (Textual dedups). Move away
            # first, install timing, then click a DIFFERENT row.
            if label == "cold":
                samples: dict[str, list[float]] = {}
                gaps: list[float] = []
                # Park the cursor somewhere else first.
                if len(results) >= 2:
                    tree.cursor_line = results[1].line
                    await asyncio.sleep(0.6)
                install_timing(app, samples, gaps)
                target_row = results[2] if len(results) >= 3 else results[0]
                tree.cursor_line = target_row.line
                await asyncio.sleep(3.0)
                remove_timing(app)
                screen = app.screen
                total = sum(1 for _ in screen.walk_children(with_self=True))
                tasks = sum(1 for t in asyncio.all_tasks() if not t.done())
                return (
                    f"  ctx: DOM={total} tasks={tasks} files_cached="
                    f"{sum(1 for _ in app.query('PreviewContainer'))}\n"
                    f"{fmt_steps(label, samples, gaps)}"
                )

            # Warmup phase — NOT timed.
            actual_warm = await warm_to_cap(app, tree, n_warm, results)
            await asyncio.sleep(2.0)
            # Real measurement.
            samples = {}
            gaps = []
            install_timing(app, samples, gaps)
            fired = await intra_file_clicks(app, tree, intra_clicks)
            await asyncio.sleep(2.0)
            remove_timing(app)
            screen = app.screen
            total = sum(1 for _ in screen.walk_children(with_self=True))
            tasks = sum(1 for t in asyncio.all_tasks() if not t.done())
            return (
                f"  ctx: DOM={total} tasks={tasks} files_cached={actual_warm} "
                f"intra_clicks_fired={fired}\n"
                f"{fmt_steps(label, samples, gaps)}"
            )
    finally:
        if prior is not None:
            _app_mod._PREVIEW_CACHE_MAX_FILES = prior


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="acorn-samefile-") as tmp:
        root = Path(tmp)
        corpus = build_vault_subset(root, n=24)
        index_dir = root / "index"
        index_dir.mkdir(parents=True, exist_ok=True)
        build_index(roots=[corpus], index_dir=index_dir, collection="default")

        # All scenarios run with debounce=0 so we time only the work, not
        # the deliberate 150 ms debounce delay (which is by design and
        # appears in real usage on every cursor move).
        cfg = Config(
            defaults=Defaults(preview_prefetch_count=0, preview_load_debounce_ms=0),
            ranking={"default": RankingProfileConfig()},
        )

        print("\nNote: probe runs with debounce=0; add 150 ms to every click in real usage.\n")

        out = await run_scenario("cold", index_dir, cfg, cap=4, n_warm=0, intra_clicks=0)
        print(out)

        out = await run_scenario("warm_1", index_dir, cfg, cap=4, n_warm=1, intra_clicks=10)
        print(out)

        cfg_with_prefetch = Config(
            defaults=Defaults(preview_prefetch_count=4, preview_load_debounce_ms=0),
            ranking={"default": RankingProfileConfig()},
        )
        out = await run_scenario(
            "cap4", index_dir, cfg_with_prefetch, cap=4, n_warm=4, intra_clicks=10
        )
        print(out)

        out = await run_scenario(
            "cap16",
            index_dir,
            cfg_with_prefetch,
            cap=4,
            n_warm=16,
            intra_clicks=10,
            cap_override=16,
        )
        print(out)
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
