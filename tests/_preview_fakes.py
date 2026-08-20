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
        # Every flip as ``(name, update)``. The flag is the point: `update=True`
        # is what walks the whole subtree, and nothing else in the suite would
        # notice it coming back.
        self.class_calls: list[tuple[str, bool]] = []
        self.parent_doc_id = parent_doc_id
        self.app = FakeApp()

    def add_class(self, name: str, update: bool = True) -> None:
        self.class_calls.append((name, update))
        self.classes.add(name)

    def remove_class(self, name: str, update: bool = True) -> None:
        self.class_calls.append((name, update))
        self.classes.discard(name)

    def has_class(self, name: str) -> bool:
        return name in self.classes
