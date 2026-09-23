"""`Markdown · 3` sat four lines above `Tags (no_index excluded)`, and the
index held 2.

The count was a raw disk tally while the rule printed under it was the one
that actually ran. This is the audit surface for "did my private files stay
out?", so the number is what gets read.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from fnd.filters import FilterSpec, build_gate
from fnd.filters.dimensions import tag_selection
from fnd.filters.scan import sample_source


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "Vault"
    root.mkdir()
    (root / "meeting-notes.md").write_text("# Meeting\n\nnotes.\n", encoding="utf-8")
    (root / "project-alpha.md").write_text("# Alpha\n\nnotes.\n", encoding="utf-8")
    (root / "salary-review.md").write_text(
        "---\ntags:\n  - no_index\n  - hr\n---\n\n# Salary\n\nprivate.\n", encoding="utf-8"
    )
    return root


def test_the_kept_count_excludes_what_the_tag_rule_removes(tmp_path: Path) -> None:
    """Three markdown files on disk, one tagged `no_index`: the count is 2."""
    root = _vault(tmp_path)
    spec = FilterSpec(exclude_tags=tag_selection({"frontmatter": ["no_index"]}))

    sample = sample_source(root, gate=build_gate(spec))

    assert sample.kinds.get("md") == 3, sample.kinds
    assert sample.kinds_kept.get("md") == 2, sample.kinds_kept


def test_a_gate_carrying_the_kind_rule_would_report_zero(tmp_path: Path) -> None:
    """Why the browser strips the kind rule before gating: with it in, a kind
    the user has NOT ticked reads `0`, which says nothing about what ticking it
    would bring in. The browser's own gate is built in
    `open_source_filter_browser` via `dataclasses.replace(..., kinds=())`.
    """
    root = _vault(tmp_path)
    md_only = FilterSpec(kinds=("pdf",))

    with_kinds = sample_source(root, gate=build_gate(md_only))
    without = sample_source(root, gate=build_gate(dataclasses.replace(md_only, kinds=())))

    assert with_kinds.kinds_kept.get("md", 0) == 0, with_kinds.kinds_kept
    assert without.kinds_kept.get("md") == 3, without.kinds_kept


def test_a_source_whose_every_file_is_excluded_reports_zero(tmp_path: Path) -> None:
    """`kinds_kept` empty with `kinds` full is the case the pane exists for:
    every markdown file tagged `no_index`. The row must say 0, not 3."""
    from fnd.filters.tree_model import _kind_items

    root = tmp_path / "AllPrivate"
    root.mkdir()
    for i in range(3):
        (root / f"n{i}.md").write_text("---\ntags: [no_index]\n---\n\n# N\n", encoding="utf-8")
    spec = FilterSpec(exclude_tags=tag_selection({"frontmatter": ["no_index"]}))

    sample = sample_source(root, gate=build_gate(spec))

    assert sample.kinds.get("md") == 3, sample.kinds
    assert sample.kinds_kept == {}, sample.kinds_kept
    assert sample.gated is True
    assert not any("3" in label for _cat, key, label in _kind_items(sample) if key == "kind:md")


def test_no_gate_leaves_the_kept_count_empty(tmp_path: Path) -> None:
    """The control: without a gate there is nothing to disagree with, and the
    tree falls back to the raw tally."""
    sample = sample_source(_vault(tmp_path))

    assert sample.kinds.get("md") == 3, sample.kinds
    assert sample.kinds_kept == {}, sample.kinds_kept
