"""Every kind that carries frontmatter gives up its tags, not only `.md`.

`KindSpec.carries_frontmatter` is the single answer, so `read_file_metadata`
asks it rather than the suffix. The cost of asking the suffix is wider than a
`.txt`: every Markdown variant beyond `.md` loses its tags, so `.qmd` and
`.rmd`, which an academic corpus carries, index as untagged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.kinds import FRONTMATTER_KINDS, KIND_BY_ID
from fnd.query import _open_index
from fnd.schema import F_TAGS_FM

_INLINE = "---\ntags: [alpha, beta]\n---\n\nbody quorum here.\n"


def _indexed_tags(index_dir: Path) -> dict[str, list[str]]:
    index = _open_index(index_dir)
    index.reload()
    searcher = index.searcher()
    hits = searcher.search(index.parse_query("quorum", ["body"]), limit=50).hits
    out: dict[str, list[str]] = {}
    for _score, addr in hits:
        doc = searcher.doc(addr)
        name = Path(str(doc.get_first("path"))).name
        out[name] = sorted(str(t) for t in doc.get_all(F_TAGS_FM))
    return out


@pytest.mark.parametrize("suffix", [".md", ".markdown", ".mdx", ".qmd", ".rmd", ".txt"])
def test_a_note_kind_carries_its_frontmatter_tags(
    suffix: str, tmp_path: Path, tmp_index_dir: Path
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    name = f"note{suffix}"
    (root / name).write_text(_INLINE, encoding="utf-8")

    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")

    assert _indexed_tags(tmp_index_dir).get(name) == ["alpha", "beta"]


def test_a_kind_that_declares_no_frontmatter_is_left_alone(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """The control. A leading `---` block in Python is a comment fence at
    worst, and judging it is exactly what the registry's `False` forbids."""
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "script.py").write_text(_INLINE, encoding="utf-8")

    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")

    assert _indexed_tags(tmp_index_dir).get("script.py") == []


def test_the_suffixes_under_test_are_the_registry_s_own() -> None:
    """A parametrised list drifts from the registry the moment one is added,
    which is the failure this whole rule exists to prevent."""
    covered = {".md", ".markdown", ".mdx", ".qmd", ".rmd", ".txt"}
    every = {s for k in FRONTMATTER_KINDS for s in KIND_BY_ID[k].suffixes}

    assert covered <= every, sorted(covered - every)
    assert ".py" not in every
