"""Guard tests for PreviewPresenter.reveal (PR #22 review #2/#3).

A finalize/reveal callback is queued a tick late; if a newer navigation
superseded the mount, revealing the captured (now stale) container would
surface the wrong file and clobber the new nav's outgoing reference. The
guard makes a superseded reveal a no-op."""

from __future__ import annotations

from tests._preview_fakes import FakeContainer as _FakeContainer

# ── _reveal_preview staleness guard (PR #22 review #2/#3) ──


class _RevealHost:
    """Minimal stand-in exposing only what reveal() touches."""

    def __init__(self, active: object, outgoing: object) -> None:
        self.active = active
        self.outgoing = outgoing

    def _cancel_reveal_watchdog(self) -> None:  # reveal disarms the watchdog
        pass


def _reveal(host: object, container: object) -> None:
    # Call PreviewPresenter.reveal as an unbound method against the stub
    # host, so we exercise the real guard logic without the full app.
    from fnd.tui.preview.presenter import PreviewPresenter

    PreviewPresenter.reveal(host, container)  # type: ignore[arg-type]


def test_reveal_preview_reveals_active_container_and_drops_outgoing() -> None:
    new = _FakeContainer()
    new.add_class("-pre-reveal")
    old = _FakeContainer()
    host = _RevealHost(active=new, outgoing=old)
    _reveal(host, new)
    assert "-pre-reveal" not in new.classes  # revealed
    assert "-hidden" in old.classes  # outgoing hidden
    assert host.outgoing is None


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
    assert host.outgoing is new_outgoing  # new nav's outgoing intact
    assert "-hidden" not in new_outgoing.classes
