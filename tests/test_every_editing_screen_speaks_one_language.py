"""Every editing screen declares a role, and only documents save or ask."""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from textual.screen import Screen

import fnd.tui.first_reindex_warning as first_run
import fnd.tui.settings_screen as settings
from fnd.tui.editing import DocumentScreen, PartScreen, Role

_ROOT = Path(__file__).resolve().parent.parent / "fnd" / "tui"
#: A base whose subclasses take the role of what opens them; never pushed itself.
_BASES = frozenset({"FilterBrowserScreen", "_SourceFields"})


def _screens() -> list[type[Screen[object]]]:
    found: list[type[Screen[object]]] = []
    for module in (settings, first_run):
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Screen) and obj.__module__ == module.__name__ and name not in _BASES:
                found.append(obj)
    return found


def _binds(cls: type[Screen[object]], key: str) -> bool:
    return any(
        key in str(b.key).split(",")
        for klass in cls.__mro__
        for b in klass.__dict__.get("BINDINGS", ())
        if hasattr(b, "key")
    )


def test_every_screen_declares_a_role() -> None:
    """A new screen cannot opt out of the language by forgetting."""
    missing = [c.__name__ for c in _screens() if not isinstance(getattr(c, "ROLE", None), Role)]
    assert not missing, f"screens with no ROLE: {missing}"


def test_documents_and_only_documents_bind_the_save() -> None:
    """A ^s anywhere else says Esc would not keep the change."""
    wrong = [
        c.__name__
        for c in _screens()
        if _binds(c, "ctrl+s") != (c.ROLE is Role.DOCUMENT)  # type: ignore[attr-defined]
    ]
    assert not wrong, f"^s bound against role: {wrong}"


def test_the_role_and_the_base_class_agree() -> None:
    """The prompt and the save come with the base, so a role cannot be claimed without them."""
    wrong = [
        c.__name__
        for c in _screens()
        if (c.ROLE is Role.DOCUMENT) != issubclass(c, DocumentScreen)  # type: ignore[attr-defined]
        or (c.ROLE is Role.PART) != issubclass(c, PartScreen)  # type: ignore[attr-defined]
    ]
    assert not wrong, wrong


def test_the_save_key_is_named_only_through_the_vocabulary() -> None:
    """COMMIT_KEY outside editing.py would be a second word for Save."""
    offenders = [
        f"{p.name}:{n}"
        for p in _ROOT.glob("*.py")
        if p.name != "editing.py"
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "COMMIT_KEY" in line
    ]
    assert not offenders, offenders


def test_no_esc_hint_is_spelt_out_by_hand() -> None:
    """Every Esc label in a Settings footer comes from the vocabulary."""
    source = (_ROOT / "settings_screen.py").read_text(encoding="utf-8")
    literal = re.findall(r'\(\s*"Esc[^"]*"\s*,\s*"[^"]+"\s*\)', source)
    assert not literal, literal


def test_only_editing_py_raises_the_unsaved_prompt() -> None:
    """One prompt, asked by documents through ask_before_leaving."""
    for p in _ROOT.glob("*.py"):
        if p.name == "editing.py":
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "UnsavedChangesScreen"
        ]
        assert not calls, p.name


def _esc_action(cls: type[Screen[object]]) -> object:
    for klass in cls.__mro__:
        for b in klass.__dict__.get("BINDINGS", ()):
            if "escape" in str(getattr(b, "key", "")).split(","):
                return getattr(cls, "action_" + str(b.action).split("(")[0])
    raise AssertionError(f"{cls.__name__} binds no Esc")


def test_every_screen_can_actually_be_left() -> None:
    """A screen whose Esc neither pops nor reaches an implemented leave is a trap."""
    trapped: list[str] = []
    for cls in _screens():
        back = inspect.getsource(_esc_action(cls))  # type: ignore[arg-type]
        if "pop_screen" in back or "dismiss" in back:
            continue
        leave = getattr(cls, "request_leave", None)
        body = inspect.getsource(leave) if leave is not None else ""
        if "request_leave" not in back or "NotImplementedError" in body:
            trapped.append(cls.__name__)
    assert not trapped, f"screens with no way out of Esc: {trapped}"
