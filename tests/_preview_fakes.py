"""Shared duck-typed stubs for the preview reveal/guard unit tests.

Several preview tests exercise ``PreviewPresenter.reveal`` / ``reveal_active`` as
unbound methods against minimal stand-ins (no full Textual app). The container
stub is identical across them, so it lives here to keep the class-flip behaviour
(``add_class`` / ``remove_class`` / ``has_class``) consistent in one place.

The stub mirrors the parts of Textual's API that ``preview/visibility.py`` uses,
including the ``update`` keyword and the ``app.stylesheet`` restyle that follows
it. Restyling means nothing to a stub that only holds a set of names, so both are
RECORDED — ``FakeContainer.class_calls`` and ``FakeStylesheet.updated`` — and
``tests/test_preview_visibility.py`` asserts against them. Recording without an
assertion would be decoration.

``FakeStylesheet`` also applies ``-pre-reveal``'s opacity, because the descendant
cache bust is conditional on that rule actually moving: a stylesheet stub that
changed nothing would take the early return and the bust would never be reached.
"""

from __future__ import annotations

from typing import Any


class FakeStyles:
    """The rules ``visibility.py`` reads to decide whether to bust descendants."""

    def __init__(self) -> None:
        self.rules: dict[str, Any] = {}

    def get_rule(self, rule_name: str, default: Any = None) -> Any:
        return self.rules.get(rule_name, default)


class FakeDescendant:
    """Counts the two calls the bust makes on everything below the node."""

    def __init__(self) -> None:
        self.notified = 0
        self.refreshed = 0

    def notify_style_update(self) -> None:
        self.notified += 1

    def refresh(self) -> None:
        self.refreshed += 1


class FakeStylesheet:
    """Records restyle requests, and applies the one rule the bust turns on."""

    def __init__(self) -> None:
        self.updated: list[tuple[Any, ...]] = []

    def update_nodes(self, nodes: Any, animate: bool = False) -> None:
        nodes = tuple(nodes)
        self.updated.append(nodes)
        for node in nodes:
            # `-pre-reveal` is the only shortcut class declaring opacity, and an
            # absent rule reads as None, not as the 1.0 default: a stub that set
            # 1.0 here would bust on a `-hidden` flip that production leaves alone.
            if node.has_class("-pre-reveal"):
                node.styles.rules["opacity"] = 0.0
            else:
                node.styles.rules.pop("opacity", None)


class FakeApp:
    def __init__(self) -> None:
        self.stylesheet = FakeStylesheet()


class FakeContainer:
    """Stand-in for a PreviewContainer that only tracks its CSS classes."""

    def __init__(self, parent_doc_id: str = "fake0000") -> None:
        self.classes: set[str] = set()
        # Every flip as ``(name, update)``. The flag is the point: `update=True`
        # is what walks the whole subtree, and nothing else in the suite would
        # notice it coming back.
        self.class_calls: list[tuple[str, bool]] = []
        self.parent_doc_id = parent_doc_id
        self.app = FakeApp()
        self.styles = FakeStyles()
        self.descendants = [FakeDescendant(), FakeDescendant()]

    def query(self, selector: str) -> list[FakeDescendant]:
        return self.descendants

    def add_class(self, name: str, update: bool = True) -> None:
        self.class_calls.append((name, update))
        self.classes.add(name)

    def remove_class(self, name: str, update: bool = True) -> None:
        self.class_calls.append((name, update))
        self.classes.discard(name)

    def has_class(self, name: str) -> bool:
        return name in self.classes
