"""Frontmatter size limits — caps protect the markdown extractor from
adversarial files whose YAML-like frontmatter expands into memory before
markdown-it ever sees them. (S7)"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from fnd import frontmatter
from fnd.frontmatter import FrontmatterParseError, read_frontmatter_from_text


def test_frontmatter_total_size_rejected() -> None:
    big_key = "x" * (4 * 1024)
    body = "\n".join(f"{big_key}{i}: y" for i in range(32))
    doc = f"---\n{body}\n---\nbody\n"
    with pytest.raises(FrontmatterParseError, match="exceeds"):
        read_frontmatter_from_text(doc)


def test_frontmatter_long_line_rejected() -> None:
    line = "k: " + "v" * (5 * 1024)
    doc = f"---\n{line}\n---\n"
    with pytest.raises(FrontmatterParseError, match="exceeds"):
        read_frontmatter_from_text(doc)


def test_frontmatter_normal_size_still_parses() -> None:
    doc = "---\ntitle: hello\ntags: [a, b]\n---\nbody\n"
    out = read_frontmatter_from_text(doc)
    assert out is not None
    assert out["title"] == "hello"


def test_frontmatter_no_fence_returns_none() -> None:
    assert read_frontmatter_from_text("just a body\n") is None


def test_frontmatter_limit_respects_patched_value() -> None:
    """Confirm the limit is consulted at call time, not import time —
    so users can tune it from config or environment in future."""
    short_limit = 32
    with patch.object(
        frontmatter,
        "read_frontmatter_from_text",
        wraps=read_frontmatter_from_text,
    ):
        doc = "---\n" + "k: v\n" * 20 + "---\n"
        # 20 * 5 bytes = 100 bytes of body alone — guarantees we trip 32.
        from fnd.extract import _limits

        with patch.object(_limits, "LIMIT_FRONTMATTER_TOTAL_BYTES", short_limit):
            with pytest.raises(FrontmatterParseError, match="exceeds"):
                read_frontmatter_from_text(doc)
