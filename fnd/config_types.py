"""Typed config fields: each value's cleaning and checking, owned once.

The TOML loader, every Settings screen and the CLI build the same models, so a
rule written on the field type holds for all of them; no surface re-derives
strip, quote, split or bounds logic. A value is checked when the config loads,
not when something first uses it.

Paths and globs are cleaned of the text around them but never Unicode
normalised: a Linux file name is bytes, and an NFC spelling of an NFD name
points at nothing.
"""

from __future__ import annotations

import os
import re
import string
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
)

from fnd.text_canon import canonical, fold

__all__ = [
    "TEMPLATE_VARS",
    "AppArgv",
    "AppTemplate",
    "CollectionName",
    "Duration",
    "Glob",
    "SettingKey",
    "SourcePath",
    "TagList",
    "TagSelection",
    "TagSource",
    "check_argv",
    "check_template",
    "clamp_numbers",
    "clamped_settings",
    "clean_path_text",
    "collection_key",
    "collection_name_hazard",
    "composed_names",
    "field_bounds",
    "glob_error",
    "join_list",
    "name_clash",
    "setting_error",
    "split_list",
]

SettingKey = tuple[str, ...]


def _stripped(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


# ── Paths ─────────────────────────────────────────────────────────────

# What a terminal's drag-and-drop or tab completion escapes in a POSIX path.
_SHELL_ESCAPE: Final = re.compile(r"\\([ \t'\"()\[\]{}&;!$`*?#~|<>])")


def clean_path_text(raw: str) -> str:
    """A typed or pasted path without the text around it: outer whitespace, one
    pair of matching quotes, and (off Windows) shell backslash escapes."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1].strip()
    if sys.platform != "win32":
        text = _SHELL_ESCAPE.sub(r"\1", text)
    return text


def _source_path(value: object, info: ValidationInfo) -> object:
    if isinstance(value, str):
        text = clean_path_text(value)
        if not text:
            raise ValueError("a source path must not be empty")
        value = Path(text)
    if not isinstance(value, Path):
        return value
    path = value.expanduser()
    # Anchor, not is_absolute(): Windows treats "/tmp" as relative to the drive.
    if path.anchor:
        return path
    # Relative to the config file it is written in; typed anywhere else, to the shell's folder.
    context = info.context if isinstance(info.context, dict) else {}
    base = context.get("config_dir")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return (base if isinstance(base, Path) else Path(os.getcwd())) / path


SourcePath = Annotated[Path, BeforeValidator(_source_path)]


# ── Globs ─────────────────────────────────────────────────────────────


def _glob(value: object) -> object:
    if not isinstance(value, str):
        return value
    from fnd.globs import config_regex

    text = value.strip()
    while text.startswith(("/", "./")):
        text = text.removeprefix(".").lstrip("/")
    if text.endswith("/"):
        text += "**"
    if not text:
        raise ValueError("a glob must not be empty")
    if config_regex(text) is None:
        raise ValueError(f"{value!r} is not a valid glob")
    return text


Glob = Annotated[str, BeforeValidator(_glob)]
"""Root-relative: a leading ``/`` or ``./`` is dropped, and ``dir/`` means
everything under it, since a walked path never ends in a separator."""


def glob_error(text: str) -> str:
    """Why :data:`Glob` refuses ``text``, or ""."""
    try:
        _glob(text)
    except ValueError as e:
        return str(e)
    return ""


def join_list(items: Iterable[str]) -> str:
    """Items as one comma-separated line; a comma inside an item is ``\\,``."""
    return ", ".join(item.replace(",", "\\,") for item in items)


def split_list(text: str) -> list[str]:
    """Inverse of :func:`join_list`; a newline separates like a comma."""
    out: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and text[i + 1 : i + 2] == ",":
            current.append(",")
            i += 2
            continue
        if ch in ",\n":
            out.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    out.append("".join(current))
    return [item.strip() for item in out if item.strip()]


# ── Names and tags ────────────────────────────────────────────────────

# `/` and `\` reach the state file path; quotes, backtick and `,` break the
# `c:"a,b"` shorthand and pasted shell commands.
_NAME_FORBIDDEN: Final = frozenset("/\\\"'`,")


def collection_name_hazard(name: str) -> str:
    """Why ``name`` would break a consumer of it, or "". Checked at load; the
    stricter style rules in ``validate_collection_name`` apply to new names."""
    if not name.strip():
        return "a collection name must not be empty"
    if canonical(name) != name:
        return f"collection name {name!r} holds an invisible or non-canonical character"
    bad = _NAME_FORBIDDEN & set(name)
    if bad:
        shown = ", ".join(sorted(repr(c) for c in bad))
        return f"collection name {name!r} contains forbidden character(s): {shown}"
    if "\t" in name or "\n" in name:
        return f"collection name {name!r} contains a control character"
    return ""


def _collection_name(value: object) -> object:
    if isinstance(value, str):
        problem = collection_name_hazard(value)
        if problem:
            raise ValueError(problem)
    return value


CollectionName = Annotated[str, AfterValidator(_collection_name)]


def collection_key(name: str) -> str:
    """Names with one key share a state file on a case-insensitive filesystem."""
    return fold(name)


def composed_names(raw: object) -> dict[str, str]:
    """``{old: new}`` for each collection key in a raw config that is not
    :func:`canonical`, when the canonical spelling is free."""
    tables = raw.get("collections") if isinstance(raw, dict) else None
    if not isinstance(tables, dict):
        return {}
    names = {k for k in tables if isinstance(k, str)}  # pyright: ignore[reportUnknownVariableType]
    out: dict[str, str] = {}
    for name in names:
        new = canonical(name)
        if new != name and new.strip() and new not in names and new not in out.values():
            out[name] = new
    return out


def name_clash(name: str, names: Iterable[str], *, ignoring: str | None = None) -> str | None:
    """The existing name ``name`` would share a state file with, if any."""
    key = collection_key(name)
    return next((n for n in names if n != ignoring and collection_key(n) == key), None)


TagSource = Literal["frontmatter", "os"]


def _usable_tags(values: list[str]) -> list[str]:
    from fnd.tags import normalise_tag

    return [v for v in values if normalise_tag(v)]


TagList = Annotated[list[Annotated[str, BeforeValidator(_stripped)]], AfterValidator(_usable_tags)]
"""Tags as typed; one that normalises to nothing (``""``, ``#``) is dropped, as every
reader already ignored it."""
TagSelection = TagList | dict[TagSource, TagList]


# ── Durations ─────────────────────────────────────────────────────────


def _duration(value: str) -> str:
    from fnd.config import parse_duration_seconds

    if parse_duration_seconds(value) <= 0:
        raise ValueError(f"duration {value!r} must be longer than zero")
    return value


Duration = Annotated[str, BeforeValidator(_stripped), AfterValidator(_duration)]


# ── App templates ─────────────────────────────────────────────────────

TEMPLATE_VARS: Final = frozenset(
    {
        "path",
        "path_pct",
        "page",
        "slide",
        "line",
        "heading",
        "heading_pct",
        "query",
        "query_pct",
        "vault",
        "vault_pct",
        "file_in_vault",
        "file_in_vault_pct",
    }
)


def check_template(value: str) -> str:
    """``value`` if it renders: every ``{placeholder}`` a plain name the opener
    fills, with a format spec a string takes."""
    try:
        fields = list(string.Formatter().parse(value))
    except ValueError as e:
        raise ValueError(f"template {value!r}: {e}") from None
    for _literal, name, _spec, _conversion in fields:
        if name is not None and name not in TEMPLATE_VARS:
            known = ", ".join(sorted(TEMPLATE_VARS))
            raise ValueError(f"template {value!r} names {{{name}}}; known: {known}")
    try:
        value.format(**dict.fromkeys(TEMPLATE_VARS, ""))
    except (ValueError, KeyError, IndexError) as e:
        raise ValueError(f"template {value!r}: {e}") from None
    return value


AppTemplate = Annotated[str, AfterValidator(check_template)]


def check_argv(value: list[str]) -> list[str]:
    """``value`` if it names a program and every token renders."""
    if not value or not value[0].strip():
        raise ValueError("argv must name a program")
    for token in value:
        check_template(token)
    return value


AppArgv = Annotated[list[str], AfterValidator(check_argv)]


# ── One field, by key ─────────────────────────────────────────────────


def _field_info(key: SettingKey) -> Any:
    from fnd.config import Config

    model: type[BaseModel] = Config
    info: Any = None
    parts = list(key)
    while parts:
        name = parts.pop(0)
        info = model.model_fields.get(name)
        if info is None:
            return None
        annotation = info.annotation
        origin = getattr(annotation, "__origin__", None)
        if origin is dict:
            if not parts:
                return info
            parts.pop(0)
            annotation = annotation.__args__[1]
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            model = annotation
        elif parts:
            return None
    return info


def _limits(info: Any) -> tuple[float | None, float | None]:
    low = high = None
    for item in info.metadata:
        low = getattr(item, "ge", low)
        high = getattr(item, "le", high)
    return low, high


def clamp_numbers(model: type[BaseModel], data: object) -> object:
    """``data`` with each out-of-range number moved to its field's nearest bound.

    Earlier builds wrote any number, so refusing one at load would lock a user
    out over a value that breaks nothing; the next rewrite stores the clamp.
    """
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = dict(data)  # pyright: ignore[reportUnknownArgumentType]
    for name, info in model.model_fields.items():
        value = out.get(name)
        if not isinstance(value, int | float) or isinstance(value, bool):
            continue
        low, high = _limits(info)
        if low is not None and value < low:
            out[name] = low
        elif high is not None and value > high:
            out[name] = high
    return out


def clamped_settings(raw: object, model: object, path: str = "") -> list[str]:
    """What :func:`clamp_numbers` moved, as messages naming each setting."""
    out: list[str] = []
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():  # pyright: ignore[reportUnknownVariableType]
        where = f"{path}.{key}" if path else str(key)  # pyright: ignore[reportUnknownArgumentType]
        held = model.get(key) if isinstance(model, dict) else getattr(model, str(key), None)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        if isinstance(value, dict):
            out += clamped_settings(value, held, where)
        elif isinstance(value, list) and isinstance(held, list):
            for i, item in enumerate(value):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
                if i < len(held):  # pyright: ignore[reportUnknownArgumentType]
                    out += clamped_settings(item, held[i], f"{where}[{i}]")
        elif (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and isinstance(held, int | float)
            and held != value
        ):
            out.append(f"Move {where} from {value} to {held}, the nearest it allows")
    return out


def field_bounds(key: SettingKey) -> tuple[float, float] | None:
    """The ``(low, high)`` a numeric field declares, or None."""
    info = _field_info(key)
    if info is None:
        return None
    low, high = _limits(info)
    return (low, high) if low is not None and high is not None else None


def setting_error(key: SettingKey, value: object) -> str:
    """Why the field at ``key`` refuses ``value``, or ""."""
    info = _field_info(key)
    if info is None:
        return ""
    bounds = field_bounds(key)
    if bounds is not None and isinstance(value, int | float) and not isinstance(value, bool):
        low, high = bounds
        return "" if low <= value <= high else f"outside {low:g}-{high:g}"
    try:
        annotation = (
            Annotated[info.annotation, *info.metadata] if info.metadata else info.annotation
        )
        TypeAdapter(annotation).validate_python(value)
    except ValidationError as e:
        return str(e.errors()[0]["msg"])
    return ""
