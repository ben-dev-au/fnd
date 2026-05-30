"""Guard tests for FNDApp._reveal_preview (PR #22 review #2/#3).

A finalize/reveal callback is queued a tick late; if a newer navigation
superseded the mount, revealing the captured (now stale) container would
surface the wrong file and clobber the new nav's outgoing reference. The
guard makes a superseded reveal a no-op."""

from __future__ import annotations

# ── _reveal_preview staleness guard (PR #22 review #2/#3) ──


class _FakeContainer:
    def __init__(self) -> None:
        self.classes: set[str] = set()

    def add_class(self, name: str) -> None:
        self.classes.add(name)

    def remove_class(self, name: str) -> None:
        self.classes.discard(name)


class _RevealHost:
    """Minimal stand-in exposing only what _reveal_preview touches."""

    def __init__(self, active: object, outgoing: object) -> None:
        self._active_preview = active
        self._outgoing_preview = outgoing


def _reveal(host: object, container: object) -> None:
    # Call FNDApp._reveal_preview as an unbound method against the stub host,
    # so we exercise the real guard logic without constructing the full app.
    from fnd.tui.app import FNDApp

    FNDApp._reveal_preview(host, container)  # type: ignore[arg-type]


def test_reveal_preview_reveals_active_container_and_drops_outgoing() -> None:
    new = _FakeContainer()
    new.add_class("-pre-reveal")
    old = _FakeContainer()
    host = _RevealHost(active=new, outgoing=old)
    _reveal(host, new)
    assert "-pre-reveal" not in new.classes  # revealed
    assert "-hidden" in old.classes  # outgoing hidden
    assert host._outgoing_preview is None


def test_reveal_preview_is_noop_for_superseded_container() -> None:
    # A finalize callback fires a tick late after a newer nav swapped in
    # ``current``. Revealing the stale ``superseded`` would surface the wrong
    # file and clobber the new nav's outgoing reference — must be a no-op.
    superseded = _FakeContainer()
    superseded.add_class("-pre-reveal")
    current = _FakeContainer()
    new_outgoing = _FakeContainer()
    host = _RevealHost(active=current, outgoing=new_outgoing)
    _reveal(host, superseded)
    assert "-pre-reveal" in superseded.classes  # stale container NOT revealed
    assert host._outgoing_preview is new_outgoing  # new nav's outgoing intact
    assert "-hidden" not in new_outgoing.classes
