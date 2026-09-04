"""Tantivy schema definition — single source of truth for the index format.

Changing fields = full reindex; the writer compares
``SCHEMA_VERSION`` against the index sidecar and refuses to load on mismatch.

Fields:

================  =======  ========  ======  ====  ============================================
Field             Type     Indexed   Stored  Fast  Purpose
================  =======  ========  ======  ====  ============================================
parent_id         text     raw       yes     yes   groups chunks per file (sha1 of abs path)
collection        text     raw       yes     yes   collection scoping
path              text     raw       yes     no    filesystem path (display + open)
path_tokens       text     default   no      no    tokenized for path matching
mtime             u64      yes       yes     yes   incremental skip + recency boost
kind              text     raw       yes     yes   fine-grained file-type id (see fnd.kinds.KIND_SPECS)
page              u64      yes       yes     yes   PDF page index (1-based); 0 = N/A
page_label        text     raw       yes     no    printed page label (e.g. "292" or "iv"); "" if N/A
slide             u64      yes       yes     yes   1-based; 0 = N/A
heading_path      text     default   yes     no    DOCX/MD section path (boosted)
title             text     default   yes     no    metadata title (boosted)
author            text     default   yes     no    metadata author
body              text     en_stem   no      no    full-text ranking
body_struct       bytes    no        yes     no    JSON-encoded blocks for snippet generation
body_md           bytes    no        yes     no    UTF-8 markdown source for the preview pane
meta_blob         bytes    no        yes     no    JSON frontmatter for query-time filter
chunk_seq         u64      yes       yes     yes   ordering within a file
================  =======  ========  ======  ====  ============================================
"""

from __future__ import annotations

from typing import Final

from tantivy import Schema, SchemaBuilder

# Bump on any field-shape change; indexer refuses to open a stale index.
# v7 (2026-05-19): added F_LINE for MD / TXT line-locator deep links via
# user app templates like `code -g {path}:{line}:1`.
# v8 (2026-07-19): added F_CREATED (birthtime) + F_INODE_CTIME (metadata
# freshness) and the two tag fields. Tags are split by provenance so
# toggling a source in config is a query-time change, not a reindex.
# v9 (2026-07-23): fine-grained kinds + new suffixes (epub, html, ipynb, odf,
# per-language code, data/config). No field or schema-shape change — the bump
# forces a reindex so existing collections pick up the newly-supported files.
SCHEMA_VERSION: Final[int] = 9

# Field-name constants so callers don't sprinkle string literals.
F_PARENT_ID: Final = "parent_id"
F_COLLECTION: Final = "collection"
F_SOURCE_PATH: Final = "source_path"
F_PATH: Final = "path"
F_PATH_TOKENS: Final = "path_tokens"
F_MTIME: Final = "mtime"
# st_birthtime. 0 = unknown (non-Darwin, or stat failure), which falls
# outside every range query, so such files match only the "any" bucket.
F_CREATED: Final = "created"
# st_ctime. Bookkeeping only, never queried: a Finder retag moves ctime
# but not mtime, so the incremental skip needs both to notice it.
F_INODE_CTIME: Final = "inode_ctime"
# Tags, one field per provenance. Multi-valued, raw tokenizer (values
# contain "/" and spaces). Kept out of DEFAULT_SEARCH_FIELDS so a bare
# keyword search doesn't match on them.
F_TAGS_FM: Final = "tags_fm"
F_TAGS_OS: Final = "tags_os"
F_KIND: Final = "kind"
F_PAGE: Final = "page"
F_PAGE_LABEL: Final = "page_label"
F_SLIDE: Final = "slide"
# 1-based source line of the chunk's first character (MD heading-open
# line, TXT chunk window start). 0 for kinds without line tracking
# (PDF, DOCX, PPTX) — they have page/slide/heading_path instead.
F_LINE: Final = "line"
F_HEADING_PATH: Final = "heading_path"
F_TITLE: Final = "title"
F_AUTHOR: Final = "author"
F_BODY: Final = "body"
F_BODY_STRUCT: Final = "body_struct"
F_BODY_MD: Final = "body_md"
F_META_BLOB: Final = "meta_blob"
F_CHUNK_SEQ: Final = "chunk_seq"

