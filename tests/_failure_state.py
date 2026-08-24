"""What the app looked like when a wait gave up.

A timing failure reaches CI as one line — ``wait_until timed out after 20.0s:
<message>`` — which names the wait but not the clause still false, nor whether
the poller ran 3 times or 900. This module is the snapshot appended to it: the
preview pipeline's state plus the ``_diag_log`` breadcrumbs the app already
emits, teed here per-test by an autouse fixture in ``conftest``.

Every read goes through :func:`_fields`, so a half-built widget tree yields
``<AttributeError>`` for one field rather than replacing the real failure with
a snapshot of its own.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from typing import Any

# Breadcrumbs from ``FNDApp._diag_log``, teed here by the ``conftest`` capture
# fixture. Bounded: a navigation-heavy test emits hundreds and only the tail
# says anything about where it stopped.
_DIAG: deque[str] = deque(maxlen=40)

_Field = tuple[str, Callable[[], object]]


def record_diag(message: str) -> None:
    _DIAG.append(message)


def reset_diag() -> None:
    _DIAG.clear()


def _read(fn: Callable[[], object]) -> str:
    """``None`` is an answer here, so only a raise reads as unavailable."""
    try:
        return str(fn())
    except Exception as exc:
        return f"<{type(exc).__name__}>"


def _fields(prefix: str, fields: Sequence[_Field]) -> str:
    parts = [f"{name}={_read(fn)}" for name, fn in fields]
    return " ".join([prefix, *parts])


def _short(parent_id: object) -> str:
    text = str(parent_id)
    return text[:8] if len(text) > 8 else text


def _task_state(task: Any) -> str:
    if task is None:
        return "None"
    if getattr(task, "done", None) is not None and task.done():
        return "done"
    cancelling = getattr(task, "cancelling", None)
    if callable(cancelling) and cancelling():
        return "cancelling"
    return "pending"


def _container_state(active: Any) -> str:
    if active is None:
        return "None"
    return _fields(
        _short(getattr(active, "parent_doc_id", "?")),
        [
            ("mounted", lambda: f"{len(active.mounted_indices)}/{active.total_chunks}"),
            ("complete", lambda: active.is_complete),
            ("pre_reveal", lambda: active.has_class("-pre-reveal")),
        ],
    )


def _preview_state(app: Any) -> str:
    preview = getattr(app, "_preview", None)
    if preview is None:
        return "preview=<absent>"
    return _fields(
        "preview",
        [
            ("active", lambda: _container_state(preview.active)),
            ("inflight", lambda: preview.inflight_target),
            ("mount_task", lambda: _task_state(preview.mount_task)),
            ("decode_worker", lambda: preview.decode_worker),
            ("chunk_cache", lambda: sorted(_short(k) for k in preview.chunk_cache)),
        ],
    )


def _scroll_state(app: Any) -> str:
    ctrl = getattr(app, "_preview_scroll", None)
    if ctrl is None:
        return "scroll=<absent>"
    return _fields(
        "scroll",
        [
            ("armed", lambda: ctrl.is_armed),
            ("settling", lambda: ctrl.is_settling),
            ("restoring", lambda: ctrl.is_restoring),
            ("restores_completed", lambda: ctrl.restores_completed),
            ("anchor", lambda: ctrl.anchor),
        ],
    )


def _pane_state(app: Any) -> str:
    def pane() -> Any:
        return app.query_one("#preview_pane")

    return _fields(
        "pane",
        [
            ("y", lambda: pane().scroll_offset.y),
            ("vsize_h", lambda: pane().virtual_size.height),
            ("region", lambda: pane().region),
        ],
    )


def _viewport(pane: Any) -> str:
    top = pane.scroll_offset.y
    return f"[{top},{top + pane.scrollable_content_region.height})"


def _match_nav_state(app: Any) -> str:
    nav = getattr(app, "_match_nav", None)
    if nav is None:
        return "match_nav=<absent>"
    return _fields(
        "match_nav",
        [
            ("count", lambda: nav.count),
            ("above", lambda: nav.above),
            ("below", lambda: nav.below),
            ("position", lambda: nav.position),
            ("measure_pending", lambda: nav._measure_pending),
            # The rows themselves, not just how many. above/below is derived
            # from these against the viewport, so a disagreement between where
            # the scroll landed and where the stops are is only readable here.
            ("chunk_stops", lambda: nav._chunk_stops(nav._pane())),
            ("all_stops", lambda: nav._region_stops(nav._pane())),
            ("chunk_extent", lambda: nav._current_chunk_extent(nav._pane())),
            ("viewport", lambda: _viewport(nav._pane())),
        ],
    )


def _search_state(app: Any) -> str:
    search = getattr(app, "_search", None)
    if search is None:
        return "search=<absent>"
    return _fields(
        "search",
        [
            ("idle", lambda: search.idle),
            ("groups", lambda: len(search.groups)),
            ("signature", lambda: search.query_signature()),
        ],
    )


def _worker_state(app: Any) -> str:
    return _fields(
        "workers",
        [("live", lambda: sorted({f"{w.group}:{w.state.name}" for w in app.workers}))],
    )


def describe(pilot: Any) -> str:
    """A multi-line snapshot to append to a wait's failure message."""
    app = getattr(pilot, "app", None)
    if app is None:
        return "  app=<unavailable>"
    lines = [
        _preview_state(app),
        _scroll_state(app),
        _pane_state(app),
        _match_nav_state(app),
        _search_state(app),
        _worker_state(app),
    ]
    if _DIAG:
        lines.append("recent diag:")
        lines.extend(f"  {line}" for line in _DIAG)
    return "\n".join(f"  {line}" for line in lines)
