"""Regression: the active preview container must never stay ``-pre-reveal``
(invisible) once idle.

Symptom (data-reproduced under rapid navigation): selecting a result sometimes
left its preview blank until the user navigated to a different result and back.
Root cause: reveal is driven by one specific finalize task that rapid navigation
can cancel before it reveals, or hang for seconds awaiting above-window chunks a
cancelled mount never mounted — either leaves the active container invisible; and
the scroll-only resume path (``_settled_instant_scroll``) never revealed at all.

The fix enforces the invariant "an active container becomes visible within a
bounded time" via ``reveal_active`` at the reveal seams plus a bounded-time
watchdog. These tests pin each piece.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.containers import VerticalScroll

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.widgets.preview_container import PreviewContainer
from tests._pilot_wait import safe_pause
from tests._preview_fakes import FakeContainer as _FakeContainer


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


# ── reveal_active invariant helper (pure, no app) ──────────────────────────


class _Host:
    """Minimal stand-in exposing only what reveal_active / reveal touch."""

    def __init__(self, active: object, outgoing: object = None) -> None:
        self.active = active
        self.outgoing = outgoing
        self.inflight_target: object = ("stale", 0)

    def reveal(self, container: object) -> None:
        from fnd.tui.preview.presenter import PreviewPresenter

        PreviewPresenter.reveal(self, container)  # type: ignore[arg-type]

    def hide_document_view(self) -> None:
        # Modelled, not stubbed away: reveal() drops the served document here,
        # because a document is the OUTGOING substrate for a structural build
        # and hiding it any earlier blanks the pane for the whole build.
        self.hid_document = True

    def diag_log(self, msg: str) -> None:
        # Modelled, not stubbed away: the real presenter logs the first-paint
        # event from reveal(), and a stand-in that lacks it would hide that.
        pass

    def _cancel_reveal_watchdog(self) -> None:
        pass

    def hide_progress_bar(self) -> None:
        self.bar_hidden = True


def _reveal_active(host: object) -> None:
    from fnd.tui.preview.presenter import PreviewPresenter

    PreviewPresenter.reveal_active(host)  # type: ignore[arg-type]


def test_reveal_active_reveals_pre_reveal_active_container() -> None:
    c = _FakeContainer()
    c.add_class("-pre-reveal")
    host = _Host(active=c)
    _reveal_active(host)
    assert not c.has_class("-pre-reveal"), (
        "reveal_active must lift -pre-reveal off the active container"
    )
    # Rescuing a cut-short mount must also finish the finalize's terminal cleanup:
    # hide the progress bar (else 'mount stuck at 49%') and clear the in-flight
    # latch (else a re-select of the same result dedups out).
    assert getattr(host, "bar_hidden", False), "reveal_active must hide the stuck progress bar"
    assert host.inflight_target is None, "reveal_active must clear the in-flight latch"


def test_reveal_active_cleans_up_bar_and_latch_when_already_visible() -> None:
    # Branch-A settle: the container is already visible, but a superseded mount
    # may have left the shared bar open / latch set — reveal_active still clears.
    c = _FakeContainer()  # no -pre-reveal
    host = _Host(active=c)
    _reveal_active(host)
    assert not c.has_class("-pre-reveal")  # unchanged, and no crash
    assert getattr(host, "bar_hidden", False), "must hide a stale bar on a Branch-A settle"
    assert host.inflight_target is None


def test_reveal_active_is_noop_with_no_active_container() -> None:
    host = _Host(active=None)
    _reveal_active(host)  # must not raise
    # Nothing active → nothing to clean up (the latch is left untouched).
    assert host.inflight_target == ("stale", 0)


# ── bounded-time watchdog (end-to-end) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_watchdog_reveals_stranded_active_container(
    built_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container activated invisibly with NO finalize to reveal it (the
    cut-short / hung-finalize case) must be revealed by the bounded-time
    watchdog — otherwise it stays invisible forever ('blank until I pick a
    different result')."""
    from fnd.tui.preview import tuning

    monkeypatch.setattr(tuning, "REVEAL_WATCHDOG_MS", 80)  # keep the test fast

    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._search.groups, "setup — query produced no results"

        preview = app._preview
        g = app._search.groups[0]
        chunks = app._search.searcher.get_file_chunks(g.parent_id)  # type: ignore[union-attr]
        container = PreviewContainer(
            parent_doc_id=g.parent_id,
            query_signature=app._search.query_signature(),
            total_chunks=len(chunks),
        )
        pane = app.query_one("#preview_pane", VerticalScroll)
        await pane.mount(container)

        # Activate invisibly exactly as a cold mount does, but never spawn a
        # finalize task — the strand condition (finalize cancelled/hung).
        preview.activate_container(container, pre_reveal=True)
        assert container.has_class("-pre-reveal"), "setup — container should start invisible"
        assert preview.active is container

        # Nothing else will reveal it. Within REVEAL_WATCHDOG_MS the watchdog must.
        await asyncio.sleep(0.2)
        await safe_pause(pilot)
        assert not container.has_class("-pre-reveal"), (
            "BUG: an active -pre-reveal container was never revealed — the "
            "reveal watchdog did not fire (preview stays blank until re-nav)"
        )


@pytest.mark.asyncio
async def test_watchdog_rearms_and_does_not_reveal_during_active_nav(
    built_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-arming: each pre-reveal activation resets the timer, so the watchdog
    only fires once the user STOPS on a still-invisible container — it never
    interrupts an in-flight navigation by revealing a container prematurely."""
    from fnd.tui.preview import tuning

    monkeypatch.setattr(tuning, "REVEAL_WATCHDOG_MS", 120)

    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        preview = app._preview
        g = app._search.groups[0]
        chunks = app._search.searcher.get_file_chunks(g.parent_id)  # type: ignore[union-attr]
        pane = app.query_one("#preview_pane", VerticalScroll)

        first = PreviewContainer(
            parent_doc_id=g.parent_id,
            query_signature=app._search.query_signature(),
            total_chunks=len(chunks),
        )
        await pane.mount(first)
        preview.activate_container(first, pre_reveal=True)
        first_timer = preview._reveal_watchdog
        assert first_timer is not None, "activation must arm the watchdog"
        # Before the first watchdog would fire, a new nav activates a second
        # container — this must re-arm the timer onto the new active one.
        await asyncio.sleep(0.05)
        second = PreviewContainer(
            parent_doc_id=g.parent_id,
            query_signature=app._search.query_signature(),
            total_chunks=len(chunks),
        )
        await pane.mount(second)
        preview.activate_container(second, pre_reveal=True)
        # Prove the timer was actually re-armed (a new timer object), not left as
        # the stale first one — otherwise this test would pass even if re-arming
        # were broken, since some other path could still reveal `second`.
        assert preview._reveal_watchdog is not None
        assert preview._reveal_watchdog is not first_timer, (
            "activating a new container must cancel the old watchdog and arm a new one"
        )
        # The second container is now the active one; the watchdog fires for IT.
        await asyncio.sleep(0.2)
        await safe_pause(pilot)
        assert not second.has_class("-pre-reveal"), (
            "watchdog must reveal the latest active container"
        )
