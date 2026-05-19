"""Collection-name validator covers two attack surfaces:

- TOML key injection. The TOML writer interpolates the name as
  ``[collections.<name>]``; a name containing ``]``, ``=``, or a
  newline would either corrupt the file or coax a phantom top-level
  table.
- Query DSL ambiguity. The ``c:<name>`` shorthand splits on ``,`` and
  matches against ``[A-Za-z0-9_,\\-]+``; a non-conforming name would
  fail to round-trip from the config back through the DSL.

(S3)
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
    ["default", "papers", "papers-2024", "team_notes", "a", "Z9", "ABC123"],
)
def test_validator_accepts_canonical_names(good: str) -> None:
    assert validate_collection_name(good) == good


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "-leading",  # leading hyphen
        "with space",  # whitespace
        "comma,split",  # DSL separator
        "with.dot",  # not in approved set
        "with/slash",  # path-component
        "with[bracket]",  # TOML key injection
        'with"quote',  # TOML key injection
        "with\nnewline",  # line-break injection
        "x" * 65,  # over length cap
    ],
)
def test_validator_rejects_unsafe_names(bad: str) -> None:
    with pytest.raises(InvalidCollectionNameError):
        validate_collection_name(bad)


def test_write_collection_source_rejects_unsafe_name(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    src = SourceConfig(path=tmp_path)
    with pytest.raises(InvalidCollectionNameError):
        write_collection_source(
            config_path=cfg,
            collection_name="evil ] = injected",
            source=src,
        )
    # No partial file written.
    assert not cfg.exists()
