"""The text view of a :class:`FilterSpec`, and the way back.

The rows and the expression are two views of one filter set: editing either
must show up in the other. Rendering is canonical; parsing recognises the
clause shapes the rows can edit and keeps everything else verbatim.

A clause the user typed as free text that happens to match a row's shape
becomes that row. That is the point — it is how typing
``file.kind in ['pdf']`` makes the File type row show PDF — so the round-trip
guarantee is that the *filter behaves the same*, not that a value stays in the
field it started in.
"""

from __future__ import annotations

import datetime as dt
import re

from fnd.filter_dsl import And, Compare, FieldIn, FilterError, In, Not, Or, referenced_fields
from fnd.filter_dsl import parse as parse_dsl
from fnd.filters.dimensions import dimension
from fnd.filters.model import FilterSpec

__all__ = ["parse", "render"]

_BARE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-.]*\Z")


def _tag_sources(fact: str) -> tuple[str, ...] | None:
    """Which tag sources a ``file.tags.*`` fact names, or None if it is not one.

    Derived from the registry so a new tag source needs no edit here.
    ``file.tags.all`` names every source, which is how a bare config list is
    expanded too.
    """
    from fnd.tags import TAG_PROVIDERS

    if fact == "file.tags.all":
        return tuple(TAG_PROVIDERS)
    source = fact.removeprefix("file.tags.")
    return (source,) if source in TAG_PROVIDERS else None


_COMPARISONS: dict[tuple[str, str], str] = {
    ("file.size", ">="): "min_size",
    ("file.size", "<="): "max_size",
    ("file.created", ">="): "created_after",
    ("file.created", "<="): "created_before",
    ("file.modified", ">="): "modified_after",
    ("file.modified", "<="): "modified_before",
}
_BY_FIELD = {v: k for k, v in _COMPARISONS.items()}


def _field(name: str) -> str:
    if _BARE.match(name):
        return name
    # Quoted field names need the same escaping values get, or a name
    # carrying a double quote re-emits as text that will not parse back.
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    return str(value)


def _unparse(node: object, *, depth: int = 0) -> str:
    if isinstance(node, Compare):
        return f"{_field(node.field)} {node.op} {_value(node.value)}"
    if isinstance(node, In):
        op = "not in" if node.negated else "in"
        return f"{_value(node.value)} {op} {_field(node.field)}"
    if isinstance(node, FieldIn):
        op = "not in" if node.negated else "in"
        return f"{_field(node.field)} {op} [{', '.join(_value(v) for v in node.values)}]"
    if isinstance(node, Not):
        return f"NOT ({_unparse(node.operand, depth=depth + 1)})"
    if isinstance(node, (And, Or)):
        keyword = "AND" if isinstance(node, And) else "OR"
        text = f"{_unparse(node.left, depth=depth + 1)} {keyword} {_unparse(node.right, depth=depth + 1)}"
        return f"({text})" if depth else text
    return ""


def render(spec: FilterSpec) -> str:
    """The whole filter set as one expression.

    Every clause is parenthesised when more than one is joined: a raw clause
    containing ``OR`` would otherwise capture its neighbours and widen the
    filter it was meant to narrow.
    """
    clauses: list[str] = []
    if spec.kinds:
        clauses.append(f"file.kind in [{', '.join(_value(k) for k in spec.kinds)}]")
    # Rendered by the dimensions themselves: they own how a tag names its
    # source, and this second copy drifted — the qualifier leaked into the tag
    # value, so 'os:archive' re-parsed as 'os:os:archive'.
    for dim_id in ("include_tags", "exclude_tags"):
        text = dimension(dim_id).render(getattr(spec, dim_id))
        if text:
            clauses.append(text)
    for field_name, (fact, op) in _BY_FIELD.items():
        value = getattr(spec, field_name, None)
        if value is not None:
            clauses.append(f"{fact} {op} {_value(value)}")
    if spec.frontmatter:
        # The kind scope the compiler applies is written out, so the text says
        # what the rule actually does rather than silently dropping it.
        # No escape clause: a rule that asks about a frontmatter field is
        # skipped for a file that has no frontmatter, so the scope needs no
        # spelling out. Writing it into the text produced a clause that read
        # as excluding Markdown and had no counterpart in any other tool.
        clauses.append(spec.frontmatter)
    if spec.expression:
        clauses.append(spec.expression)
    clauses.extend(spec.raw)
    if not clauses:
        return ""
    if len(clauses) == 1:
        return clauses[0]
    return " AND ".join(f"({c})" for c in clauses)


