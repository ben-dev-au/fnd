"""`fnd search` blends when semantic search is on; reindexing embeds afterwards unless told not to."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

np = pytest.importorskip("numpy")

import fnd.cli as cli  # noqa: E402
import fnd.semantic_search as semantic_search  # noqa: E402
from fnd.cli import app  # noqa: E402
from fnd.query import Searcher  # noqa: E402
from tests._semantic_fakes import HEADER, FakeEmbedder, build_context  # noqa: E402


@pytest.fixture
def library(tmp_path: Path, isolated_config_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "band.md").write_text("# Band\n\n## Kit\n\ndrum and a flute\n", encoding="utf-8")
    (root / "orchestra.md").write_text(
        "# Orchestra\n\n## Solo\n\na piccolo solo\n", encoding="utf-8"
    )
    for i in range(4):
        (root / f"other{i}.md").write_text(
            f"# Other {i}\n\nboat {'gourd ' * (i + 1)}\n", encoding="utf-8"
        )
    isolated_config_path.write_text(
        textwrap.dedent(f"""
            [defaults]
            semantic_search = true

            [[collections.notes.sources]]
            path = "{root.as_posix()}"
        """),
        encoding="utf-8",
    )
    index_dir = tmp_path / "index"
    monkeypatch.setattr(cli, "default_index_dir", lambda: index_dir)
    assert CliRunner().invoke(app, ["collection", "reindex", "notes", "--no-embed"]).exit_code == 0
    return index_dir


def _fake_context(monkeypatch: pytest.MonkeyPatch, index_dir: Path, tmp_path: Path) -> None:
    ctx = build_context(Searcher(index_dir=index_dir), tmp_path / "vectors", opted_in={"notes"})
    monkeypatch.setattr(semantic_search, "context_for", lambda config: ctx)


def test_cli_search_blends_when_on(
    library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With semantic search on, a file without the word is listed."""
    _fake_context(monkeypatch, library, tmp_path)
    out = CliRunner().invoke(app, ["search", "flute", "-c", "notes"]).output
    assert "orchestra.md" in out


