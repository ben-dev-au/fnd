"""Sanity that the package imports and exposes a version."""

from __future__ import annotations


def test_package_imports_and_has_version() -> None:
    import acorn

    assert isinstance(acorn.__version__, str)
    assert acorn.__version__
