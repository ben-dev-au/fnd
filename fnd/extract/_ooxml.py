"""Shared safety prechecks for OOXML (DOCX, PPTX) extractors.

DOCX and PPTX are ZIP archives. `python-docx` / `python-pptx` open them
without any size or ratio bound; a 1 MB archive that declares 10 GB of
inflation will OOM the indexer before any text is extracted. We read
only the central directory (no decompression) and refuse to hand the
file off if either of the two limits in `_limits.py` is tripped.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from fnd.extract._limits import (
    LIMIT_OOXML_ENTRY_RATIO,
    LIMIT_OOXML_TOTAL_UNCOMPRESSED,
)
from fnd.extract.base import ExtractError


def reject_if_zip_bomb(path: Path) -> None:
    """Inspect the ZIP central directory; raise ``ExtractError`` on
    threshold trip. Does not decompress."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as e:
        raise ExtractError(str(path), f"not a valid OOXML zip: {e}") from e

    total = sum(zi.file_size for zi in infos)
    if total > LIMIT_OOXML_TOTAL_UNCOMPRESSED:
        raise ExtractError(
            str(path),
            f"OOXML uncompressed size {total} > limit {LIMIT_OOXML_TOTAL_UNCOMPRESSED}",
        )
    for zi in infos:
        if zi.compress_size == 0:
            continue
        ratio = zi.file_size / zi.compress_size
        if ratio > LIMIT_OOXML_ENTRY_RATIO:
            raise ExtractError(
                str(path),
                f"OOXML entry {zi.filename!r} ratio {ratio:.0f}x exceeds "
                f"{LIMIT_OOXML_ENTRY_RATIO}x limit",
            )
