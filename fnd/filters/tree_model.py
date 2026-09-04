"""The filter set as the branches a tree shows, and back again.

Pure model: no Textual import, so the mapping between a
:class:`~fnd.filters.model.FilterSpec` and what the user sees is testable on
its own. The screen renders these groups and hands the selection back.

Date and size branches offer named windows rather than a typed value, and a
window resolves to an absolute bound when it is chosen — an index must not
change what it holds as the clock moves.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace

from fnd.filters.model import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.kinds import CATEGORIES, KIND_BY_ID, KINDS_IN_CATEGORY

__all__ = ["BRANCHES", "LEGEND", "apply_selection", "selection_for", "spec_branches"]

# (id, label, days back). ``None`` days = no bound.
_WINDOWS: tuple[tuple[str, str, int | None], ...] = (
    ("any", "Any time", None),
    ("7", "Last 7 days", 7),
    ("30", "Last 30 days", 30),
    ("90", "Last 3 months", 90),
    ("365", "Last 12 months", 365),
)

_SIZES: tuple[tuple[str, str, int | None], ...] = (
    ("any", "Any size", None),
    ("1mb", "Under 1 MB", 1_000_000),
    ("10mb", "Under 10 MB", 10_000_000),
    ("50mb", "Under 50 MB", 50_000_000),
    ("200mb", "Under 200 MB", 200_000_000),
)

BRANCHES = ("kinds", "tags", "ignore", "size", "modified", "created")

# Shown above the tree. The glyphs carry different polarity per branch —
# ● on a file type includes, ⊘ on a tag excludes — so the meaning is stated
# once here rather than guessed from each row.
LEGEND = "●  keep only these   ⊘  never these   ○  no rule"


@dataclass(frozen=True, slots=True)
class Branch:
    """One collapsible row: its leaves, its sub-branches and how they behave.

    ``empty_label`` says what the branch means with nothing switched on —
    without it, "no file type ticked" reads as *nothing is indexed*.
    """

    id: str
    label: str
    mode: str
    items: tuple[tuple[str, str], ...] = ()  # (item id, label)
    groups: tuple[Branch, ...] = ()
    empty_label: str = ""


def _kind_items(sample: SourceSample | None) -> list[tuple[str, str, str]]:
    """(category id, kind id, label) for kinds present, or all when unknown."""
    present = set(sample.kinds) if sample and sample.kinds else None
    out: list[tuple[str, str, str]] = []
    for cat in CATEGORIES:
        for kind in KINDS_IN_CATEGORY.get(cat.id, ()):
            if present is not None and kind not in present:
                continue
            spec = KIND_BY_ID.get(kind)
            if spec is None:
                continue
            count = (sample.kinds.get(kind, 0) if sample else 0) or 0
            suffixes = "/".join(spec.suffixes)
            label = f"{spec.label} ({suffixes})"
            out.append((cat.id, f"kind:{kind}", f"{label}  ·  {count}" if count else label))
    return out


def spec_branches(spec: FilterSpec, sample: SourceSample | None = None) -> list[Branch]:
    """The branches a filter screen should render for ``spec``."""
    branches: list[Branch] = []

    kinds = _kind_items(sample)
    by_cat: dict[str, list[tuple[str, str]]] = {}
    for cat_id, kind, label in kinds:
        by_cat.setdefault(cat_id, []).append((kind, label))
    categories = tuple(
        Branch(f"kinds:{cat.id}", cat.label, "multi", tuple(by_cat[cat.id]))
        for cat in CATEGORIES
        if by_cat.get(cat.id)
    )
    if categories:
        branches.append(
            Branch(
                "kinds",
                "File types",
                "multi",
                groups=categories,
                empty_label="every type",
            )
        )

    # Counts merge across providers: a tag carried both as an OS tag and in
    # YAML is one exclusion, and listing it twice would give the tree two rows
    # sharing an id.
    totals: dict[str, int] = {}
    for source in ("os", "frontmatter"):
        for value, count in sample.tags_for(source) if sample else []:
            totals[value] = totals.get(value, 0) + count
    seen: list[tuple[str, str]] = [
        (f"tag:{v}", f"{v}  ({c})")
        for v, c in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    for tag in (*spec.include_tags, *spec.exclude_tags):
        if not any(i[0] == f"tag:{tag}" for i in seen):
            seen.append((f"tag:{tag}", tag))
    # Whatever is switched on sorts first, so the branch shows what it is
    # doing without the user scrolling a corpus-length list to find it.
    active = {f"tag:{t}" for t in (*spec.include_tags, *spec.exclude_tags)}
    tag_items = [i for i in seen if i[0] in active] + [i for i in seen if i[0] not in active]
    if tag_items:
        branches.append(Branch("tags", "Tags", "cycle", tuple(tag_items), empty_label="any tag"))

    branches.append(
        Branch(
            "ignore",
            "Obey ignore files",
            "multi",
            (("ignore:git", ".gitignore"), ("ignore:fnd", ".fndignore")),
            empty_label="none",
        )
    )
    branches.append(
        Branch(
            "size", "Maximum file size", "radio", tuple((f"size:{i}", lbl) for i, lbl, _ in _SIZES)
        )
    )
    for field_name, label in (("modified", "Modified within"), ("created", "Created within")):
        branches.append(
            Branch(
                field_name,
                label,
                "radio",
                tuple((f"{field_name}:{i}", lbl) for i, lbl, _ in _WINDOWS),
            )
        )
    return branches


def selection_for(
    spec: FilterSpec, *, gitignore: bool = True, fndignore: bool = True
) -> tuple[set[str], set[str]]:
    """``(selected, excluded)`` item ids matching ``spec``.

    The two ignore flags are passed separately: they choose which files are
    read rather than filtering one, so they are not part of the predicate
    spec the gate compiles.
    """
    selected: set[str] = {f"tag:{t}" for t in spec.include_tags}
    excluded: set[str] = {f"tag:{t}" for t in spec.exclude_tags}
    selected |= {f"kind:{k}" for k in spec.kinds}
    # ``kinds`` empty means every type, which the tree shows as nothing ticked.
    if gitignore:
        selected.add("ignore:git")
    if fndignore:
        selected.add("ignore:fnd")
    selected.add(f"size:{_size_id(spec.max_size)}")
    selected.add(f"modified:{_window_id(spec.modified_after)}")
    selected.add(f"created:{_window_id(spec.created_after)}")
    return selected, excluded


def _size_id(value: int | None) -> str:
    if value is None:
        return "any"
    return next((i for i, _l, v in _SIZES if v == value), "any")


def _window_id(value: dt.date | None) -> str:
    if value is None:
        return "any"
    days = (dt.date.today() - value).days
    return next((i for i, _l, d in _WINDOWS if d is not None and abs(d - days) <= 1), "any")


def apply_selection(
    spec: FilterSpec, selected: set[str] | frozenset[str], excluded: set[str] | frozenset[str]
) -> tuple[FilterSpec, bool, bool]:
    """``(spec, respect_gitignore, respect_fndignore)`` matching the tree."""
    kinds = tuple(sorted(i.removeprefix("kind:") for i in selected if i.startswith("kind:")))
    keep = tuple(sorted(i.removeprefix("tag:") for i in selected if i.startswith("tag:")))
    tags = tuple(sorted(i.removeprefix("tag:") for i in excluded if i.startswith("tag:")))
    today = dt.date.today()

    max_size = next(
        (v for i, _l, v in _SIZES if f"size:{i}" in selected and v is not None),
        None,
    )
    bounds: dict[str, dt.date | None] = {}
    for field_name in ("modified", "created"):
        days = next(
            (d for i, _l, d in _WINDOWS if f"{field_name}:{i}" in selected and d is not None),
            None,
        )
        bounds[f"{field_name}_after"] = today - dt.timedelta(days=days) if days else None

    updated = replace(
        spec,
        kinds=kinds,
        include_tags=keep,
        exclude_tags=tags,
        max_size=max_size,
        modified_after=bounds["modified_after"],
        created_after=bounds["created_after"],
    )
    return updated, "ignore:git" in selected, "ignore:fnd" in selected