def _split_and(node: object) -> list[object]:
    if isinstance(node, And):
        return _split_and(node.left) + _split_and(node.right)
    return [node]


def _is_note_escape(node: object) -> bool:
    """The kind-scope clause older versions wrote around a frontmatter rule."""
    inner: object = node
    negated = False
    if isinstance(node, Not):
        inner, negated = node.operand, True
    if not isinstance(inner, FieldIn) or inner.field != "file.kind":
        return False
    return inner.negated != negated


def _match_frontmatter(node: object) -> str | None:
    """A clause that only a file with frontmatter can answer.

    Either it asks about frontmatter fields alone — no ``file.*`` anywhere —
    or it is the kind-scoped form an older version wrote, which is unwrapped
    so a config written then still round-trips.
    """
    if isinstance(node, Or):
        if _is_note_escape(node.left):
            return _unparse(node.right)
        if _is_note_escape(node.right):
            return _unparse(node.left)
    fields = referenced_fields(node)
    if fields and not any(f.startswith("file.") for f in fields):
        return _unparse(node)
    return None


def _tag_in(node: object) -> dict[str, tuple[str, ...]] | None:
    """``'x' in file.tags.<source>`` as ``{source: (tag,)}``.

    Keyed by source, so a rule about Finder tags does not come back as a rule
    about every tag.
    """
    if not isinstance(node, In) or node.negated:
        return None
    sources = _tag_sources(node.field)
    if sources is None:
        return None
    return {s: (str(node.value),) for s in sources}


def _merge_tags(
    a: dict[str, tuple[str, ...]], b: dict[str, tuple[str, ...]]
) -> dict[str, tuple[str, ...]]:
    out = {k: tuple(v) for k, v in a.items()}
    for source, tags in b.items():
        out[source] = tuple(dict.fromkeys(out.get(source, ()) + tags))
    return out


def _include_tags(node: object) -> dict[str, tuple[str, ...]] | None:
    """A single tag membership, or an ``OR`` chain of nothing else."""
    single = _tag_in(node)
    if single is not None:
        return single
    if not isinstance(node, Or):
        return None
    left, right = _include_tags(node.left), _include_tags(node.right)
    return None if left is None or right is None else _merge_tags(left, right)


def _recognise(node: object) -> tuple[str, object] | None:
    if isinstance(node, FieldIn) and node.field == "file.kind" and not node.negated:
        return "kinds", tuple(str(v) for v in node.values)
    included = _include_tags(node)
    if included is not None:
        return "include_tags", included
    if isinstance(node, Not):
        excluded = _tag_in(node.operand)
        if excluded is not None:
            return "exclude_tags", excluded
    if isinstance(node, Compare):
        field_name = _COMPARISONS.get((node.field, node.op))
        if field_name is not None:
            return field_name, node.value
    scoped = _match_frontmatter(node)
    if scoped is not None:
        return "frontmatter", scoped
    return None


def parse(text: str) -> FilterSpec:
    """Text back into a spec. Raises :class:`FilterError` if it does not parse.

    Clauses the rows cannot express land in ``expression`` (the first) and
    ``raw`` (any further ones), so nothing the user wrote is dropped.
    """
    from dataclasses import replace

    stripped = text.strip()
    if not stripped:
        return FilterSpec()
    updates: dict[str, object] = {}
    tags: dict[str, tuple[str, ...]] = {}
    leftover: list[str] = []
    for clause in _split_and(parse_dsl(stripped)):
        found = _recognise(clause)
        if found is None:
            leftover.append(_unparse(clause) or stripped)
            continue
        name, value = found
        if name == "include_tags":
            updates["include_tags"] = _merge_tags(
                updates.get("include_tags", {}),  # type: ignore[arg-type]
                value,  # type: ignore[arg-type]
            )
        elif name == "exclude_tags":
            tags = _merge_tags(tags, value)  # type: ignore[arg-type]
        else:
            updates[name] = value
    if tags:
        updates["exclude_tags"] = tags
    if leftover:
        updates["expression"] = leftover[0]
        if len(leftover) > 1:
            updates["raw"] = tuple(leftover[1:])
    return replace(FilterSpec(), **updates)  # type: ignore[arg-type]


def parse_or_error(text: str) -> tuple[FilterSpec | None, FilterError | None]:
    """Non-raising variant, for a live-validating editor."""
    try:
        return parse(text), None
    except FilterError as e:
        return None, e