def test_cli_search_is_unchanged_when_off(
    library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no context, output is exactly the word-matching output."""
    monkeypatch.setattr(semantic_search, "context_for", lambda config: None)
    plain = CliRunner().invoke(app, ["search", "flute", "-c", "notes"]).output
    assert "orchestra.md" not in plain
    assert "band.md" in plain


def test_explain_shows_both_ranks(
    library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--explain reports the blend and each shown file's fnd and model positions."""
    _fake_context(monkeypatch, library, tmp_path)
    out = CliRunner().invoke(app, ["search", "flute", "-c", "notes", "--explain", "1"]).output
    payload = json.loads(out[out.index("{") :])
    assert payload["semantic"]["applied"] is True
    assert payload["semantic"]["ranks"]


def test_reindex_embeds_afterwards_unless_told_not_to(
    library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reindex runs the reconcile; --no-embed skips it."""
    calls: list[str] = []
    monkeypatch.setattr(cli, "_embed_after_index", lambda cfg: calls.append("embedded"))
    assert CliRunner().invoke(app, ["collection", "reindex", "notes", "--no-embed"]).exit_code == 0
    assert calls == []
    assert CliRunner().invoke(app, ["collection", "reindex", "notes"]).exit_code == 0
    assert calls == ["embedded"]


def test_ctrl_c_during_embedding_keeps_finished_batches(
    library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupt mid-reconcile leaves every appended batch in the store and says so."""
    from fnd.vectors import VectorStore

    fake = FakeEmbedder()
    folder = tmp_path / "store"
    real = fake.embed

    def embed(texts: list[str]):  # type: ignore[no-untyped-def]
        if fake.embedded >= 4:
            raise KeyboardInterrupt
        return real(texts)

    fake.embed = embed  # type: ignore[method-assign]
    monkeypatch.setattr(
        cli,
        "_semantic_job_parts",
        lambda: (fake, VectorStore.open_writer(folder, dim=fake.dim, header=HEADER)),
    )
    monkeypatch.setattr(cli, "_semantic_ready", lambda: True)
    monkeypatch.setattr("fnd.embed_job.BATCH", 2)
    result = CliRunner().invoke(app, ["collection", "reindex", "notes"])
    assert result.exit_code == 130
    assert "stopped" in result.output
    assert len(VectorStore.open(folder, dim=fake.dim, header=HEADER)) >= 4


def test_status_reports_how_many_sections_are_embedded(
    library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fnd extras status` gives the embedded section count once installed."""
    from fnd import extras

    monkeypatch.setattr(extras, "is_extra_installed", lambda extra: extra.name == "semantic")
    monkeypatch.setattr("fnd.semantic.is_model_present", lambda spec=None: True)
    build_context(Searcher(index_dir=library), tmp_path / "unused", opted_in={"notes"}).rows.save(
        _store_dir()
    )
    out = CliRunner().invoke(app, ["extras", "status"]).output
    assert "sections embedded" in out


def _store_dir() -> Path:
    from fnd.semantic import GRANITE
    from fnd.vectors import store_dir

    folder = store_dir(GRANITE)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_ad_hoc_index_embeds_afterwards_too(
    library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fnd index ROOT` reconciles the vectors as `collection reindex` does."""
    calls: list[str] = []
    monkeypatch.setattr(cli, "_embed_after_index", lambda cfg: calls.append("embedded"))
    assert (
        CliRunner()
        .invoke(app, ["index", str(tmp_path / "notes"), "--collection", "notes"])
        .exit_code
        == 0
    )
    assert (
        CliRunner()
        .invoke(app, ["index", str(tmp_path / "notes"), "--collection", "notes", "--no-embed"])
        .exit_code
        == 0
    )
    assert calls == ["embedded"]


def test_a_reindex_finds_the_running_embed_before_loading_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another writer holding the store costs a lock attempt, not a model load."""
    from fnd import semantic
    from fnd.semantic import GRANITE
    from fnd.vectors import StoreBusyError, VectorStore, store_dir, store_header

    def no_load(**kw: object) -> object:
        raise AssertionError("model loaded before the lock")

    monkeypatch.setattr(semantic, "load_embedder", no_load)
    holder = VectorStore.open_writer(
        store_dir(GRANITE), dim=GRANITE.dim, header=store_header(GRANITE)
    )
    try:
        with pytest.raises(StoreBusyError):
            cli._semantic_job_parts()  # pyright: ignore[reportPrivateUsage]
    finally:
        holder.close()


def test_a_model_that_fails_to_load_lets_go_of_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lock taken first is released when the model then cannot load."""
    from fnd import semantic
    from fnd.semantic import GRANITE
    from fnd.vectors import VectorStore, store_dir, store_header

    def broken(**kw: object) -> object:
        raise OSError("model unreadable")

    monkeypatch.setattr(semantic, "load_embedder", broken)
    with pytest.raises(OSError, match="unreadable") as failed:
        cli._semantic_job_parts()  # pyright: ignore[reportPrivateUsage]
    assert failed.traceback  # holds the frame, so only an explicit close frees the lock
    VectorStore.open_writer(
        store_dir(GRANITE), dim=GRANITE.dim, header=store_header(GRANITE)
    ).close()


def test_a_search_before_any_vectors_are_saved_loads_no_model(
    library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switched on but not yet embedded, a CLI search pays nothing for the model."""
    from fnd import semantic, semantic_search
    from fnd.config import Config, Defaults

    def no_load(**kw: object) -> object:
        raise AssertionError("model loaded with no vectors to search")

    monkeypatch.setattr(semantic, "is_ready", lambda: True)
    monkeypatch.setattr(semantic, "load_embedder", no_load)
    assert semantic_search.context_for(Config(defaults=Defaults(semantic_search=True))) is None
