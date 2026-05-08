"""``body_struct`` JSON encoding for the preview pane.

The schema stores ``body_struct`` as bytes; we serialize the small list of
:class:`Block` records to compact JSON.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from acorn.extract.base import Block


def encode(blocks: list[Block]) -> bytes:
    return json.dumps([asdict(b) for b in blocks], ensure_ascii=False).encode("utf-8")


def decode(data: bytes) -> list[Block]:
    raw = json.loads(data.decode("utf-8"))
    return [Block(**item) for item in raw]
