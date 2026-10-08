"""What an earlier build saved still means the same thing once names and text are canonical."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

NFD = "cafe\N{COMBINING ACUTE ACCENT}"
NFC = "caf\N{LATIN SMALL LETTER E WITH ACUTE}"


# ── saved scope ──────────────────────────────────────────────────────


def test_a_saved_scope_names_a_collection_as_the_config_now_does(tmp_path: Path) -> None:
    from fnd.state import load

    saved = tmp_path / "scope.toml"
    saved.write_text(
        f'[scope]\ncollections = ["{NFD}"]\nsources = []\n'
        f'[panels]\nexpanded_collections = ["{NFD}"]\n',
        encoding="utf-8",
    )
    state = load(saved)
    assert state.collections == [NFC]
    assert state.expanded_collections == [NFC]


def test_a_saved_tag_selection_is_canonical(tmp_path: Path) -> None:
    from fnd.state import load

    saved = tmp_path / "scope.toml"
    saved.write_text(f'[filters]\ntag_include = {{ os = ["{NFD}"] }}\n', encoding="utf-8")
    assert load(saved).tag_include == {"os": [NFC]}


@pytest.mark.parametrize(
    "raw",
    [
        "\N{LATIN SMALL LETTER J WITH CARON}-tag",
        "\N{GREEK SMALL LETTER UPSILON WITH PSILI}\N{GREEK SMALL LETTER GAMMA}",
        "\N{GREEK SMALL LETTER ALPHA WITH PERISPOMENI}\N{GREEK SMALL LETTER SIGMA}",
        "##foo",
    ],
)
def test_a_saved_tag_restores_as_the_index_holds_it(tmp_path: Path, raw: str) -> None:
    """Case folding can decompose a letter, so a restored tag must be the indexed term."""
    from fnd.state import UiState, load, save
    from fnd.tags import normalise_tag

    tag = normalise_tag(raw)
    save(UiState(tag_include={"os": [tag]}), path=tmp_path / "scope.toml")
    assert load(tmp_path / "scope.toml").tag_include == {"os": [tag]}


def test_a_decomposed_name_on_the_command_line_finds_its_collection() -> None:
    from fnd.vocabulary import Vocabulary

    assert Vocabulary("collection", [NFC], case_sensitive=True).match(NFD) == NFC


# ── a run saved under the old spelling ───────────────────────────────


def test_a_run_saved_under_a_decomposed_name_resumes_under_the_composed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fnd import index_runner

    monkeypatch.setattr(index_runner, "state_dir", lambda: tmp_path)
    # A zero-width space, not NFD: APFS treats NFD and NFC as one file name.
    old_name = "Ca\N{ZERO WIDTH SPACE}fe"
    old = tmp_path / f"{old_name}.state.toml"
    old.write_text(
        f'[state]\ncollection = "{old_name}"\nstarted_at = "2026-10-01T00:00:00+00:00"\n'
        "total_files = 4\nfiles_completed = 1\n",
        encoding="utf-8",
    )
    ((path, state),) = index_runner.saved_states()
    assert state.collection == "Cafe"
    assert path == index_runner.state_file_for("Cafe")
    assert not old.exists()


# ── decoding ─────────────────────────────────────────────────────────


def test_a_tie_between_bad_and_good_bytes_stays_utf8() -> None:
    from fnd.text_canon import decode

    assert decode(f"{NFC} ".encode() + b"\xff").startswith(NFC)


# ── relative source paths ────────────────────────────────────────────


def test_a_relative_source_in_the_toml_is_under_the_config_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fnd.config import load

    home = tmp_path / "cfg"
    home.mkdir()
    (home / "config.toml").write_text(
        '[[collections.notes.sources]]\npath = "notes"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert load(home / "config.toml").collections["notes"].sources[0].path == home / "notes"


def test_a_relative_legacy_root_is_under_the_config_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fnd.config import load

    home = tmp_path / "cfg"
    home.mkdir()
    (home / "config.toml").write_text('[collections.notes]\nroots = ["notes"]\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert load(home / "config.toml").collections["notes"].sources[0].path == home / "notes"


def test_a_relative_source_on_the_command_line_is_under_the_shell_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fnd.config import SourceConfig

    monkeypatch.chdir(tmp_path)
    assert SourceConfig(path="notes").path == tmp_path / "notes"  # pyright: ignore[reportArgumentType]


# ── one fold ─────────────────────────────────────────────────────────

# Paths are compared as the filesystem spells them, never canonical.
_FOLD_ALLOWED = {"text_canon.py", "cloud_files.py"}


def test_case_and_unicode_folding_live_in_text_canon() -> None:
    root = Path(__file__).resolve().parent.parent / "fnd"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel in _FOLD_ALLOWED:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr in ("casefold", "normalize"):
                offenders.append(f"{rel}:{getattr(node, 'lineno', 0)}")
    assert not offenders, offenders


def test_fold_meets_case_and_composition() -> None:
    from fnd.text_canon import fold

    assert fold("CAFE\N{COMBINING ACUTE ACCENT}") == fold(NFC)
    assert fold("Stra\N{LATIN SMALL LETTER SHARP S}e") == fold("STRASSE")


def test_a_synonym_group_matches_a_decomposed_term() -> None:
    from fnd.synonyms import SynonymTable

    table = SynonymTable.from_groups([[NFC, "coffee shop"]])
    assert table.expansions_for(NFD) == (NFC, "coffee shop")
