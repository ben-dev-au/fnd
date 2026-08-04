"""Regression guard: every Hit-rebuild helper preserves every field.

Phase 4 added ``Hit.line`` but the cascade / fusion / rerank rebuild
helpers were not audited — they kept the legacy field list, silently
defaulting ``line`` to 0 every time they cloned a Hit. The TUI's
search path always goes through cascade + rerank, so by the time a
Hit reached the opener, ``line=0``, and the vscode/sublime handlers
opened files at the top.

Pin every Hit-rebuild site by round-tripping a Hit with all fields
populated. Any new field added to :class:`fnd.query.Hit` that isn't
copied through these helpers will trip this guard on the next test
run, regardless of whether the new field has TUI surface yet.
"""

from __future__ import annotations

import dataclasses

import pytest

from fnd.cascade import _with_pass
from fnd.fusion import _with_pass_index, _with_score
from fnd.query import Hit
from fnd.rerank import _replace_score


def _populated_hit() -> Hit:
    """A Hit with every field set to a distinctive non-default value so
    accidental defaulting is unambiguous.

    Every field must be listed: the guard compares field-by-field, so any field
    left at its default compares equal to a dropped one and the guard passes
    while the field is being silently lost. That is exactly what happened to
    ``body_text`` and ``body_md`` — see
    :func:`test_every_hit_field_is_populated_by_the_fixture`, which now makes
    the omission impossible to repeat.
    """
    return Hit(
        score=1.5,
        parent_id="pid",
        path="/tmp/note.md",
        kind="md",
        page=3,
        slide=4,
        heading_path="A > B",
        title="Title",
        snippet="some text",
        page_label="iv",
        chunk_seq=7,
        line=42,
        mtime=1700000000,
        pass_index=2,
        meta_blob=b"\x01\x02",
        body_text="the full decoded chunk body",
        body_md="## the markdown the preview renders",
    )


def test_every_hit_field_is_populated_by_the_fixture() -> None:
    """The guard on the guard.

    A field left at its default in ``_populated_hit`` makes the round-trip
    assertions vacuous for that field. ``body_md`` was added to Hit and dropped
    by all four rebuild helpers, and this suite stayed green throughout because
    the fixture never set it.
    """
    populated = _populated_hit()
    empty = Hit(
        score=0.0,
        parent_id="",
        path="",
        kind="",
        page=0,
        slide=0,
        heading_path="",
        title="",
        snippet="",
    )
    defaulted = [
        f.name
        for f in dataclasses.fields(Hit)
        if getattr(populated, f.name) == getattr(empty, f.name)
    ]
    assert not defaulted, f"_populated_hit leaves these at their default: {defaulted}"


@pytest.mark.parametrize(
    ("name", "rebuilt"),
    [
        ("rerank._replace_score", _replace_score(_populated_hit(), 9.99)),
        ("fusion._with_score", _with_score(_populated_hit(), 9.99)),
        ("fusion._with_pass_index", _with_pass_index(_populated_hit(), 1)),
        ("cascade._with_pass", _with_pass(_populated_hit(), 1)),
    ],
)
def test_rebuild_preserves_all_fields_except_intentional_overrides(name: str, rebuilt: Hit) -> None:
    """Every field except the one the helper overrides (score or
    pass_index) must round-trip. Without this guard, adding a field to
    Hit silently defaults that field to 0/""/None in every rebuilt
    Hit — the exact class of bug behind Phase 4's missing line."""
    original = _populated_hit()
    overridden = {"score", "pass_index"}
    for field in dataclasses.fields(Hit):
        if field.name in overridden:
            continue
        assert getattr(rebuilt, field.name) == getattr(original, field.name), (
            f"{name} dropped {field.name}: "
            f"original={getattr(original, field.name)!r}, "
            f"rebuilt={getattr(rebuilt, field.name)!r}"
        )
