"""Drive every settings workflow end-to-end and report failures.

A more rigorous companion to ``tools/render_screen.py``. Snapshot
rendering captures layout; this harness captures BEHAVIOUR:

- Push each workflow's entrypoint.
- Drive the UI via pilot.
- Watch for crashes, exceptions in background tasks, dangling
  modals, and stuck state.
- Emit a single-line PASS / FAIL per workflow, with the failure
  reason.

Run after any change that touches a settings workflow. The
underlying tests live in ``tests/workflows/``; this script is a
faster way to get a summary read on what's broken without spinning
up the full pytest harness.

Usage:
    uv run python tools/workflow_audit.py [WORKFLOW ...]
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fnd.config import CollectionConfig, Config, Defaults, SourceConfig  # noqa: E402
from fnd.index import build_index  # noqa: E402
from fnd.tui import FNDApp  # noqa: E402

_TERM_SIZE = (120, 40)
_PILOT_TICKS = 60


def _isolate_cache(name: str) -> None:
    """Point fnd's PDF structure cache at a per-workflow tmp dir so
    tests don't see whatever's in the user's real cache. Reset the
    cached singleton in fnd.extract.pdf so the new dir takes effect."""
    from fnd.extract import pdf as _pdf

    scratch = _REPO_ROOT / "tools" / "snapshots" / ".scratch-workflow" / name / "cache"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    import fnd.cache as cache_mod

    cache_mod.default_cache_dir = lambda: scratch  # type: ignore[assignment]
    _pdf._cache_singleton = None


def _scratch_corpus(name: str) -> tuple[Path, Path]:
    """Per-workflow scratch corpus + index. Cached between runs."""
    root = _REPO_ROOT / "tools" / "snapshots" / ".scratch-workflow" / name
    corpus = root / "corpus"
    index = root / "index"
    if not corpus.exists():
        corpus.mkdir(parents=True, exist_ok=True)
        (corpus / "alpha.md").write_text("# alpha\n\nbody\n")
    if not index.exists():
        index.mkdir(parents=True, exist_ok=True)
        build_index(roots=[corpus], index_dir=index, collection="default")
    return corpus, index


def _three_collection_config(corpus: Path) -> Config:
    return Config(
        defaults=Defaults(),
        collections={
            name: CollectionConfig(sources=[SourceConfig(path=corpus)])
            for name in ("alpha", "beta", "gamma")
        },
    )


def _one_collection_config(corpus: Path) -> Config:
    return Config(
        defaults=Defaults(),
        collections={"default": CollectionConfig(sources=[SourceConfig(path=corpus)])},
    )


# ── Workflows ─────────────────────────────────────────────────────


async def _update_all_visits_every_collection() -> str:
    """Confirm > Yes triggers a run for every queued collection."""
    from fnd.tui.settings_screen import UpdateAllConfirm

    corpus, index = _scratch_corpus("update_all")
    cfg = _three_collection_config(corpus)
    app = FNDApp(index_dir=index, config=cfg)
    invocations: list[str] = []
    original = app.start_indexer

    def _record(*, collection: str, **kw: object) -> bool:
        invocations.append(collection)
        return original(collection=collection, **kw)  # type: ignore[arg-type]

    app.start_indexer = _record  # type: ignore[method-assign]

    async with app.run_test(size=_TERM_SIZE) as pilot:
        await pilot.pause()
        app.push_screen(UpdateAllConfirm(collection_names=["alpha", "beta", "gamma"]))
        await pilot.pause()
        await pilot.press("enter")
        for _ in range(_PILOT_TICKS):
            await pilot.pause()
            if len(invocations) >= 3:
                break

    if invocations == ["alpha", "beta", "gamma"]:
        return "ok"
    return f"chain didn't iterate; saw {invocations}"


async def _install_confirm_mounts() -> str:
    from fnd.tui.menu import _open_pdf_install_confirm
    from fnd.tui.settings_screen import StructuredPdfConfirmScreen

    corpus, index = _scratch_corpus("install_confirm")
    cfg = _one_collection_config(corpus)
    app = FNDApp(index_dir=index, config=cfg)
    async with app.run_test(size=_TERM_SIZE) as pilot:
        await pilot.pause()
        _open_pdf_install_confirm(app)
        await pilot.pause()
        if not isinstance(app.screen, StructuredPdfConfirmScreen):
            return f"expected StructuredPdfConfirmScreen, got {type(app.screen).__name__}"
    return "ok"


async def _failed_install_swaps_to_close_button() -> str:
    from textual.widgets import OptionList

    from fnd.tui.extras_install_progress import (
        ExtrasInstallProgressScreen,
        ProgressEvent,
    )

    corpus, index = _scratch_corpus("failed_install")
    cfg = _one_collection_config(corpus)
    app = FNDApp(index_dir=index, config=cfg)
    async with app.run_test(size=_TERM_SIZE) as pilot:
        await pilot.pause()
        screen = ExtrasInstallProgressScreen(action_label="Install")
        app.push_screen(screen)
        await pilot.pause()
        screen._render_event(
            ProgressEvent(phase="failed", cmd_index=0, cmd_total=2, error="exit 1")
        )
        for _ in range(_PILOT_TICKS):
            await pilot.pause()
            terminal_opt = None
            with suppress(Exception):
                terminal_opt = screen.query_one("#extras_actions_terminal", OptionList)
            if terminal_opt is not None and not terminal_opt.has_class("-hidden"):
                break
        terminal = screen.query_one("#extras_actions_terminal", OptionList)
        running = screen.query_one("#extras_actions_running", OptionList)
        if terminal.has_class("-hidden"):
            return "Close OptionList didn't reveal after failed event"
        if not running.has_class("-hidden"):
            return "Running OptionList didn't hide after failed event"
    return "ok"


async def _first_reindex_skipped_without_pdfs() -> str:
    from fnd.tui.first_reindex_warning import FirstReindexWarningScreen

    corpus, index = _scratch_corpus("first_reindex_skip")
    cfg = _one_collection_config(corpus)
    app = FNDApp(index_dir=index, config=cfg)
    async with app.run_test(size=_TERM_SIZE) as pilot:
        await pilot.pause()
        app._reindex_with_warning_if_needed("default")
        await pilot.pause()
        if any(isinstance(s, FirstReindexWarningScreen) for s in app.screen_stack):
            return "warning shown for md-only collection (should skip)"
    return "ok"


async def _cache_clear_when_empty_is_noop() -> str:
    _isolate_cache("cache_clear_empty")
    from fnd.tui.menu import _run_cache_clear
    from fnd.tui.settings_screen import CacheMaintenanceConfirm

    corpus, index = _scratch_corpus("cache_clear_empty")
    cfg = _one_collection_config(corpus)
    app = FNDApp(index_dir=index, config=cfg)
    async with app.run_test(size=_TERM_SIZE) as pilot:
        await pilot.pause()
        before = type(app.screen)
        _run_cache_clear(app)
        await pilot.pause()
        if isinstance(app.screen, CacheMaintenanceConfirm):
            return "Clear pushed a confirm screen on an empty cache"
        if type(app.screen) is not before:
            return f"unexpected screen change: {type(app.screen).__name__}"
    return "ok"


async def _delete_cancel_keeps_collection() -> str:
    from fnd.tui.settings_screen import DeleteCollectionScreen

    corpus, index = _scratch_corpus("delete_cancel")
    cfg = _three_collection_config(corpus)
    app = FNDApp(index_dir=index, config=cfg)
    async with app.run_test(size=_TERM_SIZE) as pilot:
        await pilot.pause()
        app.push_screen(DeleteCollectionScreen(collection_name="beta"))
        await pilot.pause()
        # Yes is first; arrow to Cancel and Enter.
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        if app._config is None or "beta" not in app._config.collections:
            return "beta was deleted despite Cancel"
    return "ok"


WORKFLOWS: dict[str, Callable[[], Awaitable[str]]] = {
    "update_all_visits_every_collection": _update_all_visits_every_collection,
    "install_confirm_mounts": _install_confirm_mounts,
    "failed_install_swaps_to_close_button": _failed_install_swaps_to_close_button,
    "first_reindex_skipped_without_pdfs": _first_reindex_skipped_without_pdfs,
    "cache_clear_when_empty_is_noop": _cache_clear_when_empty_is_noop,
    "delete_cancel_keeps_collection": _delete_cancel_keeps_collection,
}


def main() -> int:
    names = sys.argv[1:] or list(WORKFLOWS)
    unknown = [n for n in names if n not in WORKFLOWS]
    if unknown:
        print(f"unknown: {unknown}", file=sys.stderr)
        print(f"available: {sorted(WORKFLOWS)}", file=sys.stderr)
        return 2
    failures = 0
    width = max(len(n) for n in names)
    for name in names:
        fn = WORKFLOWS[name]
        try:
            result = asyncio.run(fn())
        except Exception:
            result = "CRASH:\n" + traceback.format_exc()
        if result == "ok":
            print(f"  PASS  {name}")
        else:
            failures += 1
            print(f"  FAIL  {name:<{width}}  {result}")
    print()
    if failures:
        print(f"{failures} workflow(s) failed.")
        return 1
    print(f"All {len(names)} workflow(s) passed.")
    return 0


if __name__ == "__main__":
    # Suppress Textual's snapshot-test-mode "task was destroyed" warnings
    # that get noisy under run_test teardown.
    with suppress(Exception):
        raise SystemExit(main())
