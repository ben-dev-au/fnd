"""Shared duck-typed stubs for the preview reveal/guard unit tests.

Several preview tests exercise ``PreviewPresenter.reveal`` / ``reveal_active`` as
unbound methods against minimal stand-ins (no full Textual app). The container
stub is identical across them, so it lives here to keep the class-flip behaviour
(``add_class`` / ``remove_class`` / ``has_class``) consistent in one place.

The stub mirrors the parts of Textual's API that ``preview/visibility.py`` uses,
including the ``update`` keyword and the ``app.stylesheet`` restyle that follows
it. Restyling means nothing to a stub that only holds a set of names, so it is
recorded rather than performed — which also lets a test assert that the shortcut
restyled the node itself and not its descendants.
"""

from __future__ import annotations

from typing import Any


class FakeStylesheet:
    """Records restyle requests instead of applying them."""

    def __init__(self) -> None:
        self.updated: list[tuple[Any, ...]] = []

    def update_nodes(self, nodes: Any, animate: bool = False) -> None:
        self.updated.append(tuple(nodes))


class FakeApp:
    def __init__(self) -> None:
        self.stylesheet = FakeStylesheet()


class FakeContainer:
    """Stand-in for a PreviewContainer that only tracks its CSS classes."""

    def __init__(self, parent_doc_id: str = "fake0000") -> None:
        self.classes: set[str] = set()
        self.parent_doc_id = parent_doc_id
        self.app = FakeApp()

    def add_class(self, name: str, update: bool = True) -> None:
        self.classes.add(name)

    def remove_class(self, name: str, update: bool = True) -> None:
        self.classes.discard(name)

    def has_class(self, name: str) -> bool:
        return name in self.classes