# Tag provider id -> field. Defined once here because both the writer
# (fnd/index.py) and the reader (fnd/tag_query.py) need it and they must
# never disagree. Unknown ids are ignored by both, so a provider added in
# a newer build can't break an older reader.
TAG_FIELD_BY_SOURCE: Final[dict[str, str]] = {
    "frontmatter": F_TAGS_FM,
    "os": F_TAGS_OS,
}

# Default search fields when the query has no explicit field qualifier.
DEFAULT_SEARCH_FIELDS: Final[list[str]] = [F_BODY, F_TITLE, F_HEADING_PATH, F_PATH_TOKENS]

# Default per-field boosts applied at parse_query time.
DEFAULT_FIELD_BOOSTS: Final[dict[str, float]] = {
    F_HEADING_PATH: 2.0,
    F_TITLE: 2.5,
    F_PATH_TOKENS: 1.5,
    F_BODY: 1.0,
}


def build_schema() -> Schema:
    sb = SchemaBuilder()

    # Exact-match identifiers (raw tokenizer = single-term).
    sb.add_text_field(F_PARENT_ID, stored=True, fast=True, tokenizer_name="raw")
    sb.add_text_field(F_COLLECTION, stored=True, fast=True, tokenizer_name="raw")
    sb.add_text_field(F_SOURCE_PATH, stored=True, fast=True, tokenizer_name="raw")
    sb.add_text_field(F_KIND, stored=True, fast=True, tokenizer_name="raw")
    # Stored-only printed page label; raw tokenizer keeps it untouched.
    sb.add_text_field(F_PAGE_LABEL, stored=True, tokenizer_name="raw")

    # Display-only path; raw tokenizer keeps it stored without weird tokenization.
    sb.add_text_field(F_PATH, stored=True, tokenizer_name="raw")

    # Tokenized fields — default tokenizer (lowercase + simple).
    sb.add_text_field(F_PATH_TOKENS, stored=False, tokenizer_name="default")
    sb.add_text_field(F_HEADING_PATH, stored=True, tokenizer_name="default")
    sb.add_text_field(F_TITLE, stored=True, tokenizer_name="default")
    sb.add_text_field(F_AUTHOR, stored=True, tokenizer_name="default")

    # Body uses Snowball English stemmer for query-stemming.
    sb.add_text_field(F_BODY, stored=False, tokenizer_name="en_stem")

    # Tags: raw tokenizer so "project/alpha" and "two words" stay single
    # exact terms. fast=True powers the Tags pane's terms aggregation.
    sb.add_text_field(F_TAGS_FM, stored=True, fast=True, tokenizer_name="raw")
    sb.add_text_field(F_TAGS_OS, stored=True, fast=True, tokenizer_name="raw")

    # Numeric fast fields.
    sb.add_unsigned_field(F_MTIME, stored=True, indexed=True, fast=True)
    sb.add_unsigned_field(F_CREATED, stored=True, indexed=True, fast=True)
    # Stored-only: read back by the incremental skip, never queried.
    sb.add_unsigned_field(F_INODE_CTIME, stored=True, indexed=False, fast=False)
    sb.add_unsigned_field(F_PAGE, stored=True, indexed=True, fast=True)
    sb.add_unsigned_field(F_SLIDE, stored=True, indexed=True, fast=True)
    sb.add_unsigned_field(F_LINE, stored=True, indexed=True, fast=True)
    sb.add_unsigned_field(F_CHUNK_SEQ, stored=True, indexed=True, fast=True)

    # Stored bytes for the structured-preview JSON (snippet generation).
    sb.add_bytes_field(F_BODY_STRUCT, stored=True, indexed=False)

    # Stored UTF-8 markdown source for the preview pane's structural
    # renderer (Textual Markdown widget). Distinct from body_struct so
    # the snippet pipeline (which wants plain text) and the renderer
    # (which wants full markdown structure) don't fight over one field.
    sb.add_bytes_field(F_BODY_MD, stored=True, indexed=False)

    # JSON-encoded frontmatter for the query-time metadata filter.
    sb.add_bytes_field(F_META_BLOB, stored=True, indexed=False)

    return sb.build()
