"""Readable content held in inline ``<script>`` data (single-file generated pages).

Such pages ship empty containers and fill them at runtime from JS data, so the
DOM walk sees none of it. This reads the scripts' data literals (statically, see
``_js_data``, or evaluated in a sandbox, see ``_js_eval``) and rebuilds the page's own structure: a program
listing, then numbered steps, each with its narration, the code it runs, its
printed output, and the diagram views (chips, partitions, row mappings,
tables, terminal transcripts) as inline code, lists and tables.

Blocks are keyed by the name of the object they sit in, so the walker can place
them at the DOM node whose attribute carries the same name (``FLOWS.fs = {...}``
fills ``data-flow="fs"``). Prose literals outside any such object are kept
under the ``""`` key.
"""

from __future__ import annotations

import itertools
import re
import time
from collections.abc import Iterator
from html import escape
from itertools import chain
from typing import NamedTuple

from lxml.html import HtmlElement

from fnd.extract import _js_eval
from fnd.extract._js_data import OPAQUE, Assignment, Call, parse_assignments, parse_literal, strings

# Past this a script is a bundle, not authored data.
_MAX_SCRIPT_CHARS = 2_000_000
_MAX_TOTAL_CHARS = 2_000_000
_MAX_NODES = 500_000
_RENDER_SECONDS = 15  # a hostile page costs the indexer at most this much rendering time
_MAX_DEPTH = 40
_MAX_CELL_DEPTH = 12
_MAX_COVERAGE_CHECKS = 200
_DATA_TYPES = frozenset({"application/json", "text/json"})
_CONTROL = re.compile(
    r"\x1b\[[0-9;?]*[\x20-\x2f]*[\x40-\x7e]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff"
    + chr(0xFFFE)
    + chr(0xFFFF)
    + "]"
)
_ENTITY = re.compile(r"&#?\w+;")
_MIN_WORDS = 4
_MIN_LETTER_RATIO = 0.5

_NARRATION_KEYS = ("say", "desc", "description", "caption", "explain")
_CODE_KEYS = frozenset({"code", "cmd", "command", "out", "output", "stdout", "stderr"})
# Keys that style a view (highlights, colours, flags) rather than carry content.
_STYLE_KEYS = frozenset(
    {"type", "hl", "dim", "wrap", "lazy", "state", "k", "cls", "on", "link", "read", "win"}
    | {"hlCol", "hlTypes", "badRow", "level", "marks", "showParts", "reveal", "keep", "err", "pick"}
)
# A mapping view labels its columns in/out/step with these keys.
_MAPPING_HEADS = {"a": "inLabel", "b": "outLabel", "why": "mid"}
# Keys a view uses to caption or head its own table, so they are not repeated as fields.
_VIEW_LABELS = frozenset({"label", "sub", "col", "rules", *_MAPPING_HEADS.values()})
_MAX_TABLE_COLUMNS = 7
_WORD_BREAK = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|_")
_CODE_SHAPED = re.compile(r"[\s()=\[\]{}.:;#]")
_BALANCED_TAG = re.compile(r"<[^<>]*>")
_CODE_CALL = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\(.*\)")
_JS_SYNTAX = re.compile(
    r"[{}]|;\s*$|;\s*[\w.$]+\s*[=(.]|=>|\b(?:function|var|let|const|return)\b|querySelector|^[.#\[]"
)
_SHELL_WORDS = ("shell", "bash", "terminal")


class ScriptBlock(NamedTuple):
    # "p" or "li" (may carry inline HTML), "h" (a data section's name), "html" (a fragment),
    # "code" or "table"
    kind: str
    text: str = ""
    rows: tuple[tuple[str, ...], ...] = ()  # table cells, as markdown
    lang: str = ""


def _clean(text: str) -> str:
    """``text`` without the characters the HTML parser and the index reject."""
    return _CONTROL.sub("", text)


