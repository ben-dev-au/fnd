"""Config + filesystem locations.

Phase 1 minimum: just the default index directory under ``platformdirs``.
Phase 3 fills in the full TOML config schema (§6) for collections and
ranking profiles.
"""

from __future__ import annotations

from pathlib import Path

from platformdirs import user_data_dir

_APP_NAME = "acorn"


def app_data_dir() -> Path:
    return Path(user_data_dir(_APP_NAME, appauthor=False))


def default_index_dir() -> Path:
    d = app_data_dir() / "index"
    d.mkdir(parents=True, exist_ok=True)
    return d
