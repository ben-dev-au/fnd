"""The filter set as the branches a tree shows, and back again.

Pure model: no Textual import, so the mapping between a
:class:`~fnd.filters.model.FilterSpec` and what the user sees is testable on
its own. The screen renders these groups and hands the selection back.

Date and size branches offer named windows rather than a typed value, and a
window resolves to an absolute bound when it is chosen: an index must not
change what it holds as the clock moves.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, replace

from fnd.filters.model import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.kinds import ALL_KIND_IDS, CATEGORIES, KIND_BY_ID, KINDS_IN_CATEGORY

# Source-neutral labels: there are no Finder tags off macOS, and the branch
# should not name an OS the user is not on.
TAG_SOURCE_LABELS: dict[str, str] = {
    "os": "System tags",
    "frontmatter": "Note tags (YAML)",
}

__all__ = [
    "BOUND_LEGEND",
    "BRANCHES",
    "IGNORE_LEGEND",
    "KINDS_LEGEND",
    "LEGEND",
    "RULES_LEGEND",
    "apply_selection",
    "custom_ids",
    "selection_for",
    "spec_branches",
]

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
    ("1mb", "Up to 1 MB", 1_000_000),
    ("10mb", "Up to 10 MB", 10_000_000),
    ("50mb", "Up to 50 MB", 50_000_000),
    ("200mb", "Up to 200 MB", 200_000_000),
)

BRANCHES = ("kinds", "tags", "ignore", "size", "modified", "created")

# Shown above the tree for the branches this reading holds for.
LEGEND = "⊘  never index these   ●  index ONLY these   ◐  some of these   ○  no rule"

#: Branches where that reading is FALSE. On the ignore branch ● means "obey
#: this file", which indexes FEWER files, and ○ means more: the opposite of
#: what the shared line claims. A branch names its own meaning or inherits.
IGNORE_LEGEND = "●  obey this file   ○  ignore it   (obeying one indexes fewer files)"
RULES_LEGEND = "⏎  opens a branch, then the editor for a rule in it"
#: `kinds` is an include-only list in the model, so `⊘` is unreachable here. The
#: shared line would send a user looking for an exclude state that does not exist,
#: and allow-listing everything else instead drops every file type added later.
KINDS_LEGEND = "●  index ONLY these   ◐  some of these   ○  no rule   ⊘  needs a typed rule (t)"
#: Size and date branches are radio: one bound is in force or none. Neither ⊘
#: nor ◐ can occur on them, and "index ONLY these" is the wrong sentence for
#: "Up to 1 MB".
BOUND_LEGEND = "●  the bound in force   ○  no bound   (one at a time)"


@dataclass(frozen=True, slots=True)
class Branch:
    """One collapsible row: its leaves, its sub-branches and how they behave.

    ``empty_label`` says what the branch means with nothing switched on;
    without it, "no file type ticked" reads as *nothing is indexed*.
    """

    id: str
    label: str
    mode: str
    items: tuple[tuple[str, ...], ...] = ()
    """(item id, label) or (item id, label, key). The key says what makes
    two leaves the same thing to a user, where the label carries more: a tag
    row shows its file count, so the label alone would count one tag twice."""
    groups: tuple[Branch, ...] = ()
    empty_label: str = ""
    full_label: str = ""
    noun: str = ""
    legend: str = ""
    """What the glyphs mean here, where the shared line would be wrong."""
    name_leaves: bool = False
    """Name what is on rather than counting it. See :class:`ToggleGroup`."""
    elsewhere: str = ""
    """A bound on this dimension that this branch cannot show. Without it the
    row reads `○ Maximum file size (Any size)` while a minimum is filtering:
    two rows contradicting each other on the same frame."""
    complete: bool = True
    """False while the leaves are still being discovered. A roll-up that says
    "all of these" is a claim about leaves the branch has not seen yet."""


def _offers_every_kind(items: list[tuple[str, str, str]]) -> bool:
    """Whether the tree is showing the whole registry rather than a sample."""
    # The ids carry the widget prefix; ALL_KIND_IDS does not.
    return {k.removeprefix("kind:") for _cat, k, _label in items} >= set(ALL_KIND_IDS)


def _kind_items(sample: SourceSample | None) -> list[tuple[str, str, str]]:
    """(category id, kind id, label) for every kind, counted where the sample saw one.

    Every kind is offered, whatever the sample saw. A picker that shows only
    today's types has to be revisited as the corpus grows, and a filter set
    once should keep holding: the counts say what is there now, the rows say
    what can be chosen.
    """
    out: list[tuple[str, str, str]] = []
    for cat in CATEGORIES:
        for kind in KINDS_IN_CATEGORY.get(cat.id, ()):
            spec = KIND_BY_ID.get(kind)
            if spec is None:
                continue
            # What the source's other rules admit, not the raw disk tally, or
            # `Markdown · 3` sits above `Tags (no_index excluded)` while the index
            # holds 2.
            raw = sample.kinds if sample else {}
            counts = sample.kinds_kept if sample and sample.gated else raw
            count = counts.get(kind, 0) or 0
            # `· 0` where the source HAS files of this kind and the rules keep
            # none is the loudest thing the pane can say. A kind the source has
            # none of stays bare: every kind is offered, and forty zeros say nothing.
            counted = (raw.get(kind, 0) or 0) > 0
            suffixes = "/".join(spec.suffixes)
            label = f"{spec.label} ({suffixes})"
            out.append((cat.id, f"kind:{kind}", f"{label}  ·  {count}" if counted else label))
    return out


def _facts_in_free_text(spec: FilterSpec) -> frozenset[str]:
    """Facts named by clauses no picker owns.

    A branch can only light up for the shape it understands: `file.size <= N`
    fills `max_size`, while `< N` and `> N` stay raw text. Without this the
    size row read `○ (Any size)` while a live rule took the walk from ten
    files to six, so the branch says a rule of its own is in the text form
    rather than claiming there is none.
    """
    import contextlib

    from fnd.filter_dsl import parse, referenced_fields

    out: set[str] = set()
    for text in (spec.expression, *spec.raw):
        if not (text or "").strip():
            continue
        # Unparseable text is the text editor's problem to report; a branch
        # simply learns nothing from it and says nothing extra.
        with contextlib.suppress(Exception):
            out |= set(referenced_fields(parse(text)))
    return frozenset(out)


def _beyond_the_pickers(spec: FilterSpec) -> tuple[tuple[str, str], ...]:
    """Bounds no branch can show, as (field, one-line description)."""
    out: list[tuple[str, str]] = []
    if spec.min_size is not None:
        out.append(("min_size", f"At least {_human_size(spec.min_size)}"))
    for field, verb in (("modified", "Modified"), ("created", "Created")):
        bound = getattr(spec, f"{field}_before")
        if bound is not None:
            out.append((f"{field}_before", f"{verb} before {bound.isoformat()}"))
    return tuple(out)


def _elsewhere_note(has_bound: bool, bound_says: str, in_free_text: bool) -> str:
    """What a branch adds about rules on its dimension that it cannot show."""
    parts = [
        text
        for flag, text in ((has_bound, bound_says), (in_free_text, "a rule is typed below"))
        if flag
    ]
    return " · ".join(parts)


def _rule_label(name: str, value: str) -> str:
    """A typed rule's row: its current text, or that it has none."""
    text = (value or "").strip()
    return f"{name}   {text}" if text else f"{name}   (none)"


