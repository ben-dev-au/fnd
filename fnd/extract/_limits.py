"""Hard limits applied to untrusted documents before they reach a parser.

Centralised so the same numbers cover DOCX, PPTX, frontmatter, and the
query layer — and so they're easy to tune from one place if a real-world
file legitimately exceeds them.

Numbers are conservative: a legitimate office document doesn't approach
500 MB uncompressed, doesn't have a single entry that expands 200× from
its compressed size, and doesn't carry a 64 KB YAML frontmatter block.
A document that does trip a limit is either pathological or hostile —
either way we'd rather refuse and tell the user than feed the parser.
"""

from __future__ import annotations

from typing import Final

# OOXML containers (DOCX, PPTX) are ZIP archives. Two-pronged check:
# (1) total uncompressed size across all members, (2) any single member's
# compression ratio. (1) catches a 1 MB archive that expands to 10 GB of
# text; (2) catches a smaller archive whose single XML part expands
# pathologically.
LIMIT_OOXML_TOTAL_UNCOMPRESSED: Final = 500 * 1024 * 1024  # 500 MB
LIMIT_OOXML_ENTRY_RATIO: Final = 200  # uncompressed / compressed

# Frontmatter is read before the markdown parser. A 100 MB frontmatter
# block would OOM the indexer; legitimate Obsidian/Jekyll frontmatter
# is rarely over a kilobyte. 64 KB is more headroom than any human-
# authored frontmatter I've encountered.
LIMIT_FRONTMATTER_TOTAL_BYTES: Final = 64 * 1024  # 64 KB
LIMIT_FRONTMATTER_LINE_BYTES: Final = 4 * 1024  # 4 KB single line

# Query layer. Today the only consumer is the same user typing into the
# TUI, but once we add Spotlight / URL-handler / --query-from-file the
# query becomes attacker-controllable and these bounds matter. Tantivy's
# parser will compile arbitrarily-deep boolean trees and arbitrarily-
# long phrase queries — the limits below cap both before parsing.
LIMIT_QUERY_BYTES: Final = 8 * 1024  # 8 KB raw query string
LIMIT_QUERY_BOOLEAN_TOKENS: Final = 64  # AND/OR/NOT count
