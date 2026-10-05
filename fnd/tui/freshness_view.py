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
MARKER_COLOUR = "#e0af68"
MARKER_STYLE = Style(color=MARKER_COLOUR, dim=False)
#: The state badge on a Settings row, in the marker's colour.
BADGE_STYLE = f"bold {MARKER_COLOUR}"


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
    if verdict.state is State.CURRENT:
        return full, compact
    return f"{full} · {MARKER} {verdict.state.value}", f"{compact} {MARKER}"


def badge(app: FNDApp, name: str) -> str:
    """The state a collection's Update row shows beside its button, or ""."""
    verdict = verdict_for(app, name)
    return "" if verdict.state is State.CURRENT else verdict.label


def announce_saved(app: FNDApp, names: Sequence[str]) -> None:
    """One toast per save, naming what it left out of date and the key that fixes it."""
    behind = [(n, v) for n in names if (v := verdict_for(app, n)).state is not State.CURRENT]
    if not behind:
        app.notify("Saved.")
        return
    if len(behind) == 1:
        name, verdict = behind[0]
        app.notify(f"Saved. {_sentence(name, verdict)}", timeout=8)
        return
    if all(v.state is State.NEEDS_UPDATE for _n, v in behind):
        app.notify(
            f"Saved. {len(behind)} collections are outdated: "
            "Settings › Collections › Update all collections brings them up to date.",
            timeout=8,
        )
        return
    # Update all leaves tags and unindexed collections as they are, so each is named.
    phrases = [_STATE_PHRASE[v.state].format(name=repr(n)) for n, v in behind]
    joined = f"{', '.join(phrases[:-1])} and {phrases[-1]}"
    app.notify(
        f"Saved. {joined}: press u on each in Collections to bring it up to date.", timeout=8
    )


_STATE_PHRASE = {
    State.NEEDS_UPDATE: "{name} is outdated",
    State.NEEDS_REBUILD: "{name} has outdated tags",
    State.NOT_INDEXED: "{name} is not indexed",
}


def _sentence(name: str, verdict: Verdict) -> str:
    where = "press u on it in Collections"
    if verdict.state is State.NOT_INDEXED:
        return f"{name!r} is not indexed yet: {where} to index it."
    if verdict.state is State.NEEDS_REBUILD:
        return f"{name!r} has outdated tags: {verdict.because}; {where} to rebuild it."
    return f"{name!r} is outdated: {verdict.because}; {where} to update it."


def run_pending(app: FNDApp, name: str) -> None:
    """Run what the collection needs: an Update, or a confirmed re-read of every file."""
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
                f"{name!r} has outdated tags: {verdict.because}. Tags are read when a file "
                "is indexed, and an Update skips unchanged files, so every file is read again.\n\n"
                "PDF textures are reused from the cache. The files on disk are untouched."
            ),
            on_confirm=lambda: app._indexer.reindex_with_warning(name, rebuild=True),  # type: ignore[attr-defined]
        )
    )
