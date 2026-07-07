"""Shared duck-typed stubs for the preview reveal/guard unit tests.

Several preview tests exercise ``PreviewPresenter.reveal`` / ``reveal_active`` as
unbound methods against minimal stand-ins (no full Textual app). The container
stub is identical across them, so it lives here to keep the class-flip behaviour
(``add_class`` / ``remove_class`` / ``has_class``) consistent in one place.
"""

from __future__ import annotations


class FakeContainer:
    """Stand-in for a PreviewContainer that only tracks its CSS classes."""

    def __init__(self, parent_doc_id: str = "fake0000") -> None:
        self.classes: set[str] = set()
        self.parent_doc_id = parent_doc_id

    def add_class(self, name: str) -> None:
        self.classes.add(name)

    def remove_class(self, name: str) -> None:
        self.classes.discard(name)

    def has_class(self, name: str) -> bool:
        return name in self.classes
