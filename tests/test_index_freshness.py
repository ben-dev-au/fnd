"""What a run indexed with, compared with the config now (spec D4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fnd.config import CollectionConfig, Config, Defaults, SourceConfig
from fnd.index_freshness import State, compare, indexed_with


def _config(tmp_path: Path, **source: Any) -> Config:
    (tmp_path / "notes").mkdir(exist_ok=True)
    return Config(
        collections={
            "notes": CollectionConfig(sources=[SourceConfig(path=tmp_path / "notes", **source)])
        }
    )


def _now(cfg: Config, **defaults: Any) -> dict[str, Any]:
    merged = cfg.defaults.model_copy(update=defaults) if defaults else cfg.defaults
    return indexed_with(cfg.collections["notes"], merged)


def test_the_same_config_is_current(tmp_path: Path) -> None:
    """Recording and comparing the same inputs reads current."""
    cfg = _config(tmp_path)
    assert compare(_now(cfg), _now(cfg)).state is State.CURRENT


def test_nothing_recorded_reads_not_indexed(tmp_path: Path) -> None:
    """A collection no run has finished for is not indexed."""
    assert compare(_now(_config(tmp_path)), None).state is State.NOT_INDEXED


def test_an_exclude_needs_an_update_and_names_it(tmp_path: Path) -> None:
    """Which files belong changed, so an Update converges it."""
    before = _now(_config(tmp_path))
    verdict = compare(_now(_config(tmp_path, excludes=["build/**"])), before)
    assert verdict.state is State.NEEDS_UPDATE
    assert verdict.reasons == ("excludes",)


def test_an_index_filter_needs_an_update(tmp_path: Path) -> None:
    """Index-time filters are selection: an Update prunes what they now drop."""
    before = _now(_config(tmp_path))
    after = _now(_config(tmp_path, filters={"exclude_tags": ["draft"]}))
    assert compare(after, before).reasons == ("index filters",)


def test_an_extra_tag_key_needs_a_rebuild(tmp_path: Path) -> None:
    """Tags are read when a file is indexed, and an Update skips unchanged files."""
    cfg = _config(tmp_path)
    verdict = compare(_now(cfg, tag_frontmatter_keys=["Course"]), _now(cfg))
    assert verdict.state is State.NEEDS_REBUILD
    assert verdict.reasons == ("extra tag keys",)


def test_a_tag_key_differing_only_in_case_is_no_change(tmp_path: Path) -> None:
    """Keys match case-insensitively, so case alone changes nothing indexed."""
    cfg = _config(tmp_path)
    before = _now(cfg, tag_frontmatter_keys=["course"])
    assert compare(_now(cfg, tag_frontmatter_keys=["Course"]), before).state is State.CURRENT


def test_a_tag_source_turned_on_needs_a_rebuild(tmp_path: Path) -> None:
    """A source turned on has tags no run has read yet."""
    cfg = _config(tmp_path)
    before = _now(cfg, tag_sources=["frontmatter"])
    after = _now(cfg, tag_sources=["frontmatter", "os"])
    assert compare(after, before).state is State.NEEDS_REBUILD


def test_a_tag_source_turned_off_is_still_current(tmp_path: Path) -> None:
    """Turning a source off hides its tags at search time; nothing needs reading."""
    cfg = _config(tmp_path)
    before = _now(cfg, tag_sources=["frontmatter", "os"])
    after = _now(cfg, tag_sources=["frontmatter"])
    assert compare(after, before).state is State.CURRENT


def test_a_rebuild_wins_over_an_update(tmp_path: Path) -> None:
    """A rebuild walks every file, so it converges the selection too."""
    before = _now(_config(tmp_path))
    after = indexed_with(
        _config(tmp_path, excludes=["x/**"]).collections["notes"],
        Defaults(tag_frontmatter_keys=["Topic"]),
    )
    assert compare(after, before).state is State.NEEDS_REBUILD


def test_the_app_is_in_neither_fingerprint(tmp_path: Path) -> None:
    """Which app opens a file changes nothing in the index."""
    before = _now(_config(tmp_path))
    after = _now(_config(tmp_path, app="obsidian", app_params={"vault": "v"}))
    assert compare(after, before).state is State.CURRENT


def test_a_reverted_change_reads_current_again(tmp_path: Path) -> None:
    """Nothing is flagged, so undoing the edit undoes the verdict."""
    before = _now(_config(tmp_path))
    assert compare(_now(_config(tmp_path, excludes=[])), before).state is State.CURRENT


def test_a_recorded_value_survives_a_json_round_trip(tmp_path: Path) -> None:
    """The sidecar is JSON; dates and tuples must compare equal after it."""
    cfg = _config(tmp_path, filters={"created_after": "2024-01-01"})
    now = _now(cfg)
    assert compare(now, json.loads(json.dumps(now))).state is State.CURRENT


def test_a_summary_names_the_state_and_the_reasons(tmp_path: Path) -> None:
    """The marker's description reads as one phrase."""
    before = _now(_config(tmp_path))
    after = _now(_config(tmp_path, excludes=["b/**"], follow_symlinks=True))
    assert compare(after, before).summary == "needs update (excludes, follow symlinks changed)"


def _ledger_dir(tmp_path: Path) -> Path:
    index_dir = tmp_path / "idx"
    index_dir.mkdir(exist_ok=True)
    return index_dir


def test_a_record_reads_back_after_a_restart(tmp_path: Path) -> None:
    """A second ledger over the same directory sees the first one's record."""
    from fnd.index_freshness import Ledger

    cfg = _config(tmp_path)
    Ledger(_ledger_dir(tmp_path)).record("notes", _now(cfg))
    verdict = Ledger(tmp_path / "idx").verdict("notes", cfg.collections["notes"], cfg.defaults)
    assert verdict.state is State.CURRENT


