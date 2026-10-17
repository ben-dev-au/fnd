"""The embedding job embeds only what is missing, whole files at a time, and loses nothing."""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from fnd.embed_job import reconcile  # noqa: E402
from fnd.index import build_index  # noqa: E402
from fnd.vectors import RowTable, VectorStore  # noqa: E402
from tests._semantic_fakes import HEADER, FakeEmbedder  # noqa: E402

_LONG = " ".join(f"word{i}" for i in range(400))


def _write(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")


def _files(n: int) -> dict[str, str]:
    return {f"f{i}.md": f"# F{i}\n\n## A\n\napple {i}\n\n## B\n\nboat {i}\n" for i in range(n)}


@pytest.fixture
def index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    _write(tmp_path / "notes", _files(6))
    build_index(roots=[tmp_path / "notes"], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


def _store(tmp_path: Path) -> VectorStore:
    return VectorStore.open_writer(tmp_path / "vectors", dim=FakeEmbedder.dim, header=HEADER)


def _run(index: Path, store: VectorStore, fake: FakeEmbedder, **kw: object) -> RowTable:
    return reconcile(index, store=store, embedder=fake, opted_in=frozenset({"notes"}), **kw)  # type: ignore[arg-type]


def test_reconcile_embeds_every_section_once(index: Path, tmp_path: Path) -> None:
    """A first run embeds each section and records a row for it."""
    fake = FakeEmbedder()
    rows = _run(index, _store(tmp_path), fake)
    assert fake.embedded == len(rows.parent_id) > 0


def test_reconcile_embeds_only_what_is_missing(index: Path, tmp_path: Path) -> None:
    """A second reconcile with no index change embeds nothing."""
    store = _store(tmp_path)
    _run(index, store, FakeEmbedder())
    again = FakeEmbedder()
    _run(index, store, again)
    assert again.embedded == 0


def test_an_edit_re_embeds_only_its_section(index: Path, tmp_path: Path) -> None:
    """Changing one section's text embeds that one section."""
    store = _store(tmp_path)
    _run(index, store, FakeEmbedder())
    notes = tmp_path / "notes"
    (notes / "f0.md").write_text("# F0\n\n## A\n\napple 0\n\n## B\n\ncloud now\n", encoding="utf-8")
    build_index(roots=[notes], index_dir=index, collection="notes")
    again = FakeEmbedder()
    _run(index, store, again)
    assert again.embedded == 1


def test_an_edit_past_the_token_cut_re_embeds_nothing(tmp_path: Path, tmp_index_dir: Path) -> None:
    """A change after the first 256 tokens keeps the section's vector."""
    notes = tmp_path / "notes"
    _write(notes, {"long.md": f"# Long\n\n## A\n\n{_LONG} end\n"})
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    store = _store(tmp_path)
    _run(tmp_index_dir, store, FakeEmbedder())
    (notes / "long.md").write_text(f"# Long\n\n## A\n\n{_LONG} changed\n", encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    again = FakeEmbedder()
    _run(tmp_index_dir, store, again)
    assert again.embedded == 0


def test_whole_files_at_a_time(index: Path, tmp_path: Path) -> None:
    """A run cancelled after its first batch leaves every file fully embedded or not at all."""
    store = _store(tmp_path)
    calls = {"n": 0}

    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    rows = _run(index, store, FakeEmbedder(), batch=3, cancel=cancel)
    per_file: dict[str, int] = {}
    for pid in rows.parent_id.tolist():
        per_file[pid] = per_file.get(pid, 0) + 1
    assert per_file
    assert len(per_file) < 6
    assert set(per_file.values()) == {3}


def test_an_interrupted_run_resumes(index: Path, tmp_path: Path) -> None:
    """After cancellation, the next reconcile finishes the rest and re-embeds nothing done."""
    store = _store(tmp_path)
    first = FakeEmbedder()
    _run(index, store, first, batch=2, cancel=lambda: first.embedded >= 4)
    rest = FakeEmbedder()
    rows = _run(index, store, rest)
    assert rest.embedded > 0
    assert len(rows.parent_id) == 18
    assert first.embedded + rest.embedded == 18


def test_shared_sections_are_embedded_once(tmp_path: Path, tmp_index_dir: Path) -> None:
    """A file in two collections is embedded once, its rows members of both."""
    _write(tmp_path / "shared", {"same.md": "# Same\n\n## A\n\napple once\n"})
    for col in ("notes", "books"):
        build_index(roots=[tmp_path / "shared"], index_dir=tmp_index_dir, collection=col)
    fake = FakeEmbedder()
    rows = reconcile(
        tmp_index_dir, store=_store(tmp_path), embedder=fake, opted_in=frozenset({"notes", "books"})
    )
    assert fake.embedded == len(rows.parent_id)
    assert rows.member.all()


def test_identical_copies_are_embedded_once(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Two files with the same text share their vectors."""
    _write(
        tmp_path / "notes",
        {"a.md": "# Same\n\n## A\n\napple once\n", "b.md": "# Same\n\n## A\n\napple once\n"},
    )
    build_index(roots=[tmp_path / "notes"], index_dir=tmp_index_dir, collection="notes")
    fake = FakeEmbedder()
    rows = _run(tmp_index_dir, _store(tmp_path), fake)
    assert fake.embedded * 2 == len(rows.parent_id)


def test_no_collection_while_a_reindex_is_incomplete(
    index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a reindex unfinished, no vector is dropped however few sections remain."""
    import fnd.embed_job as job

    store = _store(tmp_path)
    _run(index, store, FakeEmbedder())
    before = len(store)
    for name in ("f1.md", "f2.md", "f3.md", "f4.md", "f5.md"):
        (tmp_path / "notes" / name).unlink()
    build_index(roots=[tmp_path / "notes"], index_dir=index, collection="notes")
    monkeypatch.setattr(job, "_reindex_incomplete", lambda: True)
    _run(index, store, FakeEmbedder())
    assert len(store) == before
    monkeypatch.setattr(job, "_reindex_incomplete", lambda: False)
    _run(index, store, FakeEmbedder())
    assert len(store) < before


def test_opted_out_collections_are_not_embedded(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Sections only in an opted-out collection get no vector."""
    _write(tmp_path / "notes", {"a.md": "# A\n\n## X\n\napple\n"})
    _write(tmp_path / "books", {"b.md": "# B\n\n## Y\n\nboat\n"})
    for col in ("notes", "books"):
        build_index(roots=[tmp_path / col], index_dir=tmp_index_dir, collection=col)
    fake = FakeEmbedder()
    rows = _run(tmp_index_dir, _store(tmp_path), fake)
    assert fake.embedded == len(rows.parent_id)
    assert rows.collections == ("notes",)
    assert rows.member.all()


def test_the_row_table_is_saved_while_running(index: Path, tmp_path: Path) -> None:
    """Rows for finished files are saved before the run ends, so search can use them."""
    store = _store(tmp_path)
    seen: list[RowTable | None] = []

    def cancel() -> bool:
        if len(store) >= 4:
            seen.append(RowTable.load(store.folder))
            return True
        return False

    _run(index, store, FakeEmbedder(), batch=2, cancel=cancel, save_every_s=0.0)
    assert seen[0] is not None
    assert len(seen[0].parent_id) >= 4


def test_the_subprocess_reports_progress_and_stops_cleanly(index: Path, tmp_path: Path) -> None:
    """The entry point streams progress and, sent SIGTERM, exits 3 after its batch."""
    if sys.platform == "win32":
        pytest.skip("SIGTERM handling is POSIX; Windows terminates the process")
    env = {**os.environ, "FND_FAKE_EMBED_SLOW_S": "0.3"}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "fnd.embed_job",
            str(index),
            str(tmp_path / "vectors"),
            "notes",
            "--embedder",
            "tests._semantic_fakes:FakeEmbedder",
            "--batch",
            "2",
        ],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert proc.stdout is not None
    first = json.loads(proc.stdout.readline())
    assert first["total"] == 18
    proc.send_signal(signal.SIGTERM)
    deadline = time.monotonic() + 20
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert proc.returncode == 3


def test_an_index_update_tokenises_only_changed_sections(index: Path, tmp_path: Path) -> None:
    """After one section changes, only that section is decoded and tokenised again."""
    store = _store(tmp_path)
    _run(index, store, FakeEmbedder())
    notes = tmp_path / "notes"
    (notes / "f0.md").write_text("# F0\n\n## A\n\napple 0\n\n## B\n\ncloud now\n", encoding="utf-8")
    build_index(roots=[notes], index_dir=index, collection="notes")
    again = FakeEmbedder()
    rows = _run(index, store, again)
    assert again.tokenised == 1
    assert len(rows.parent_id) == 18


def test_a_section_whose_stored_text_changed_is_tokenised_again(
    index: Path, tmp_path: Path
) -> None:
    """Key reuse follows the stored section, not the file's bytes."""
    from fnd import embed_job
    from fnd.embed_job import indexed_sections
    from fnd.query import Searcher

    store = _store(tmp_path)
    _run(index, store, FakeEmbedder())
    first = indexed_sections(Searcher(index_dir=index))[0]
    changed = dataclasses.replace(first, body_md=first.body_md + " re-textured")
    assert embed_job.stored_digest(changed) != embed_job.stored_digest(first)


def test_batches_share_segment_files(index: Path, tmp_path: Path) -> None:
    """Several batches go into one segment file, so a large library is not thousands of files."""
    store = _store(tmp_path)
    writes: list[int] = []
    real = store.append

    def append(keys: list[str], vecs: object) -> None:
        writes.append(len(keys))
        real(keys, vecs)  # type: ignore[arg-type]

    store.append = append  # type: ignore[method-assign]
    _run(index, store, FakeEmbedder(), batch=3, flush_every=4)
    assert len(writes) == 3
    assert len(list(store.folder.glob("seg-*.npy"))) == 1


def test_the_row_table_is_saved_on_a_timer_not_per_batch(
    index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long run saves its rows about once a minute, not after every few batches."""
    from fnd import vectors

    saves: list[int] = []
    real = vectors.RowTable.save
    monkeypatch.setattr(
        vectors.RowTable, "save", lambda self, folder: (saves.append(1), real(self, folder))[1]
    )
    ticks = iter(range(0, 10_000, 10))
    _run(
        index,
        _store(tmp_path),
        FakeEmbedder(),
        batch=1,
        save_every_s=60.0,
        clock=lambda: float(next(ticks)),
    )
    assert 2 <= len(saves) <= 4


def test_pausing_survives_a_removed_store(tmp_path: Path) -> None:
    """Pausing after an uninstall removed the store folder is a no-op, not a crash."""
    from fnd.embed_job import EmbedJobRunner

    runner = EmbedJobRunner(index_dir=tmp_path / "index", store_dir=tmp_path / "gone")
    runner.pause(True)
    runner.pause(False)


def test_time_left_comes_from_this_runs_own_rate() -> None:
    """No figure until 20 s of this run's progress; then the remainder at the measured rate."""
    from fnd.embed_job import TimeLeft

    now = [0.0]
    left = TimeLeft(clock=lambda: now[0])
    assert left.text(100, 2000) == ""
    now[0] = 10.0
    assert left.text(200, 2000) == ""
    now[0] = 30.0
    assert left.text(400, 2000) == "~3 min left"
    now[0] = 190.0
    assert left.text(1900, 2000) == "under a minute left"


def test_cancel_is_heard_while_tokenising(
    index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop during the first phase ends the run there, not after tokenising every section."""
    from fnd import embed_job

    monkeypatch.setattr(embed_job, "_CANCEL_EVERY", 1)
    fake = FakeEmbedder()
    _run(index, _store(tmp_path), fake, cancel=lambda: fake.tokenised >= 2)
    assert fake.tokenised == 2
    assert fake.embedded == 0


def test_a_busy_store_is_found_before_the_model_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry against another writer costs a lock attempt, not a model load."""
    from fnd import embed_job
    from fnd.semantic import GRANITE
    from fnd.vectors import store_header

    def no_load(*a: object) -> object:
        raise AssertionError("model loaded before the lock")

    monkeypatch.setattr(embed_job, "_load_embedder", no_load)
    monkeypatch.setattr(embed_job.os, "nice", lambda n: 0)
    monkeypatch.setattr(embed_job.signal, "signal", lambda *a: None)
    holder = VectorStore.open_writer(tmp_path / "v", dim=GRANITE.dim, header=store_header(GRANITE))
    try:
        assert (
            embed_job._main([str(tmp_path / "i"), str(tmp_path / "v"), "notes"])
            == embed_job.BUSY_EXIT
        )  # pyright: ignore[reportPrivateUsage]
    finally:
        holder.close()
