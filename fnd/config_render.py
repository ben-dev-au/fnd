"""Render a :class:`~fnd.config.Config` as the canonical ``config.toml``.

The file fnd writes is generated from the models, so every key arrives with
its description and its default, and the documentation cannot drift from the
schema. Two consequences worth knowing:

* A user's own comments and key order are replaced on the next write. The
  region between the ``PRESERVE_BEGIN``/``PRESERVE_END`` markers is the
  exception; it is carried through verbatim, and is where an annotation or a
  parked, disabled setting belongs.
* ``SourceFilters`` mirrors ``DefaultFilters`` field for field, so its prose is
  resolved from there rather than written twice.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from fnd.config import Config

WIDTH = 78
PRESERVE_BEGIN = "# >>> notes: kept verbatim when fnd rewrites this file"
PRESERVE_END = "# <<< notes"

#: ``[defaults]`` in reading order. Every field must appear in exactly one
#: group; ``test_config_render`` fails if one is added and not placed.
DEFAULT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Search and results",
        (
            "collection",
            "result_limit",
            "debounce_ms",
            "sections_score_threshold",
            "sections_per_file_max",
            "fuzzy_enabled",
            "fuzzy_min_term_chars",
        ),
    ),
    (
        "Preview pane",
        (
            "preview_chunks",
            "preview_decode_workers",
            "preview_warm_margin",
            "preview_load_debounce_ms",
            "preview_prefetch_count",
            "preview_scroll_animation",
            "scrollbar_match_highlight",
            "multicolour_highlights",
            "render_mermaid",
        ),
    ),
    ("Tags", ("tag_sources", "tag_frontmatter_keys")),
    (
        "Indexing",
        (
            "indexer_auto_resume",
            "cache_at_index_time",
            "cloud_fetch_timeout_s",
            "skip_junk_dirs",
            "extra_junk_dirs",
        ),
    ),
    ("Interface", ("drill_summary_mode",)),
)

_SOURCE_ORDER = (
    "path",
    "includes",
    "excludes",
    "follow_symlinks",
    "frontmatter_filter",
    "app",
)


def extract_preserved(text: str) -> str:
    """The user's kept region, or empty. Markers themselves are not returned."""
    start = text.find(PRESERVE_BEGIN)
    if start < 0:
        return ""
    end = text.find(PRESERVE_END, start)
    if end < 0:
        # No end marker: stop at the first line that is neither blank nor a
        # comment; running to end of file would preserve the generated prose
        # below forever and grow the file on every write.
        rest = text[start + len(PRESERVE_BEGIN) :].split("\n")
        stop = next(
            (i for i, ln in enumerate(rest) if ln.strip() and not ln.lstrip().startswith("#")),
            len(rest),
        )
        body = "\n".join(rest[:stop])
    else:
        body = text[start + len(PRESERVE_BEGIN) : end]
    if body.startswith("\n"):
        body = body[1:]
    if body.endswith("\n"):
        body = body[:-1]
    # Live TOML in the block is real config: it is parsed and re-rendered in
    # its own table, so keeping the text here would declare that table twice.
    kept = [ln for ln in body.split("\n") if not ln.strip() or ln.lstrip().startswith("#")]
    return "\n".join(kept)


def _live_fields(model: type[BaseModel]) -> set[str]:
    """Field names a generated config may mention. A deprecated field is
    honoured on load and retired on the next write, so advertising it would
    teach a shape we are trying to remove."""
    return {n for n, info in model.model_fields.items() if not info.deprecated}


def _default_of(model: type[BaseModel], name: str) -> Any:
    """A field's default, read from the schema. Constructing a blank instance
    instead fails for any model whose required fields carry validators."""
    info = model.model_fields[name]
    return None if info.is_required() else info.get_default(call_default_factory=True)


