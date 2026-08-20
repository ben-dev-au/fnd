"""What the progress trackers read from the rest of the app.

The trackers **observe** their subsystems rather than being called from
inside them, which is what stops a stale teardown retiring someone else's
bar (see ``fnd/tui/progress/operations.py``). The price of that choice is
a set of implicit dependencies: the tracker reads attributes it does not
own, and renaming or removing one breaks the line **silently** — the
sampler's catch-all treats the failure as "not busy", so the bar stops
reflecting that stage rather than raising.

These tests are the tripwire, and they run against a live app because
every one of these is an instance attribute: nothing here is visible on
the class. They are deliberately shallow — existence, not behaviour.
Behaviour is covered by ``test_progress_preview_tracker`` and the
real-app tests; what nothing else catches is a signal quietly going away.

**If one of these fails after a preview-architecture change**, the fix is
not to delete the assertion. Find the replacement signal, update
``PreviewProgressTracker``, and then re-run
``dev/tools/progress_phase_reachability.py`` — a phase that can no longer
be entered still owns its share of the bar, which caps the fill with
nothing to show for it. That defect class has shipped four times.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.preview import tuning
from fnd.tui.progress.facility import ProgressTracker
from fnd.tui.progress.operations import IndexProgressTracker, PreviewProgressTracker
from tests._pilot_wait import run_search, wait_until

# Read by PreviewProgressTracker.sample or .plan_for. The comment is what it
# is used for, so a replacement can be chosen rather than guessed at.
PRESENTER_SIGNALS = {
    "pipeline_busy": "anything in flight at all — the session's lifetime",
    "showing_parent": "which file the pane is on — warm vs cold, and landing",
    "decode_worker": "the decode phase",
    "mount_task": "the mount phase",
    "active": "the container carrying mounted_indices / _finalise_task",
    "chunk_cache": "warm vs cold: is there a decode to do?",
    "decode_token": "generation guard for the flat renderer's line counts",
}

# Read off ``presenter.active``. The mount fraction is measured against the
# WINDOW, not the file — dividing by the whole file is why the old bar topped
# out near 1% on a large PDF.
CONTAINER_SIGNALS = {
    "mounted_indices": "how much of the mount window has landed",
    "total_chunks": "clamps the window against a short file",
    "_finalise_task": "the build phase",
}

# The mount window's size, and the bound on how long a reveal may take.
TUNING_SIGNALS = ("VISIBLE_FIRST_ABOVE", "VISIBLE_FIRST_BELOW", "REVEAL_WATCHDOG_MS")


@pytest.fixture
def contract_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "note.md").write_text(
        "\n".join(f"## S{i}\n\ntarget paragraph {i} with enough words\n" for i in range(30)),
        encoding="utf-8",
    )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_the_preview_still_exposes_every_signal_the_line_reads(
    contract_index: Path,
) -> None:
    app = FNDApp(index_dir=contract_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        group = app._search.groups[0]
        app._preview.render_full_doc(group.parent_id, focus_chunk_seq=0)
        await wait_until(pilot, lambda: app._progress.active is None, timeout=10.0)

        missing = [
            f"presenter.{name} ({why})"
            for name, why in PRESENTER_SIGNALS.items()
            if not hasattr(app._preview, name)
        ]
        assert not missing, f"the progress line reads these and they are gone: {missing}"

        assert hasattr(app._preview_scroll, "is_settling"), (
            "is_settling is the whole of the `land` phase — the measured "
            "reconcile-to-scroll-commit window is 440-740 ms"
        )

        container = app._preview.active
        assert container is not None, "setup — the navigation did not produce a container"
        gone = [
            f"container.{name} ({why})"
            for name, why in CONTAINER_SIGNALS.items()
            if not hasattr(container, name)
        ]
        assert not gone, f"the mount phase reads these and they are gone: {gone}"


@pytest.mark.parametrize("name", TUNING_SIGNALS)
def test_the_mount_window_tunables_still_exist(name: str) -> None:
    assert hasattr(tuning, name), f"the progress line reads tuning.{name}"


def test_both_trackers_satisfy_the_tracker_protocol() -> None:
    """The protocol is the seam a new subsystem implements. If a tracker
    drifts off it, the next one gets copied from a broken example."""
    assert issubclass(PreviewProgressTracker, ProgressTracker)
    assert issubclass(IndexProgressTracker, ProgressTracker)
