"""No widget in fnd parses variable text as markup: every sink goes through the
literal seams in :mod:`fnd.tui.ui_text`."""

from __future__ import annotations

import ast
import re
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

FND = Path(__file__).resolve().parents[1] / "fnd"
SEAM = FND / "tui" / "ui_text.py"

# Textual classes that parse a ``str`` as markup; fnd uses the Plain* seams.
_MARKUP_CLASSES = {"Static", "Label", "Tree", "OptionList", "SelectionList", "Button", "App"}
_TEMPLATES = {"ui_text", "ui_block"}
_LITERAL_CALLS = {*_TEMPLATES, "literal"}
# Constructed directly only with a literal label.
_LITERAL_LABELLED = {"Button"}
# Set only through ui_text.set_border_title / set_border_subtitle.
_TITLES = {"border_title", "border_subtitle"}
# A style tag in a literal belongs in a ui_text/ui_block template; anywhere
# else a literal widget shows it as text.
_MARKUP_TAG = re.compile(
    r"\[(?:/(?=[\]a-z$@])|\$|@|/?(?:b|bold|i|italic|u|underline|dim|reverse|strike|s|blink|"
    r"link|red|green|yellow|blue|cyan|magenta|white|black|grey|gray|on|not)(?=[\] =]))[^\]]*\]"
)


def _is_textual(module: str | None) -> bool:
    return module is not None and (module == "textual.app" or module.startswith("textual.widgets"))


def _imports(module: ast.Module) -> tuple[set[str], set[str]]:
    """``(markup class names, aliases of textual widget modules)`` bound here."""
    classes: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and _is_textual(node.module):
            classes |= {a.asname or a.name for a in node.names if a.name in _MARKUP_CLASSES}
        elif isinstance(node, ast.ImportFrom) and node.module == "textual":
            modules |= {a.asname or a.name for a in node.names if a.name in {"widgets", "app"}}
        elif isinstance(node, ast.Import):
            modules |= {a.asname or a.name for a in node.names if _is_textual(a.name)}
    return classes, modules


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (owner := _dotted(node.value)):
        return f"{owner}.{node.attr}"
    return None


def _markup_class(node: ast.expr, classes: set[str], modules: set[str]) -> str | None:
    root = node.value if isinstance(node, ast.Subscript) else node
    if isinstance(root, ast.Name) and root.id in classes:
        return root.id
    if (
        isinstance(root, ast.Attribute)
        and root.attr in _MARKUP_CLASSES
        and _dotted(root.value) in modules
    ):
        return root.attr
    return None


def _called_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _constant(node: ast.expr) -> object:
    return node.value if isinstance(node, ast.Constant) else None


def _is_str_literal(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_literal_text(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return node.value is None or isinstance(node.value, str)
    return isinstance(node, ast.Call) and _called_name(node.func) in _LITERAL_CALLS


def _template_literals(module: ast.Module) -> set[int]:
    templates: set[int] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Call) and _called_name(node.func) in _TEMPLATES and node.args:
            templates.add(id(node.args[0]))
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            templates.add(id(node.value))  # a docstring
    return templates


def module_violations(where: str, module: ast.Module) -> list[str]:
    classes, modules = _imports(module)
    templates = _template_literals(module)
    found: list[str] = []
    for node in ast.walk(module):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if name := _markup_class(base, classes, modules):
                    found.append(f"{where}:{node.lineno} subclasses {name}")
        elif isinstance(node, ast.Call):
            name = _called_name(node.func)
            if cls := _markup_class(node.func, classes, modules):
                labelled = cls in _LITERAL_LABELLED and node.args and _is_literal_text(node.args[0])
                if not labelled:
                    found.append(f"{where}:{node.lineno} constructs {cls}")
            elif name in {"from_markup", "render_str"}:
                found.append(f"{where}:{node.lineno} calls {name}")
            elif name == "setattr" and len(node.args) > 1 and _constant(node.args[1]) in _TITLES:
                found.append(f"{where}:{node.lineno} sets a border title by name")
            elif name in _TEMPLATES and not (node.args and _is_str_literal(node.args[0])):
                found.append(
                    f"{where}:{node.lineno} passes {name} a template that is not a literal"
                )
        elif isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr in _TITLES:
                    found.append(f"{where}:{node.lineno} assigns {target.attr}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in templates
            and _MARKUP_TAG.search(node.value)
        ):
            found.append(f"{where}:{node.lineno} has markup outside a template")
    return found


def _modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(FND.rglob("*.py")):
        if path != SEAM:
            yield str(path.relative_to(FND.parent)), ast.parse(path.read_text(encoding="utf-8"))


def test_every_widget_sink_shows_variable_text_literally() -> None:
    assert [v for where, module in _modules() for v in module_violations(where, module)] == []


@pytest.mark.parametrize(
    "source",
    [
        "from textual.widgets import Static\nStatic(name)",
        "from textual.widgets._static import Static\nStatic(name)",
        "from textual import widgets\nwidgets.Static(name)",
        "import textual.widgets as w\nw.Label(name)",
        "from textual import widgets\nclass Row(widgets.Label): ...",
        "from textual.widgets import Tree\nclass T(Tree[int]): ...",
        "from textual.app import App\nclass A(App[None]): ...",
        "from textual.widgets import Button\nButton(name)",
        "import textual.widgets\ntextual.widgets.Static(name)",
        "box.border_title = name",
        "box.border_title += name",
        "setattr(box, 'border_subtitle', name)",
        "Content.from_markup(text)",
        "ui_text(f'Renamed to {name}')",
        "ui_text(template)",
        "status.update('[dim]Current:[/] x')",
    ],
)
def test_the_gate_catches_each_way_round_it(source: str) -> None:
    assert module_violations("probe", ast.parse(source))


def test_the_gate_passes_the_seams() -> None:
    source = """
        from textual.widgets import Button, Static
        Button("Open")
        query_one("#x", Static).update(name)
        ui_text("[dim]Current:[/] $name", name=name)
        set_border_title(box, f"Collections › {name}")
    """
    assert module_violations("probe", ast.parse(textwrap.dedent(source))) == []
