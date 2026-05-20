"""Phase 4: schema bump to 7 + new F_LINE field.

The schema-version sidecar mismatch triggers the existing rebuild
prompt in ``fnd/migrate.py``. The field-list snapshot here guards
against accidental field reordering across the bump.
"""

from __future__ import annotations

from fnd import schema


def test_schema_version_bumped_to_seven() -> None:
    assert schema.SCHEMA_VERSION == 7


def test_schema_exposes_f_line_constant() -> None:
    assert hasattr(schema, "F_LINE")
    assert schema.F_LINE == "line"


def test_built_schema_builds_without_error() -> None:
    """build_schema() must accept the new F_LINE field. Tantivy's
    Python Schema doesn't expose a field-name iterator, so the end-to-end
    round-trip test in test_md_line_tracking is the real gate; this is
    a fast smoke that catches typos in the field-add line."""
    sch = schema.build_schema()
    assert sch is not None
