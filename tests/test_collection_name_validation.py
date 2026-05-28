"""Collection-name validator: permissive on display, strict on injection.

After the relaxation in fix/indexer-freeze-and-ctrl-c the rule allows
internal spaces and most printable characters so users can name a
collection "Soft Eng Textbooks". The forbidden set is narrowed to
characters that would actually break a downstream consumer:

- ``/`` and ``\\`` — collide with the per-collection state file path
- quote characters and backticks — TOML / shell / DSL quoting conflict
- ``,`` — the ``c:<a>,<b>`` DSL list separator
- control characters and DEL — never useful

The first character must still be an ASCII alnum or underscore so the
name can't start with a hyphen (CLI flag collision), a dot (hidden file
collision), or whitespace. (S3)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import (
    InvalidCollectionNameError,
    SourceConfig,
    validate_collection_name,
    write_collection_source,
)


@pytest.mark.parametrize(
    "good",
    [
        # Canonical from before the relaxation
        "default",
        "papers",
        "papers-2024",
        "team_notes",
        "a",
        "Z9",
        "ABC123",
        # Newly permitted under the relaxed contract
        "Soft Eng Textbooks",  # internal spaces
        "papers (2024)",  # parentheses
        "team.notes",  # interior dot
        "papers_α",  # non-ASCII allowed after the first char
        "x" * 64,  # at length cap
    ],
)
def test_validator_accepts_canonical_names(good: str) -> None:
    assert validate_collection_name(good) == good


@pytest.mark.parametrize(
    ("bad", "expected_substring"),
    [
        ("", "must not be empty"),
        ("-leading", "must start with"),
        (".leading", "must start with"),
        (" leading", "must start with"),
        ("trailing ", "must not end with"),
        ("trailing.", "must not end with"),
        ("comma,split", "forbidden"),
        ("with/slash", "forbidden"),
        ("with\\back", "forbidden"),
        ('with"quote', "forbidden"),
        ("with'quote", "forbidden"),
        ("with`tick", "forbidden"),
        ("with\nnewline", "forbidden"),
        ("with\ttab", "forbidden"),
        ("x" * 65, "exceeds"),
    ],
)
def test_validator_rejects_unsafe_names(bad: str, expected_substring: str) -> None:
    with pytest.raises(InvalidCollectionNameError) as exc_info:
        validate_collection_name(bad)
    assert expected_substring in str(exc_info.value)


def test_write_collection_source_rejects_unsafe_name(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    src = SourceConfig(path=tmp_path)
    with pytest.raises(InvalidCollectionNameError):
        write_collection_source(
            config_path=cfg,
            collection_name="evil/injected",
            source=src,
        )
    # No partial file written.
    assert not cfg.exists()
