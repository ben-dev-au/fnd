"""Cloud-only files are fetched, reported, and skipped only when blocked.

The policy, from the 7-minute Update-all stall: a file the user can see in
their file manager should end up in the index, so the indexer pulls it down
rather than refusing it. What must never happen again is the *silent* wait —
so every fetch is announced through :func:`fnd.cloud_files.current_wait`,
bounded by ``defaults.cloud_fetch_timeout_s``, and abandonable mid-run via
the modal's "Skip cloud-only" action.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from fnd import cloud_files
from fnd.config import CollectionConfig, SourceConfig
from fnd.index_runner import CloudPolicy, run_indexer


@pytest.fixture(autouse=True)
def _clear_wait() -> Any:  # pyright: ignore[reportUnusedFunction]
    """The in-flight fetch record is module-global; clear it either side
    so a timed-out fetch in one test can't be read as another's."""
    cloud_files.reset_wait()
    yield
    cloud_files.reset_wait()


def test_fetch_publishes_what_it_is_waiting_on(tmp_path: Path) -> None:
    """The whole point: while a fetch is still blocked, another thread can
    ask what is being waited on and for how long."""
    target = tmp_path / "notes.md"
    target.write_text("x", encoding="utf-8")

    entered = threading.Event()
    release = threading.Event()
    observed: list[cloud_files.FetchWait | None] = []

    def _blocked() -> str:
        entered.set()
        # Hold the fetch open until the observation below has happened, so
        # the assertion is about a genuinely in-flight fetch rather than one
        # that already returned.
        release.wait(5.0)
        return "done"

    watcher = threading.Thread(
        target=lambda: (
            entered.wait(5.0),
            observed.append(cloud_files.current_wait()),
            release.set(),
        )
    )
    watcher.start()
    try:
        assert cloud_files.fetch(_blocked, path=target, timeout_s=5.0) == "done"
    finally:
        release.set()
        watcher.join(5.0)

    wait = observed[0]
    assert wait is not None, "no record published while the fetch was blocked"
    assert wait.path == str(target)
    assert wait.provider  # a label, whatever the platform decided
    assert cloud_files.current_wait() is None, "wait record outlived the fetch"


def test_fetch_that_never_arrives_raises(tmp_path: Path) -> None:
    target = tmp_path / "big.pdf"
    target.write_bytes(b"x")

    def _never() -> str:
        time.sleep(5.0)
        return "never"

    with pytest.raises(cloud_files.CloudFetchError):
        cloud_files.fetch(_never, path=target, timeout_s=0.15)
    assert cloud_files.current_wait() is None


def test_policy_default_fetches_and_opt_out_skips(tmp_path: Path) -> None:
    policy = CloudPolicy()
    assert policy.skipping() is False

    flag = asyncio.Event()
    opted_out = CloudPolicy(skip=flag)
    assert opted_out.skipping() is False
    flag.set()
    assert opted_out.skipping() is True
    assert "skipped for this run" in opted_out.skip_reason(tmp_path / "a.md")


@pytest.mark.asyncio
async def test_cloud_only_file_is_fetched_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A placeholder is materialised and then indexed — the old behaviour
    refused it outright with "download in Finder before indexing"."""
    doc = tmp_path / "cloud.md"
    doc.write_text("# heading\n\nbody text\n", encoding="utf-8")

    monkeypatch.setattr("fnd.index_runner.is_placeholder", lambda _p: True)

    errors: list[str] = []
    done = None
    async for ev in run_indexer(
        config=CollectionConfig(sources=[SourceConfig(path=tmp_path)]),
        collection="cloudy",
        index_dir=tmp_path / "idx",
        state_path=tmp_path / "state.toml",
    ):
        if ev.kind == "file_error":
            errors.append(ev.error)
        if ev.kind == "done":
            done = ev

    assert errors == [], errors
    assert done is not None
    assert done.indexed_newly_total == 1


@pytest.mark.asyncio
async def test_opt_out_skips_cloud_only_files_mid_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the opt-out set, the placeholder is reported as skipped rather
    than fetched — and the reason names the provider."""
    doc = tmp_path / "cloud.md"
    doc.write_text("# heading\n\nbody\n", encoding="utf-8")
    monkeypatch.setattr("fnd.index_runner.is_placeholder", lambda _p: True)

    skip = asyncio.Event()
    skip.set()

    errors: list[str] = []
    async for ev in run_indexer(
        config=CollectionConfig(sources=[SourceConfig(path=tmp_path)]),
        collection="cloudy",
        index_dir=tmp_path / "idx",
        state_path=tmp_path / "state.toml",
        skip_cloud=skip,
    ):
        if ev.kind == "file_error":
            errors.append(ev.error)

    assert len(errors) == 1
    assert "skipped for this run" in errors[0]


@pytest.mark.asyncio
async def test_blocked_fetch_is_reported_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fetch that never completes fails that one file with a reason the
    user can act on, and the rest of the run continues."""
    (tmp_path / "cloud.md").write_text("# a\n\nbody\n", encoding="utf-8")
    (tmp_path / "local.md").write_text("# b\n\nbody\n", encoding="utf-8")

    monkeypatch.setattr("fnd.index_runner.is_placeholder", lambda p: Path(p).name == "cloud.md")
    monkeypatch.setattr(
        "fnd.index_runner.CloudPolicy.materialise",
        lambda _self, path: (_ for _ in ()).throw(
            cloud_files.CloudFetchError("did not deliver the file within 60s")
        ),
    )

    errors: list[str] = []
    done = None
    async for ev in run_indexer(
        config=CollectionConfig(sources=[SourceConfig(path=tmp_path)]),
        collection="cloudy",
        index_dir=tmp_path / "idx",
        state_path=tmp_path / "state.toml",
    ):
        if ev.kind == "file_error":
            errors.append(ev.error)
        if ev.kind == "done":
            done = ev

    assert len(errors) == 1
    assert "Could not fetch" in errors[0]
    assert done is not None
    assert done.indexed_newly_total == 1, "the local file should still be indexed"


