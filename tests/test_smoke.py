"""Sanity that the package imports and exposes a version."""

from __future__ import annotations


def test_package_imports_and_has_version() -> None:
    import fnd

    assert isinstance(fnd.__version__, str)
    assert fnd.__version__