def _balanced(text: str) -> bool:
    rest = _BALANCED_TAG.sub("", text)
    return "<" not in rest and ">" not in rest


def _readable(text: str, code: bool = True) -> bool:
    """Prose, or (``code``) a one-line call such as ``rdd.collect()`` a lesson shows."""
    if not _balanced(text):  # a template fragment, not prose
        return False
    plain = " ".join(_BALANCED_TAG.sub("", text).split())
    if not plain or _JS_SYNTAX.search(_ENTITY.sub(" ", plain)):
        return False
    if _CODE_CALL.fullmatch(plain):
        return code
    if len(plain.split()) < _MIN_WORDS:
        return False
    return sum(ch.isalpha() or ch.isspace() for ch in plain) / len(plain) >= _MIN_LETTER_RATIO


def _scalar(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _is_chip(value: object) -> bool:
    return isinstance(value, dict) and ("t" in value or "h" in value)


def _chip(value: object) -> str:
    """Text of a data chip: a literal, an evaluated ``{t, h}`` object, or a helper call's first argument."""
    if isinstance(value, Call):
        return next((t for a in value.args if (t := _chip(a))), "")
    if isinstance(value, dict) and _is_chip(value):
        text = value.get("t")
        if isinstance(text, str) and text:
            return text
        html = value.get("h")
        return " ".join(_BALANCED_TAG.sub("", html).split()) if isinstance(html, str) else ""
    return _scalar(value)


def _humanise(key: str) -> str:
    """``keyStats`` / ``key_stats`` as "Key stats"."""
    return " ".join(_WORD_BREAK.sub(" ", key).split()).capitalize()


def _is_link(value: object) -> bool:
    """A ``{label, url}`` citation."""
    return (
        isinstance(value, dict)
        and isinstance(value.get("label"), str)
        and set(value) <= {"label", "url"}
    )


def _is_links(value: object) -> bool:
    return _is_link(value) or (
        isinstance(value, list) and bool(value) and all(_is_link(v) for v in value)
    )


def _link_html(link: object) -> str:
    assert isinstance(link, dict)
    label, url = escape(str(link["label"])), link.get("url")
    return (
        f'<a href="{escape(url, quote=True)}">{label}</a>'
        if isinstance(url, str) and url
        else label
    )


def _record_cell(value: object, depth: int = 0) -> str:
    """A table cell for a record field: scalars as text, ``{label, url}`` as a link."""
    if _is_link(value):
        assert isinstance(value, dict)
        url = value.get("url")
        return f"[{value['label']}]({url})" if isinstance(url, str) and url else str(value["label"])
    if isinstance(value, list):
        if depth > _MAX_CELL_DEPTH:
            return ""
        return "; ".join(c for c in (_record_cell(v, depth + 1) for v in value) if c)
    return "" if isinstance(value, bool) else _scalar(value)


def _is_flat(value: object, depth: int = 0) -> bool:
    """Lists of plain values and links: a table cell shows every one of them."""
    if _is_link(value) or not isinstance(value, (dict, list)):
        return True
    return (
        isinstance(value, list)
        and depth <= _MAX_CELL_DEPTH
        and all(_is_flat(v, depth + 1) for v in value)
    )


def _strs(value: object) -> list[str] | None:
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return [str(v) for v in value]
    return None


def _code_html(chips: list[str]) -> str:
    return " ".join(f"<code>{escape(c)}</code>" for c in chips)


class _Renderer:
    def __init__(
        self,
        kinds: dict[str, str],
        *,
        data: bool = False,
        nodes: int = _MAX_NODES,
        chars: int = _MAX_TOTAL_CHARS,
        deadline: float = float("inf"),
    ) -> None:
        self._deadline = deadline
        self._kinds = kinds
        self._all_content = data  # a JSON block: every string is content, not just prose-like ones
        self._default_lang = "python" if any("python" in v.lower() for v in kinds.values()) else ""
        self._listing_lang = ""
        self.blocks: list[ScriptBlock] = []
        self._prose: set[str] = set()
        self.nodes = nodes  # a shared reference is walked once per use, so the walk is metered
        self.chars = 0
        self._char_limit = chars
        self._depth = 0

    def render(self, value: object) -> None:
        self._walk(value, None, ())

    def _add(self, block: ScriptBlock) -> None:
        """Keep a block, minus control characters (ANSI colour codes) the HTML parser rejects."""
        block = block._replace(
            text=_clean(block.text),
            rows=tuple(tuple(_clean(c) for c in row) for row in block.rows),
        )
        size = len(block.text) + sum(map(len, chain(*block.rows)))
        if self.nodes <= 0 or self.chars + size > self._char_limit:
            self.nodes = 0  # over budget: this and every later block is dropped
            return
        self.chars += size
        self.blocks.append(block)

    def _enter(self) -> bool:
        self.nodes -= 1
        if time.monotonic() > self._deadline:
            self.nodes = -1
        return self.nodes >= 0 and self._depth < _MAX_DEPTH

    def _live(self) -> bool:
        """Whether the file still has budget; loops that scan call this each turn."""
        self.nodes -= 1
        if time.monotonic() > self._deadline:
            self.nodes = -1
        return self.nodes > 0

    def _paragraph(self, html: str, *, trusted: bool = False, kind: str = "p") -> None:
        plain = trusted or self._all_content
        keep = (_balanced(html) if plain else _readable(html)) and (
            trusted or html not in self._prose
        )
        if keep and html.strip():
            self._prose.add(html)
            self._add(ScriptBlock(kind, html))

    def _code(self, text: str, lang: str = "") -> None:
        if text.strip():
            self._add(ScriptBlock("code", text, lang=lang))

    def _lang(self, kind: object) -> str:
        label = self._kind_label(kind).lower()
        if any(w in label for w in _SHELL_WORDS):
            return "bash"
        return "python" if "python" in label else self._default_lang

    def _walk(self, value: object, key: str | None, lines: tuple[str, ...]) -> None:
        if not self._enter():
            return
        self._depth += 1
        try:
            self._walk_node(value, key, lines)
        finally:
            self._depth -= 1

    def _walk_node(self, value: object, key: str | None, lines: tuple[str, ...]) -> None:
        if _is_link(value):
            self._paragraph(_link_html(value), trusted=True, kind="li")
        elif isinstance(value, str):
            if key == "code" and not _CODE_SHAPED.search(value):  # an identifier such as "DD"
                self._paragraph(f"<code>{escape(value)}</code>", trusted=True)
            elif key in _CODE_KEYS:
                self._code(value)
            else:
                self._paragraph(value)
        elif isinstance(value, list):
            code = _strs(value)
            if key == "code" and code:
                self._code("\n".join(code), self._listing_lang)
            elif key in _CODE_KEYS and code:
                self._code("\n".join(code))
            elif any(isinstance(v, dict) and self._is_step(v) for v in value):
                self._steps(value, lines)
            elif not self._record_table(value):
                bullets = len(value) > 1 and all(isinstance(v, str) for v in value)
                for item in value:
                    if bullets and isinstance(item, str):
                        self._paragraph(item, kind="li")
                    else:
                        self._walk(item, None, lines)
        elif isinstance(value, dict):
            listing = _strs(value.get("code"))
            outer, self._listing_lang = self._listing_lang, self._flow_lang(value.get("steps"))
            for k, v in value.items():
                labelled = (
                    "steps" not in value
                    and isinstance(v, (dict, list))
                    and bool(v)
                    and not _is_links(v)  # citations stay with the content they support
                )
                if labelled:
                    self._add(ScriptBlock("h", _humanise(k)))
                lead = len(self.blocks)
                self._walk(v, k, tuple(listing) if listing else lines)
                if labelled and len(self.blocks) == lead:
                    self.blocks.pop()  # a lead-in with nothing under it
            self._listing_lang = outer

    def _record_table(self, items: list[object]) -> bool:
        """Render a list of flat records as a table (links for ``{label, url}`` cells)."""
        rows = [r for r in items if isinstance(r, dict)]
        if len(rows) < 2 or len(rows) != len(items) or _is_links(items):
            return False
        keys = dict.fromkeys(k for r in rows for k in r)
        table: list[dict[str, str]] = []
        used, over = 0, False
        for r in rows:  # cells are built once, against what is left of the file's budget
            row: dict[str, str] = {}
            for k in keys:
                row[k] = _record_cell(r.get(k))
                used += len(row[k])
                if used > self._char_limit - self.chars or not self._live():
                    over = True
                    break
            if over:
                break
            table.append(row)
        cols = [k for k in keys if any(row[k] for row in table)]
        if not 2 <= len(cols) <= _MAX_TABLE_COLUMNS:
            return False
        grid = tuple(tuple(row[c] for c in cols) for row in table)
        self.nodes = max(self.nodes, 1)  # the rows that fit are kept; the budget ends after them
        self._add(ScriptBlock("table", rows=(tuple(_humanise(c) for c in cols), *grid)))
        if over:
            self.nodes = 0
            return True
        outer, self._all_content = self._all_content, True  # record fields are data, not config
        for r, cells in zip(rows, grid, strict=True):  # nested fields a cell cannot show
            shown = " ".join(cells)
            for k, v in r.items():
                if not isinstance(v, (dict, list)) or _is_flat(v):
                    continue
                inside = list(itertools.islice(_strings_in(v), _MAX_COVERAGE_CHECKS + 1))
                self.nodes -= len(inside) * (1 + len(shown) // 1024)
                if self.nodes <= 0:
                    break
                if len(inside) > _MAX_COVERAGE_CHECKS or not all(s in shown for s in inside):
                    self._walk(v, k, ())
        self._all_content = outer
        return True

    def _flow_lang(self, steps: object) -> str:
        kinds = (
            [s.get("kind") for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []
        )
        langs = {self._lang(k) for k in kinds}
        return langs.pop() if len(langs) == 1 else self._default_lang

    @classmethod
    def _is_step(cls, record: dict[str, object]) -> bool:
        """A walkthrough step narrates (``say``) or points at code and views; other records are data."""
        return bool(cls._narration(record)) and bool({"say", "views", "line"} & record.keys())

    @staticmethod
    def _narration(step: dict[str, object]) -> str:
        for k in _NARRATION_KEYS:
            v = step.get(k)
            if isinstance(v, str) and v.strip():
                return v
        return ""

    def _kind_label(self, kind: object) -> str:
        described = self._kinds.get(kind, "") if isinstance(kind, str) else ""
        return described.split(":")[0].strip()

    def _steps(self, steps: list[object], lines: tuple[str, ...]) -> None:
        n = 0
        for step in steps:
            if not self._enter():
                return
            if not isinstance(step, dict):
                self._walk(step, None, lines)
                continue
            n += 1
            label = self._kind_label(step.get("kind"))
            head = escape(f"Step {n} · {label}" if label else f"Step {n}")
            say = self._narration(step)
            if _balanced(say):
                self._paragraph(f"<b>{head}</b> {say}".rstrip(), trusted=True)
            self._step_views(step, lines)

    def _executed(self, step: dict[str, object], lines: tuple[str, ...]) -> list[str]:
        raw = step.get("line")
        refs = raw if isinstance(raw, list) else [raw]
        self.nodes -= len(refs)
        if self.nodes <= 0:
            return []
        return self._fit(
            [
                lines[r]
                for r in refs
                if isinstance(r, int) and not isinstance(r, bool) and 0 <= r < len(lines)
            ]
        )

    def _fit(self, parts: list[str]) -> list[str]:
        """The leading parts that still fit the file's budget, before anything joins them."""
        room, used = self._char_limit - self.chars, 0
        for n, part in enumerate(parts):
            used += len(part) + 1
            if used > room:
                self.nodes = 0
                return parts[:n]
        return parts

    def _step_views(self, step: dict[str, object], lines: tuple[str, ...]) -> None:
        ran = self._executed(step, lines)
        ran_set = set(ran)
        views = step.get("views")
        extras: list[dict[str, object]] = []
        printed: list[str] = []
        for view in views if isinstance(views, list) else []:
            if not self._live():
                break
            if not isinstance(view, dict):
                continue
            if view.get("type") == "out":
                cmd, out = view.get("cmd"), view.get("out")
                if isinstance(cmd, str) and cmd not in ran_set:
                    ran.append(cmd)
                    ran_set.add(cmd)
                if isinstance(out, str):
                    printed.append(out)
            else:
                extras.append(view)
        if not any(v.get("type") == "term" for v in extras):  # a transcript shows the command
            self._code("\n".join(self._fit(ran)), self._lang(step.get("kind")))
        self._code("\n".join(self._fit(printed)))
        for view in extras:
            self._view(view)

    def _view(self, view: dict[str, object]) -> None:
        if not self._enter():
            return
        kind = view.get("type")
        if kind == "table":
            self._table(view)
        elif kind == "term":
            self._code(self._transcript(view.get("lines")))
        else:
            captions = [str(view[k]) for k in ("label", "sub") if str(view.get(k) or "").strip()]
            if captions:
                self._paragraph(f"<i>{escape(' · '.join(captions))}</i>", trusted=True)
            for key, val in view.items():
                if key not in _STYLE_KEYS | _VIEW_LABELS and not isinstance(val, bool):
                    self._data(key, val, view)
            return
        note = view.get("note")
        if isinstance(note, str):
            self._paragraph(note, trusted=True)

    def _data(self, key: str, val: object, view: dict[str, object]) -> None:
        if not self._enter():
            return
        self._depth += 1
        try:
            self._data_node(key, val, view)
        finally:
            self._depth -= 1

    def _data_node(self, key: str, val: object, view: dict[str, object]) -> None:
        """Render one value of a diagram view as inline code, a list or a table."""
        if isinstance(val, str):
            if val.lstrip().startswith("<") and _balanced(val):  # markup the page built itself
                self._add(ScriptBlock("html", val))
            elif _readable(val):
                self._paragraph(val)
            elif val.strip() and key != "items":
                self._paragraph(f"<b>{escape(key)}</b> {_code_html([val])}", trusted=True)
        elif isinstance(val, dict):
            for k, v in val.items():
                if k not in _STYLE_KEYS:
                    self._data(k, v, view)
        elif isinstance(val, list):
            self._data_list(key, val, view)

    def _data_list(self, key: str, items: list[object], view: dict[str, object]) -> None:
        if key == "views":
            for sub in items:
                if isinstance(sub, dict):
                    self._view(sub)
        elif items and all(isinstance(i, list) for i in items):
            name = escape(key[:-1].capitalize() if key.endswith("s") else key.capitalize())
            for n, group in enumerate(items):
                chips = [c for c in map(_chip, group if isinstance(group, list) else []) if c]
                chips = self._fit(chips)
                self._paragraph(f"<b>{name} {n}</b> {_code_html(chips)}", trusted=True, kind="li")
        elif items and all(isinstance(i, dict) and not _is_chip(i) for i in items):
            self._row_table(items, view)
        else:
            chips = self._fit([c for c in map(_chip, items) if c])
            if chips:
                lead = "" if key == "items" else f"<b>{escape(key)}</b> "
                self._paragraph(f"{lead}{_code_html(chips)}", trusted=True)

    def _row_table(self, rows: list[object], view: dict[str, object]) -> None:
        dicts = [r for r in rows if isinstance(r, dict)]
        cols = [k for k in dict.fromkeys(k for r in dicts for k in r) if k not in _STYLE_KEYS]
        if len(cols) == 1:  # a pipeline such as a query plan's steps
            chips = [c for c in (_chip(r.get(cols[0])) for r in dicts) if c]
            if chips:
                self._paragraph(
                    " → ".join(f"<code>{escape(c)}</code>" for c in chips), trusted=True
                )
            return
        fit = max((self.nodes - 1) // max(len(cols), 1) - 1, 0)
        over = len(dicts) > fit
        dicts = dicts[:fit]
        self.nodes -= (len(dicts) + 1) * len(cols)
        heads = [self._head(k, view) for k in cols]
        grid = [tuple(heads), *(tuple(self._row_cell(r, c, view) for c in cols) for r in dicts)]
        if self.nodes > 0 and any(cell for row in grid[1:] for cell in row):
            self._add(ScriptBlock("table", rows=tuple(grid)))
        if over:
            self.nodes = 0

    @staticmethod
    def _head(key: str, view: dict[str, object]) -> str:
        if key == "hit" and isinstance(view.get("rules"), list):
            return "first true rule"
        named = view.get("col") if key == "v" else view.get(_MAPPING_HEADS.get(key, ""))
        return str(named) if isinstance(named, str) and named else key

    def _row_cell(self, row: dict[str, object], key: str, view: dict[str, object]) -> str:
        value, rules = row.get(key), view.get("rules")
        if key == "hit" and isinstance(rules, list) and isinstance(value, int):
            return f"`{rules[value]}`" if 0 <= value < len(rules) else ""
        if key == "b" and key in row and value is None and "outLabel" in view:
            return "dropped"
        return self._cell(value)

    def _cell(self, value: object) -> str:
        if isinstance(value, list):
            return ", ".join(f"`{c}`" for c in self._fit([c for c in map(_chip, value) if c]))
        if isinstance(value, Call) or _is_chip(value):
            return f"`{_chip(value)}`" if _chip(value) else ""
        return "yes" if value is True else _scalar(value)

    def _transcript(self, lines: object) -> str:
        out: list[str] = []
        used = 0
        for ln in lines if isinstance(lines, list) else []:
            if not isinstance(ln, dict):
                continue
            if isinstance(ln.get("o"), str):
                line = str(ln["o"])
            elif isinstance(ln.get("p"), str):
                line = f"{ln['p']} {ln.get('c') or ''}".rstrip()
            else:
                continue
            used += len(line) + 1
            if used > self._char_limit - self.chars:
                self.nodes = 0
                break
            out.append(line)
        return "\n".join(out)

    def _table(self, view: dict[str, object]) -> None:
        cols = view.get("cols")
        if not isinstance(cols, list) or not cols:
            return
        body = view.get("rows")
        body = body if isinstance(body, list) else []
        fit = max((self.nodes - 1) // len(cols) - 2, 0)  # short rows pad to the header width
        over = len(body) > fit
        body = body[:fit]
        self.nodes -= (len(body) + 2) * len(cols)
        grid: list[object] = [cols, view.get("types"), *body]
        rows = tuple(tuple(_scalar(c) for c in row) for row in grid if isinstance(row, list))
        if self.nodes > 0 and any(cell for row in rows for cell in row):
            self._add(ScriptBlock("table", rows=rows))
        if over:
            self.nodes = 0


def _kind_labels(assigned: list[Assignment]) -> dict[str, str]:
    """Short-code to description maps the page defines for its own step kinds."""
    labels: dict[str, str] = {}
    for a in assigned:
        if (
            isinstance(a.value, dict)
            and a.value
            and all(isinstance(v, str) for v in a.value.values())
        ):
            labels.update({k: str(v) for k, v in a.value.items()})
    return labels


def _strings_in(value: object, depth: int = 0, seen: set[int] | None = None) -> Iterator[str]:
    """Every string a parsed or evaluated literal holds, including chip and call arguments."""
    seen = set() if seen is None else seen
    if isinstance(value, str):
        yield value
    elif depth <= 12 and id(value) not in seen:
        seen.add(id(value))  # a shared reference holds the same strings however often it is used
        parts = (
            value.values()
            if isinstance(value, dict)
            else value.args
            if isinstance(value, Call)
            else value
            if isinstance(value, list)
            else ()
        )
        for part in parts:
            yield from _strings_in(part, depth + 1, seen)


def _computed(value: object, depth: int = 0, seen: set[int] | None = None) -> bool:
    """Whether the literal holds anything the static reader could not resolve."""
    if value is OPAQUE or isinstance(value, Call):  # a call can compute anything
        return True
    seen = set() if seen is None else seen
    if depth > 12 or id(value) in seen:
        return False
    seen.add(id(value))
    parts = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    return any(_computed(v, depth + 1, seen) for v in parts)


def _with_computed(src: str, assigned: list[Assignment], timeout: float) -> list[Assignment]:
    """Swap in the evaluated value where the static one is incomplete."""
    # A shell costs ~50 ms: only worth it where a script with computed values also holds prose.
    if not any(_computed(a.value) for a in assigned):
        return assigned
    if not any(_readable(text) for _, text in strings(src)):
        return assigned
    values = _js_eval.evaluate(src, assigned, timeout)
    return [a._replace(value=values.get((a.base, a.name), a.value)) for a in assigned]


def script_text(root: HtmlElement) -> dict[str, list[ScriptBlock]]:
    """Blocks per owning object name ("" when none), in source order; none if the scripts defeat us."""
    try:
        return _script_blocks(root)
    except Exception:  # a hostile script must not cost the page its own text
        return {}


def _script_blocks(root: HtmlElement) -> dict[str, list[ScriptBlock]]:
    out: dict[str, list[ScriptBlock]] = {}
    seen: set[str] = set()
    loose: list[str] = []
    chars, nodes = _MAX_TOTAL_CHARS, _MAX_NODES
    deadline = time.monotonic() + _js_eval.TIMEOUT_SECONDS  # one evaluation budget per file
    render_by = time.monotonic() + _RENDER_SECONDS
    for el in root.iter("script"):
        src = el.text_content()
        if el.get("src") or len(src) > _MAX_SCRIPT_CHARS:
            continue
        kind = el.get("type") or ""
        if "json" in kind and kind not in _DATA_TYPES:  # JSON-LD, import maps: not page content
            continue
        is_json = kind in _DATA_TYPES
        if is_json:
            assigned = [Assignment("", parse_literal(src), 0, len(src))]
        else:
            assigned = _with_computed(src, parse_assignments(src), deadline - time.monotonic())
        renderer = _Renderer(
            _kind_labels(assigned), data=is_json, nodes=nodes, chars=chars, deadline=render_by
        )
        for a in assigned:
            if a.value is OPAQUE:
                continue
            renderer.blocks = []
            renderer.render(a.value)
            out.setdefault(a.name, []).extend(renderer.blocks)
        chars -= renderer.chars
        nodes = renderer.nodes
        if chars < 0 or nodes <= 0:
            return _with_loose(out, loose)
        covered = {_clean(" ".join(s.split())) for a in assigned for s in _strings_in(a.value)}
        for _, text in strings(src):
            text = _clean(" ".join(text.split()))
            if text not in seen and text not in covered and _readable(text):
                seen.add(text)
                chars -= len(text)
                loose.append(text)
        if chars < 0:
            break
    return _with_loose(out, loose)


def _with_loose(
    out: dict[str, list[ScriptBlock]], loose: list[str]
) -> dict[str, list[ScriptBlock]]:
    """Add the strings found outside any data literal.

    One-line code calls only count on a page that also carries data: elsewhere they are
    scripting (``window.close()``), not a lesson.
    """
    shows_data = any(out.values())
    for text in loose:
        if shows_data or _readable(text, code=False):
            out.setdefault("", []).append(ScriptBlock("p", text))
    return out