def test_forget_reads_not_indexed(tmp_path: Path) -> None:
    """A dropped collection has nothing recorded."""
    from fnd.index_freshness import Ledger

    ledger = Ledger(_ledger_dir(tmp_path))
    ledger.record("notes", _now(_config(tmp_path)))
    ledger.forget("notes")
    assert ledger.recorded("notes") is None


def test_a_corrupt_sidecar_reads_as_nothing_recorded(tmp_path: Path) -> None:
    """A torn or hand-edited file must not crash the sidebar."""
    from fnd.index_freshness import SIDECAR_NAME, Ledger

    (_ledger_dir(tmp_path) / SIDECAR_NAME).write_text("{not json", encoding="utf-8")
    assert Ledger(tmp_path / "idx").recorded("notes") is None


def test_no_index_directory_records_nothing(tmp_path: Path) -> None:
    """Recording never creates the index directory; the runner owns that."""
    from fnd.index_freshness import Ledger

    Ledger(tmp_path / "missing").record("notes", _now(_config(tmp_path)))
    assert not (tmp_path / "missing").exists()


def test_adopt_records_only_collections_that_hold_documents(tmp_path: Path) -> None:
    """An index built before the sidecar existed is taken as current, not unindexed."""
    from fnd.index_freshness import Ledger

    cfg = _config(tmp_path)
    cfg.collections["empty"] = CollectionConfig(sources=[])
    ledger = Ledger(_ledger_dir(tmp_path))
    assert ledger.adopt(cfg, is_empty=lambda name: name == "empty") == ["notes"]
    assert ledger.recorded("empty") is None


def test_adopt_leaves_an_existing_record_alone(tmp_path: Path) -> None:
    """Adoption must never overwrite what a run recorded."""
    from fnd.index_freshness import Ledger

    ledger = Ledger(_ledger_dir(tmp_path))
    stale = _now(_config(tmp_path, excludes=["old/**"]))
    ledger.record("notes", stale)
    assert ledger.adopt(_config(tmp_path), is_empty=lambda _n: False) == []
    assert ledger.recorded("notes") == stale


def test_adoption_happens_once(tmp_path: Path) -> None:
    """After the one adoption, a name missing from the record is an unfinished run."""
    from fnd.index_freshness import Ledger

    cfg = _config(tmp_path)
    ledger = Ledger(_ledger_dir(tmp_path))
    assert ledger.adopt(cfg, is_empty=lambda _n: False) == ["notes"]
    cfg.collections["half"] = CollectionConfig(sources=[])
    assert ledger.adopt(cfg, is_empty=lambda _n: False) == []
    assert ledger.recorded("half") is None


def test_a_cli_run_before_the_first_launch_does_not_block_adoption(tmp_path: Path) -> None:
    """A run recorded before any adoption leaves the other old collections to adopt."""
    from fnd.index_freshness import Ledger

    cfg = _config(tmp_path)
    (tmp_path / "research").mkdir()
    cfg.collections["research"] = CollectionConfig(
        sources=[SourceConfig(path=tmp_path / "research")]
    )
    ledger = Ledger(_ledger_dir(tmp_path))
    ledger.record("notes", _now(cfg))
    assert ledger.adopt(cfg, is_empty=lambda _n: False) == ["research"]


def test_a_new_source_reads_as_a_source_list_change(tmp_path: Path) -> None:
    """The reason reads as one phrase: "(the source list changed)"."""
    before = _now(_config(tmp_path))
    cfg = _config(tmp_path)
    (tmp_path / "more").mkdir()
    cfg.collections["notes"].sources.append(SourceConfig(path=tmp_path / "more"))
    verdict = compare(_now(cfg), before)
    assert verdict.summary == "needs update (the source list changed)"


def test_a_replace_within_one_clock_tick_is_still_read(tmp_path: Path) -> None:
    """Coarse filesystems give two writes one mtime; the atomic replace changes the inode."""
    import os

    from fnd.index_freshness import SIDECAR_NAME, Ledger

    ledger = Ledger(_ledger_dir(tmp_path))
    first = _now(_config(tmp_path))
    ledger.record("notes", first)
    path = tmp_path / "idx" / SIDECAR_NAME
    stamp = path.stat().st_mtime_ns
    assert ledger.recorded("notes") == first
    other = Ledger(tmp_path / "idx")
    other.record("notes", _now(_config(tmp_path, excludes=["x/**"])))
    os.utime(path, ns=(stamp, stamp))
    assert ledger.recorded("notes") != first


def test_a_fresh_index_has_nothing_to_adopt(tmp_path: Path) -> None:
    """An index created after the record existed holds no pre-record documents."""
    from fnd.index import _ensure_index
    from fnd.index_freshness import Ledger

    _ensure_index(tmp_path / "idx")
    assert Ledger(tmp_path / "idx").adopt(_config(tmp_path), is_empty=lambda _n: False) == []


def test_an_existing_index_is_still_adopted(tmp_path: Path) -> None:
    """The control: an index from before the record still has its collections adopted."""
    from fnd.index import _ensure_index
    from fnd.index_freshness import Ledger
    from fnd.schema import SCHEMA_VERSION

    (tmp_path / "idx").mkdir()
    (tmp_path / "idx" / ".fnd-schema-version").write_text(str(SCHEMA_VERSION), encoding="utf-8")
    _ensure_index(tmp_path / "idx")
    adopted = Ledger(tmp_path / "idx").adopt(_config(tmp_path), is_empty=lambda _n: False)
    assert adopted == ["notes"]
