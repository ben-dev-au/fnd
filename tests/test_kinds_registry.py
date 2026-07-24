"""Consistency tests for the central file-type registry (fnd.kinds).

These replace the safety a ``Kind`` Literal used to give: they assert the
registry is internally coherent and that every extractor it references exists.
"""

from __future__ import annotations

import importlib

import pytest

from fnd import kinds
from fnd.extract import supported_suffixes


def test_kind_ids_unique() -> None:
    ids = [k.id for k in kinds.KIND_SPECS]
    assert len(ids) == len(set(ids)), "duplicate kind id"


def test_every_category_is_registered() -> None:
    for spec in kinds.KIND_SPECS:
        assert spec.category in kinds.CATEGORY_BY_ID, f"{spec.id}: unknown category {spec.category}"


def test_every_category_has_members() -> None:
    for cat in kinds.CATEGORIES:
        assert kinds.KINDS_IN_CATEGORY[cat.id], f"category {cat.id} has no member kinds"


def test_suffixes_unique_lowercase_dotted() -> None:
    seen: dict[str, str] = {}
    for spec in kinds.KIND_SPECS:
        for sfx in spec.suffixes:
            assert sfx.startswith("."), f"bad suffix {sfx!r}"
            assert sfx == sfx.lower(), f"non-lowercase suffix {sfx!r}"
            assert sfx not in seen, f"suffix {sfx!r} claimed by {seen[sfx]!r} and {spec.id!r}"
            seen[sfx] = spec.id


def test_derived_lookups_agree() -> None:
    assert set(kinds.ALL_KIND_IDS) == {k.id for k in kinds.KIND_SPECS}
    assert supported_suffixes() == frozenset(kinds.SUFFIX_TO_MODULE)
    assert set(kinds.SUFFIX_TO_KIND) == set(kinds.SUFFIX_TO_MODULE)
    # markdown-rendered set matches the flags, and txt (flat) is excluded.
    assert kinds.MARKDOWN_RENDERED_KINDS == frozenset(
        k.id for k in kinds.KIND_SPECS if k.markdown_rendered
    )
    assert "txt" not in kinds.MARKDOWN_RENDERED_KINDS


@pytest.mark.parametrize("module_name", sorted({k.extractor_module for k in kinds.KIND_SPECS}))
def test_every_extractor_module_exposes_extract(module_name: str) -> None:
    mod = importlib.import_module(f"fnd.extract.{module_name}")
    assert callable(mod.extract), f"fnd.extract.{module_name}.extract is not callable"


def test_kind_for_suffix_is_case_insensitive() -> None:
    assert kinds.kind_for_suffix(".PY") == "python"
    assert kinds.kind_for_suffix(".Epub") == "epub"
    assert kinds.kind_for_suffix(".nope") is None


def test_new_families_present() -> None:
    # Guard against an accidental drop of a whole family.
    for kind_id in ("epub", "html", "ipynb", "odt", "odp", "ods", "python", "cpp", "csv", "json"):
        assert kind_id in kinds.KIND_BY_ID