def _tag_branch(
    spec: FilterSpec, sample: SourceSample | None, free: frozenset[str] = frozenset()
) -> Branch | None:
    """Tags, one sub-branch per source.

    A Finder tag and a note's ``tags:`` entry that share a word are different
    statements about a file, so they get different rows, the shape
    :class:`~fnd.tag_query.TagFilter` already uses on the query side.
    """
    sources = [s for s in TAG_SOURCE_LABELS if s in (sample.tags if sample else {})]
    for source in (*spec.include_tags, *spec.exclude_tags):
        if source not in sources and source in TAG_SOURCE_LABELS:
            sources.append(source)
    groups: list[Branch] = []
    for source in sources:
        seen = [
            (f"tag:{source}:{v}", f"{v}  ({c})", v)
            for v, c in (sample.tags_for(source) if sample else [])
        ]
        configured = set(spec.tag_includes.get(source, ())) | set(spec.tag_excludes.get(source, ()))
        for tag in sorted(configured):
            if not any(i[0] == f"tag:{source}:{tag}" for i in seen):
                seen.append((f"tag:{source}:{tag}", tag, tag))
        # Whatever is switched on sorts first, so the branch shows what it is
        # doing without the user scrolling a corpus-length list to find it.
        active = {f"tag:{source}:{t}" for t in configured}
        items = [i for i in seen if i[0] in active] + [i for i in seen if i[0] not in active]
        if items:
            groups.append(
                Branch(
                    f"tags:{source}",
                    TAG_SOURCE_LABELS[source],
                    "cycle",
                    tuple(items),
                    complete=sample is not None,
                )
            )
    if not groups:
        return None
    # A typed edit that drops `no_index` must say so here, or the branch goes on
    # reading "any tag".
    typed = any(f.startswith("file.tags") for f in free)
    note = _elsewhere_note(False, "", typed)
    if len(groups) == 1:
        # Collapse the single source's rows into this branch, but KEEP the name:
        # the source's label would rename "Tags" to "Note tags (YAML)" as a side
        # effect of clearing a rule about the other source.
        return replace(groups[0], id="tags", label="Tags", empty_label="any tag", elsewhere=note)
    return Branch(
        "tags",
        "Tags",
        "cycle",
        groups=tuple(groups),
        empty_label="any tag",
        noun="tags",
        complete=sample is not None,
        elsewhere=note,
    )


