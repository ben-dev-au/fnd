"""One collection fully ticked beside one partly ticked returned nothing.

`collections` came from FULL collections and `active_sources` from PARTIAL
ones, two disjoint channels, and the query ANDed them: it intersected a
collection name with another collection's source path. The sidebar went on
reading `2/2 active, 3/4 sources` with three sources ticked.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import Config, load
from fnd.index import build_index_from_config
from fnd.query import Searcher


@pytest.fixture
def two_collections(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    vault = tmp_path / "Vault"
    workdocs = tmp_path / "WorkDocs"
    personaldocs = tmp_path / "PersonalDocs"
    # Personal's second source, deliberately never ticked: without something
    # the scope EXCLUDES, a scoped search and an unscoped one return the same
    # set and no assertion on them can discriminate.
    archive = tmp_path / "Archive"
    for d, marker in (
        (vault, "markervault"),
        (workdocs, "markerwork"),
        (personaldocs, "markerpersonal"),
        (archive, "markerarchive"),
    ):
        d.mkdir()
        (d / "note.md").write_text(f"# Note\n\nhaystack {marker} here.\n", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.Work.sources]]
            path = "{vault.as_posix()}"
            [[collections.Work.sources]]
            path = "{workdocs.as_posix()}"
            [[collections.Personal.sources]]
            path = "{vault.as_posix()}"
            [[collections.Personal.sources]]
            path = "{personaldocs.as_posix()}"
            [[collections.Personal.sources]]
            path = "{archive.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    cfg = load(cfg_path)
    # From the CONFIG, not from roots: only this path records `source_path`,
    # which is the field the per-collection source scope filters on.
    for name in ("Work", "Personal"):
        build_index_from_config(
            config=cfg.collections[name], collection=name, index_dir=tmp_index_dir
        )
    return cfg


def _hits(searcher: Searcher, query: str, **kw: object) -> set[str]:
    return {Path(h.path).parent.name for h in searcher.search(query, limit=50, **kw)}  # type: ignore[arg-type]


def test_a_full_collection_survives_beside_a_partial_one(
    two_collections: Config, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Work whole, Personal reduced to PersonalDocs: both must answer."""
    searcher = Searcher(index_dir=tmp_index_dir)
    personaldocs = str((tmp_path / "PersonalDocs").resolve())

    found = _hits(
        searcher,
        "haystack",
        collection=["Work"],
        source_scope={"Personal": [personaldocs]},
    )

    assert found == {"Vault", "WorkDocs", "PersonalDocs"}, found
    assert "Archive" not in found, "Personal's unticked source must stay out"


def test_a_partial_selection_reaches_a_shared_file_only_through_its_source(
    two_collections: Config, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """The provenance a flat path list threw away: Vault is listed in BOTH
    configs and is stored once with membership (Work, Vault) and
    (Personal, Vault). Scoping Personal to its Vault source reaches it; scoping
    Personal to a DIFFERENT source (PersonalDocs) does not, because the
    membership pairs each collection with the source that reached the file.
    """
    searcher = Searcher(index_dir=tmp_index_dir)
    vault = str((tmp_path / "Vault").resolve())
    personaldocs = str((tmp_path / "PersonalDocs").resolve())

    via_vault = searcher._filtered_raw_hits(
        "markervault",
        target=50,
        collection=None,
        metadata_filter=None,
        source_scope={"Personal": [vault]},
    )
    via_personaldocs = searcher._filtered_raw_hits(
        "markervault",
        target=50,
        collection=None,
        metadata_filter=None,
        source_scope={"Personal": [personaldocs]},
    )

    assert len(via_vault) == 1, via_vault
    assert len(via_personaldocs) == 0, "the Vault note is not in Personal via PersonalDocs"


def test_the_fuzzy_cascade_honours_a_partial_scope(
    two_collections: Config, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """The cascade is where a mistyped query lands, and it had its own copy of
    the scope filter that read collection names as source paths."""
    from fnd.cascade import cascade_search

    searcher = Searcher(index_dir=tmp_index_dir)
    personaldocs = str((tmp_path / "PersonalDocs").resolve())

    hits = cascade_search(
        searcher,
        query="haystakc",
        threshold=50,
        limit=50,
        collection=["Work"],
        source_scope={"Personal": [personaldocs]},
    )

    # `Archive` is the discriminator: it belongs to Personal, is NOT ticked,
    # and appears the moment the scope filter stops running.
    assert {Path(h.path).parent.name for h in hits} == {"Vault", "WorkDocs", "PersonalDocs"}


def test_an_explicitly_empty_scope_still_finds_nothing_in_the_cascade(
    two_collections: Config, tmp_index_dir: Path
) -> None:
    """The control: an empty scope and no scope are different, and a single
    query cannot say so, which is why the cascade reads the arms itself."""
    from fnd.cascade import cascade_search

    hits = cascade_search(
        Searcher(index_dir=tmp_index_dir),
        query="haystakc",
        threshold=50,
        limit=50,
        collection=[],
        source_scope=None,
    )

    assert hits == [], hits
