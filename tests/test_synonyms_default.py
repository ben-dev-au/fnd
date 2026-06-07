"""Bundled default synonym table + default/personal merge."""

from __future__ import annotations

from pathlib import Path

from fnd.synonyms import (
    DEFAULT_SYNONYMS_PATH,
    SynonymTable,
    expand,
    load_default_synonyms,
    load_merged_synonyms,
    load_synonyms,
    merge_tables,
)


def test_default_table_loads_nonempty():
    table = load_default_synonyms()
    assert table.groups
    assert DEFAULT_SYNONYMS_PATH.exists()


def test_mfa_expands_to_full_form():
    table = load_default_synonyms()
    out = expand("mfa", table)
    assert "multi-factor authentication" in out
    assert "2fa" in out
    # bidirectional: the quoted full phrase expands back to the acronym
    assert "mfa" in expand('"multi-factor authentication"', table)


def test_unquoted_multiword_expands_to_acronym():
    """The reverse direction: an UNQUOTED multi-word form must expand to the
    acronym (hyphen/space-agnostic), not only acronym -> phrase."""
    table = load_default_synonyms()
    assert "mfa" in expand("multi-factor authentication", table)  # hyphenated
    assert "mfa" in expand("multi factor authentication", table)  # spaced
    # mid-query, with surrounding words preserved
    out = expand("deploy multi factor authentication now", table)
    assert "mfa" in out
    assert out.startswith("deploy ") and out.endswith(" now")


def test_unrelated_multiword_left_untouched():
    table = load_default_synonyms()
    assert expand("totally unrelated phrase here", table) == "totally unrelated phrase here"


def test_acronym_groups_present():
    table = load_default_synonyms()
    for term in ("vpn", "pki", "ids", "siem", "tls"):
        assert table.expansions_for(term) is not None


def test_personal_extends_not_wipes_defaults(tmp_path: Path):
    personal = tmp_path / "synonyms.toml"
    personal.write_text(
        '[synonyms.local]\ngroups = [["honk", "goose noise"]]\n',
        encoding="utf-8",
    )
    table = load_merged_synonyms(personal)
    # personal group present
    assert table.expansions_for("honk") is not None
    # defaults still present
    assert table.expansions_for("mfa") is not None


def test_personal_group_folds_into_matching_default(tmp_path: Path):
    personal = tmp_path / "synonyms.toml"
    personal.write_text(
        '[synonyms.local]\ngroups = [["mfa", "authenticator app"]]\n',
        encoding="utf-8",
    )
    table = load_merged_synonyms(personal)
    out = expand("mfa", table)
    # original default forms AND the user's addition share one group
    assert "multi-factor authentication" in out
    assert "authenticator app" in out


def test_missing_personal_file_is_fine(tmp_path: Path):
    table = load_merged_synonyms(tmp_path / "does-not-exist.toml")
    assert table.expansions_for("vpn") is not None


def test_expansion_skipped_inside_quoted_phrase():
    table = load_default_synonyms()
    out = expand('"mfa rollout"', table)
    # the whole phrase isn't a group member → left untouched, no inner expand
    assert out == '"mfa rollout"'


def test_merge_unions_shared_term_groups():
    a = SynonymTable.from_groups([["x", "ex"]])
    b = SynonymTable.from_groups([["x", "exes"]])
    merged = merge_tables(a, b)
    group = merged.expansions_for("x")
    assert group is not None
    assert {t.casefold() for t in group} == {"x", "ex", "exes"}


def test_load_synonyms_missing_is_empty(tmp_path: Path):
    assert load_synonyms(tmp_path / "nope.toml").groups == []