def spec_branches(
    spec: FilterSpec,
    sample: SourceSample | None = None,
    keep_custom: Mapping[str, str] | None = None,
) -> list[Branch]:
    """The branches a filter screen should render for ``spec``.

    ``keep_custom`` names custom bounds to keep offering per branch even when
    the spec no longer holds them, so picking a preset over a custom value is
    reversible without retyping it in the text view.
    """
    branches: list[Branch] = []

    kinds = _kind_items(sample)
    # Which dimensions a typed rule names, so a branch that cannot show one
    # says so instead of claiming there is no rule.
    free = _facts_in_free_text(spec)
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
                # A typed rule on this dimension is the only way to exclude a
                # type, so the branch that cannot show it read "every type"
                # four lines above an expression excluding one.
                elsewhere=_elsewhere_note(False, "", "file.kind" in free),
                empty_label="every type",
                # Only claim "every type" when every type was offered; on a
                # sampled list, all-ticked means those types and says so.
                full_label="every type" if _offers_every_kind(kinds) else "",
                noun="types",
                legend=KINDS_LEGEND,
            )
        )

    tag_branch = _tag_branch(spec, sample, free)
    if tag_branch is not None:
        branches.append(tag_branch)

    branches.append(
        Branch(
            "ignore",
            "Obey ignore files",
            "multi",
            (("ignore:git", ".gitignore"), ("ignore:fnd", ".fndignore")),
            empty_label="none",
            noun="files",
            name_leaves=True,
            legend=IGNORE_LEGEND,
        )
    )
    # Rows stay ordered by the bound they set, so a custom one lands among the
    # presets rather than ahead of them.
    sized = [(-1 if v is None else v, f"size:{i}", lbl) for i, lbl, v in _SIZES]
    for custom in _custom_offers("size", spec, keep_custom):
        value = int(custom.removeprefix(f"{CUSTOM}:"))
        sized.append((value, f"size:{custom}", f"Up to {_human_size(value)}"))
    size_items = [(i, lbl) for _k, i, lbl in sorted(sized)]
    branches.append(
        Branch(
            "size",
            "Maximum file size",
            "radio",
            tuple(size_items),
            elsewhere=_elsewhere_note(
                spec.min_size is not None, "a minimum is set", "file.size" in free
            ),
            legend=BOUND_LEGEND,
        )
    )
    for field_name, label in (("modified", "Modified within"), ("created", "Created within")):
        # A window resolves to an absolute date the moment it is picked, so the
        # row names that date: "Last 7 days" alone reads as rolling.
        dated = [
            (
                -1 if d is None else d,
                f"{field_name}:{i}",
                lbl if d is None else f"{lbl}, from {_window_start(d).isoformat()}",
            )
            for i, lbl, d in _WINDOWS
        ]
        for custom in _custom_offers(field_name, spec, keep_custom):
            since = custom.removeprefix(f"{CUSTOM}:")
            days = (dt.date.today() - dt.date.fromisoformat(since)).days
            dated.append((days, f"{field_name}:{custom}", f"Since {since}"))
        items = [(i, lbl) for _k, i, lbl in sorted(dated)]
        branches.append(
            Branch(
                field_name,
                label,
                "radio",
                tuple(items),
                elsewhere=_elsewhere_note(
                    getattr(spec, f"{field_name}_before") is not None,
                    "an upper bound is set",
                    f"file.{field_name}" in free,
                ),
                legend=BOUND_LEGEND,
            )
        )
    beyond = _beyond_the_pickers(spec)
    if beyond:
        # These dimensions have no picker: "Maximum file size" cannot hold a
        # minimum, and "Modified within" cannot hold an upper bound. Without a
        # row the tree looked complete while they filtered.
        branches.append(
            Branch(
                "beyond",
                f"Set in the text form  ({len(beyond)})",
                "actions",
                tuple((f"beyond:{name}", text) for name, text in beyond),
                legend=RULES_LEGEND,
            )
        )
    # An actions branch carries no marker, so a collapsed one says in its label
    # whether a rule is set. One row per clause in `raw`, so a second typed
    # clause is not counted as "1 set" under the first one's name.
    raw_rows = tuple(
        (f"rule:raw:{i}", _rule_label("Typed rule", text))
        for i, text in enumerate(spec.raw)
        if (text or "").strip()
    )
    set_rules = sum(1 for v in (spec.frontmatter, spec.expression) if (v or "").strip())
    set_rules += len(raw_rows)
    branches.append(
        Branch(
            "rules",
            "Rules you type" + (f"  ({set_rules} set)" if set_rules else "  (none)"),
            "actions",
            (
                ("rule:frontmatter", _rule_label("Frontmatter rule", spec.frontmatter)),
                ("rule:expression", _rule_label("Custom rule", spec.expression)),
                *raw_rows,
            ),
            legend=RULES_LEGEND,
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
    selected: set[str] = {
        f"tag:{source}:{t}" for source, tags in spec.tag_includes.items() for t in tags
    }
    excluded: set[str] = {
        f"tag:{source}:{t}" for source, tags in spec.tag_excludes.items() for t in tags
    }
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


def _tags_from(ids: set[str] | frozenset[str]) -> dict[str, tuple[str, ...]]:
    """``tag:<source>:<value>`` item ids back into the source-keyed mapping.

    Split at most twice: a tag value may itself contain a colon.
    """
    out: dict[str, list[str]] = {}
    for item in ids:
        if not item.startswith("tag:"):
            continue
        _, source, tag = item.split(":", 2)
        out.setdefault(source, []).append(tag)
    return {source: tuple(sorted(tags)) for source, tags in out.items()}


CUSTOM = "custom"
"""Prefix for the row shown when a bound is not one of the offered options.

The options set a bound; the bound itself is an arbitrary number or date.
Mapping an unmatched value back to "any" would let an unrelated toggle delete
it, and make a window stop matching two days after it was picked.

The id carries the value (``size:custom:5000000``) rather than meaning
"whatever the spec holds". The tree's labels are built when it is rebuilt
while a selection is resolved as it is made, so an id that referred to the
current spec would resolve a row still reading "Up to 5 MB" to a bound the
user had since changed.
"""


def _human_size(value: int) -> str:
    for unit, step in (("GB", 1_000_000_000), ("MB", 1_000_000), ("kB", 1_000)):
        if value >= step:
            return f"{value / step:g} {unit}"
    return f"{value} bytes"


def _size_id(value: int | None) -> str:
    if value is None:
        return "any"
    return next((i for i, _l, v in _SIZES if v == value), f"{CUSTOM}:{value}")


def _window_start(days: int) -> dt.date:
    """The absolute date a window resolves to today."""
    return dt.date.today() - dt.timedelta(days=days)


def _window_id(value: dt.date | None) -> str:
    if value is None:
        return "any"
    days = (dt.date.today() - value).days
    return next(
        (i for i, _l, d in _WINDOWS if d is not None and abs(d - days) <= 1),
        f"{CUSTOM}:{value.isoformat()}",
    )


def custom_ids(spec: FilterSpec) -> dict[str, str]:
    """Branch id → the custom bound it currently holds, if any."""
    holds = {
        "size": _size_id(spec.max_size),
        "modified": _window_id(spec.modified_after),
        "created": _window_id(spec.created_after),
    }
    return {branch: i for branch, i in holds.items() if i.startswith(f"{CUSTOM}:")}


def _custom_offers(
    branch: str, spec: FilterSpec, keep: Mapping[str, str] | None
) -> tuple[str, ...]:
    """The custom rows a branch shows: its own bound first, then a kept one."""
    ids = (custom_ids(spec).get(branch), (keep or {}).get(branch))
    return tuple(dict.fromkeys(i for i in ids if i))


def _custom_value(selected: set[str] | frozenset[str], field: str) -> str | None:
    """The value carried by a selected ``<field>:custom:<value>`` id."""
    prefix = f"{field}:{CUSTOM}:"
    return next((i[len(prefix) :] for i in selected if i.startswith(prefix)), None)


def apply_selection(
    spec: FilterSpec,
    selected: set[str] | frozenset[str],
    excluded: set[str] | frozenset[str],
    offered: set[str] | frozenset[str] | None = None,
) -> tuple[FilterSpec, bool, bool]:
    """``(spec, respect_gitignore, respect_fndignore)`` matching the tree.

    ``offered`` is the set of kind ids the tree actually showed. The tree lists
    only the types a source contains, so ticking every visible box is the user
    saying "all of them" even though the registry has more.
    """
    picked = {i.removeprefix("kind:") for i in selected if i.startswith("kind:")}
    # Every box ticked means "every type", not today's list, which would never
    # index a PDF added tomorrow while an untouched branch does, both reading
    # "all types". `AddCollectionWizard._set_includes` collapses the same way.
    shown = {i.removeprefix("kind:") for i in offered if i.startswith("kind:")} if offered else None
    # Only when the tree offered EVERY type does ticking them all mean "no
    # restriction": on a homogeneous folder a genuine `kinds = ["md"]` is already
    # "all of them", and collapsing it would delete the rule with no keypress.
    everything = shown if shown is not None else set(ALL_KIND_IDS)
    complete = shown is None or shown >= set(ALL_KIND_IDS)
    kinds = () if picked and complete and picked >= everything else tuple(sorted(picked))
    keep = _tags_from(selected)
    tags = _tags_from(excluded)
    today = dt.date.today()

    custom_size = _custom_value(selected, "size")
    if custom_size is not None:
        max_size = int(custom_size)
    else:
        max_size = next(
            (v for i, _l, v in _SIZES if f"size:{i}" in selected and v is not None),
            None,
        )
    bounds: dict[str, dt.date | None] = {}
    for field_name in ("modified", "created"):
        custom_date = _custom_value(selected, field_name)
        if custom_date is not None:
            bounds[f"{field_name}_after"] = dt.date.fromisoformat(custom_date)
            continue
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