@pytest.mark.asyncio
async def test_opt_out_does_not_flag_already_indexed_cloud_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unchanged, already-indexed cloud-only file needs no work either
    way — with the opt-out on it must still read as "already indexed", not
    as something the user was denied."""
    # Source root holds only the document: the state file and index must
    # live outside it, or the second run enumerates them as corpus files.
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "cloud.md").write_text("# heading\n\nbody\n", encoding="utf-8")
    idx = tmp_path / "idx"

    async def _run(skip: asyncio.Event | None) -> Any:
        last = None
        async for ev in run_indexer(
            config=CollectionConfig(sources=[SourceConfig(path=corpus)]),
            collection="cloudy",
            index_dir=idx,
            state_path=tmp_path / "state.toml",
            skip_cloud=skip,
        ):
            if ev.kind == "done":
                last = ev
        return last

    monkeypatch.setattr("fnd.index_runner.is_placeholder", lambda _p: False)
    first = await _run(None)
    assert first is not None
    assert first.indexed_newly_total == 1

    # Now the same untouched file reads as cloud-only, with the opt-out on.
    monkeypatch.setattr("fnd.index_runner.is_placeholder", lambda _p: True)
    skip = asyncio.Event()
    skip.set()
    second = await _run(skip)
    assert second is not None
    assert second.failed_total == 0, "unchanged file flagged despite needing no work"
    assert second.indexed_already_total == 1


@pytest.mark.asyncio
async def test_scan_phase_blocks_are_counted_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate the *scan* could not resolve must still be reported.

    Frontmatter-filter candidates are dropped from the walk when the fetch
    is declined or fails, so they never reach the per-file loop. Without an
    explicit flush they vanish silently — no count, no event, no log entry —
    which is the exact failure mode this change exists to prevent.
    """
    corpus = tmp_path / "vault"
    corpus.mkdir()
    for name in ("a.md", "b.md"):
        (corpus / name).write_text("---\nCourse: '[[CPL]]'\n---\n\nbody\n", encoding="utf-8")

    monkeypatch.setattr("fnd.index_runner.is_placeholder", lambda _p: True)
    skip = asyncio.Event()
    skip.set()

    source = SourceConfig(
        path=corpus, includes=["**/*.md"], frontmatter_filter="Course == '[[CPL]]'"
    )
    errors: list[str] = []
    done = None
    async for ev in run_indexer(
        config=CollectionConfig(sources=[source]),
        collection="cloudy",
        index_dir=tmp_path / "idx",
        state_path=tmp_path / "state.toml",
        skip_cloud=skip,
    ):
        if ev.kind == "file_error":
            errors.append(ev.error)
        if ev.kind == "done":
            done = ev

    assert len(errors) == 2, f"scan-blocked files went unreported: {errors}"
    assert all("skipped for this run" in e for e in errors)
    assert done is not None
    assert done.failed_total == 2, "scan-blocked files not counted as failures"


def test_fetch_aborts_promptly_when_the_run_is_cancelled(tmp_path: Path) -> None:
    """A stalled provider must not hold Cancel for the whole timeout.

    Without polling, `fetch` joined for `timeout_s` (60s by default), so a
    Cancel pressed while a download was wedged sat unanswered for a minute —
    undoing the responsiveness this change set exists to deliver.
    """
    target = tmp_path / "wedged.pdf"
    target.write_bytes(b"x")
    cancel = threading.Event()
    entered = threading.Event()

    def _never() -> str:
        entered.set()
        time.sleep(30.0)
        return "never"

    threading.Thread(target=lambda: (entered.wait(5.0), cancel.set())).start()
    started = time.monotonic()
    with pytest.raises(cloud_files.CloudFetchCancelledError):
        cloud_files.fetch(_never, path=target, timeout_s=30.0, cancel=cancel)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"cancel took {elapsed:.1f}s — not polled during the join"
    assert cloud_files.current_wait() is None


@pytest.mark.asyncio
async def test_cancel_during_a_file_fetch_ends_the_run_without_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled fetch is not a blocked file: the run ends on `cancelled`
    and the file is not recorded as failed."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "cloud.md").write_text("# a\n\nbody\n", encoding="utf-8")

    monkeypatch.setattr("fnd.index_runner.is_placeholder", lambda _p: True)
    monkeypatch.setattr(
        "fnd.index_runner.CloudPolicy.materialise",
        lambda _self, path: (_ for _ in ()).throw(
            cloud_files.CloudFetchCancelledError("cancelled while fetching")
        ),
    )

    kinds: list[str] = []
    errors: list[str] = []
    async for ev in run_indexer(
        config=CollectionConfig(sources=[SourceConfig(path=corpus)]),
        collection="cloudy",
        index_dir=tmp_path / "idx",
        state_path=tmp_path / "state.toml",
        cancel=asyncio.Event(),
    ):
        kinds.append(ev.kind)
        if ev.kind == "file_error":
            errors.append(ev.error)

    assert kinds[-1] == "cancelled", kinds
    assert errors == [], f"a cancelled fetch was misfiled as a failure: {errors}"
