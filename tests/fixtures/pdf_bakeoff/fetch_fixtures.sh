#!/usr/bin/env bash
# Download a small permissively-licensed PDF set into the user cache.
# Outside the repo so `git clean` doesn't wipe it.
#
# Usage: bash tests/fixtures/pdf_bakeoff/fetch_fixtures.sh

set -euo pipefail

CACHE_DIR="${HOME}/Library/Caches/fnd/bakeoff/fixtures"
mkdir -p "$CACHE_DIR"/{single_col,multi_col,tables,slides,scanned_ocr,weird}

fetch() {
    local url="$1"
    local dest="$2"
    if [[ -f "$dest" ]]; then
        echo "[skip] $dest already exists"
        return 0
    fi
    echo "[get]  $url -> $dest"
    curl -fsSL --retry 3 -o "$dest" "$url"
}

# arXiv — multi-column scientific papers (CC-licensed abstracts; check
# individual paper licenses if redistributing).
fetch "https://arxiv.org/pdf/2103.00020v1.pdf" "$CACHE_DIR/multi_col/clip-radford-2021.pdf"
fetch "https://arxiv.org/pdf/1706.03762v7.pdf" "$CACHE_DIR/multi_col/attention-vaswani-2017.pdf"

# IRS public forms — table-heavy, US-Government Public Domain.
fetch "https://www.irs.gov/pub/irs-pdf/f1040.pdf" "$CACHE_DIR/tables/irs-f1040.pdf"
fetch "https://www.irs.gov/pub/irs-pdf/f1040sb.pdf" "$CACHE_DIR/tables/irs-f1040sb.pdf"

# Project Gutenberg PDF — long-form single-column, Public Domain.
fetch "https://www.gutenberg.org/files/1342/1342-pdf.pdf" "$CACHE_DIR/single_col/pride-and-prejudice.pdf"

echo
echo "Done. Run the bake-off:"
echo "  uv run python -m tools.pdf_bakeoff $CACHE_DIR /tmp/bakeoff-fixtures --pages-per-pdf 5"
