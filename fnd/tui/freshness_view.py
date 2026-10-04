"""The TUI side of index freshness: the marker, the save toast, and `u`.

The verdicts come from :mod:`fnd.index_freshness`; this module only words them
and runs the remedy, so the sidebar, the Settings pages and the toast agree.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from rich.style import Style

from fnd.index_freshness import State, Verdict

if TYPE_CHECKING:
    from fnd.tui.app import FNDApp

#: One cell, and used nowhere else in the app; `⚠` already means a missing source.
MARKER = "↻"
#: The theme's warning colour, undimmed so it survives the dimmed parent row.
MARKER_STYLE = Style(color="#e0af68", dim=False)


def verdict_for(app: FNDApp, name: str) -> Verdict:
    cfg = getattr(app, "_config", None)
    ledger = getattr(app, "_ledger", None)
    if cfg is None or ledger is None or name not in cfg.collections:
        return Verdict(State.CURRENT)
    return ledger.verdict(name, cfg.collections[name], cfg.defaults)


def sidebar_value(verdict: Verdict, n_sources: int) -> tuple[str, str]:
    """A collection row's value at full and compact width."""
    full = f"{n_sources} source{'s' if n_sources != 1 else ''}"
    compact = f"{n_sources} src"
    if verdict.state is State.NOT_INDEXED:
        return "not indexed", "new"
    if verdict.state is State.NEEDS_UPDATE:
        return f"{full} · {MARKER} update", f"{compact} {MARKER}"
    if verdict.state is State.NEEDS_REBUILD:
        return f"{full} · {MARKER} rebuild", f"{compact} {MARKER}"
    return full, compact


def announce_saved(app: FNDApp, names: Sequence[str]) -> None:
    """One toast per save, naming what it left out of date and the key that fixes it."""
    behind = [(n, v) for n in names if (v := verdict_for(app, n)).state is not State.CURRENT]
    if not behind:
        app.notify("Saved.")
        return
    if len(behind) == 1:
        name, verdict = behind[0]
        app.notify(
            f"Saved. {name!r} {verdict.summary}: press u on it in Collections to run it.",
            timeout=8,
        )
        return
    app.notify(
        f"Saved. {len(behind)} collections are out of date: "
        "Settings › Collections › Update all collections brings them up to date.",
        timeout=8,
    )


def run_pending(app: FNDApp, name: str) -> None:
    """Run what the collection needs: an Update, or a confirmed re-read of every file."""
    if any(w.group.startswith("rename-") and not w.is_finished for w in app.workers):
        # The index takes one writer, and a rename is still dropping the old name with it.
        app.notify("The old name's documents are still being dropped; try again in a moment.")
        return
    verdict = verdict_for(app, name)
    if verdict.state is not State.NEEDS_REBUILD:
        app._indexer.reindex_with_warning(name)  # type: ignore[attr-defined]
        return
    from fnd.tui.settings_screen import RebuildConfirmScreen

    app.push_screen(
        RebuildConfirmScreen(
            collection_name=name,
            crumb="Rebuild",
            body=(
                f"{name!r} {verdict.summary}. Those are read when a file is indexed, "
                "and an Update skips unchanged files, so every file is read again.\n\n"
                "PDF textures are reused from the cache. The files on disk are untouched."
            ),
            on_confirm=lambda: app._indexer.reindex_with_warning(name, rebuild=True),  # type: ignore[attr-defined]
        )
    )