def _describe(model: type[BaseModel], name: str) -> str:
    info = model.model_fields.get(name)
    if info is not None and info.description:
        return info.description
    from fnd.config import DefaultFilters, SourceFilters

    if model is SourceFilters:
        mirror = DefaultFilters.model_fields.get(name)
        if mirror is not None and mirror.description:
            return mirror.description
    return ""


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        # Digit separators for anything a person would otherwise miscount.
        return f"{value:_}" if abs(value) >= 10_000 else str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Path):
        return path_value(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "{ " + ", ".join(f"{key(k)} = {toml_value(v)}" for k, v in value.items()) + " }"
    return _quoted(str(value))


def under_home(path: Path) -> str:
    """A path re-tilded under the home directory."""
    home = Path.home()
    if path == home:
        return "~"
    if path.is_relative_to(home):
        return "~/" + path.relative_to(home).as_posix()
    return str(path)


def path_value(path: Path) -> str:
    """A path as TOML, re-tilded. The model expands `~` on load, so without
    this every generated config bakes in one machine's absolute paths and
    stops being portable or shareable."""
    return toml_value(under_home(path))


_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _quoted(text: str) -> str:
    """A TOML basic string. A raw newline or control character would end the
    string early and leave the file unparseable."""
    out = []
    for ch in text:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ch < " " or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def key(name: str) -> str:
    """A table or key name as TOML: bare where it can be, quoted otherwise;
    a collection may be called "Soft Eng Textbooks"."""
    if name and all(c.isascii() and (c.isalnum() or c in "-_") for c in name):
        return name
    return toml_value(name)


def _comment(text: str) -> list[str]:
    # Descriptions are source docstrings and carry RST double-backticks;
    # a config file wants plain prose.
    text = text.replace("``", "`")
    out: list[str] = []
    for para in text.split("\n"):
        line = ""
        for word in para.split():
            if line and len(line) + len(word) + 3 > WIDTH:
                out.append(f"# {line}")
                line = word
            else:
                line = f"{line} {word}".strip()
        out.append(f"# {line}" if line else "#")
    return out


def _divider(title: str) -> list[str]:
    return ["", f"# ── {title} " + "─" * max(3, WIDTH - len(title) - 5), ""]


def _field(model: type[BaseModel], name: str, value: Any) -> list[str]:
    """One key: its prose, then the key itself; live if set, commented with
    the default if not, so every setting doubles as its own example."""
    default = _default_of(model, name)
    out = _comment(_describe(model, name))
    if value is not None and value != default:
        out.append(f"{name} = {toml_value(value)}")
    elif default is None:
        out.append(f"# {name} =")
    else:
        out.append(f"# {name} = {toml_value(default)}")
    out.append("")
    return out


def _model_body(model: BaseModel, names: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for name in names:
        out += _field(type(model), name, getattr(model, name))
    return out


def _set_fields(model: BaseModel, order: tuple[str, ...] = ()) -> list[str]:
    """Only what differs from the default; repeated tables stay readable."""
    # Declaration order, never a set: set order follows the hash seed, so
    # ensure_current would rewrite the file on every launch. Deprecated fields
    # are still written when set, so a missing migration never drops a rule.
    declared = list(type(model).model_fields)
    names = [*(n for n in order if n in declared), *(n for n in declared if n not in order)]
    out: list[str] = []
    for name in names:
        # Straight from the stored values: going through the attribute warns
        # for a deprecated field, which this deliberately still writes. `.get`
        # with a fallback would evaluate that attribute access anyway.
        stored = model.__dict__
        value = stored[name] if name in stored else getattr(model, name, None)
        # `None` is absent; an explicit `[]` is an override to nothing, which
        # `SourceFilters` uses to opt a source out of an inherited rule.
        if value is None or isinstance(value, BaseModel):
            continue
        # Nested models get their own tables, emitted by the caller.
        if isinstance(value, (list, tuple)) and value and isinstance(value[0], BaseModel):
            continue
        if name != "path" and value == _default_of(type(model), name):
            continue
        out.append(f"{name} = {toml_value(value)}")
    return out


def _example_block(table: str, model: BaseModel) -> list[str]:
    """A whole table shown commented out, so an unused feature still says what
    it offers and can be switched on by uncommenting."""
    out = [f"# [{table}]"]
    for name in type(model).model_fields:
        value = getattr(model, name)
        out.append(f"# {name} = {toml_value(value)}" if value is not None else f"# {name} =")
    return out


def render_config(config: Config, *, preserved: str = "", version: int | None = None) -> str:
    from fnd.config import (
        DefaultFilters,
        RankingProfileConfig,
        SourceConfig,
    )
    from fnd.config_migrations import CONFIG_VERSION

    # Defaulting to a literal let the constant and the renderer drift, and they
    # did the moment a second migration landed.
    if version is None:
        version = CONFIG_VERSION

    lines = _comment(
        "fnd configuration.\n"
        "Generated by fnd: every key is listed with what it does and its "
        "default. A commented-out line is the default; uncomment it to "
        "change it.\n"
        "Comments outside the notes block below are regenerated on each save."
    )
    lines += ["", f"config_version = {version}", ""]

    lines += [PRESERVE_BEGIN]
    lines += (preserved or "# (your notes and parked settings go here)").split("\n")
    lines += [PRESERVE_END]

    lines += _divider("Defaults")
    lines.append("[defaults]")
    lines.append("")
    for title, names in DEFAULT_GROUPS:
        lines.append(f"# {title}")
        lines.append("")
        lines += _model_body(config.defaults, names)

    lines += _divider("Index filters inherited by every source")
    lines += _comment(
        "A file an ignore file or a tag excludes never enters the index, and "
        "the next update prunes any already in it. A source's own filters "
        "table overrides these field by field."
    )
    lines += ["", "[defaults.filters]", ""]
    lines += _model_body(config.defaults.filters, tuple(DefaultFilters.model_fields))

    lines += _divider("Collections")
    lines += _comment(
        "A collection groups source folders searched together. A source may "
        "also carry: " + ", ".join(sorted(_live_fields(SourceConfig) - {"path"})) + "."
    )
    for name, collection in config.collections.items():
        # The table header is emitted even when the collection has no sources,
        # or an empty collection would vanish on the next write.
        lines += ["", f"[collections.{key(name)}]"]
        lines += _set_fields(collection)
        for source in collection.sources:
            lines += ["", f"[[collections.{key(name)}.sources]]"]
            lines += _set_fields(source, _SOURCE_ORDER)
            if source.filters is not None:
                body = _set_fields(source.filters)
                if body:
                    lines += ["", f"[collections.{key(name)}.sources.filters]", *body]

    lines += _divider("Ranking profiles")
    lines += _comment(
        "A profile tunes the scorer. Name one in a collection's ranking_profile key to attach it."
    )
    if config.ranking:
        for name, profile in config.ranking.items():
            lines += ["", f"[ranking.{key(name)}]"]
            lines += _set_fields(profile)
    else:
        lines += ["", *_example_block("ranking.default", RankingProfileConfig())]

    lines += _divider("Apps")
    lines += _comment(
        "Which app opens a file with the Open shortcut (Option or Alt+O, or "
        "Ctrl+O). Resolved in order: a source's app_for[kind]; that source's "
        "app; app_defaults below; the "
        "built-in default for the type; then the system handler."
    )
    for name, app in config.apps.items():
        lines += ["", f"[apps.{key(name)}]"]
        lines += _set_fields(app)
    lines += ["", "[app_defaults]"]
    if config.app_defaults:
        lines += [f"{key(k)} = {toml_value(v)}" for k, v in config.app_defaults.items()]
    else:
        lines += _comment('One app id per file type, e.g. md = "obsidian".')

    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.rstrip() + "\n"
