"""A small corpus where every documented query construct has known positive and
negative files: two collections, notes with frontmatter, a text file, a 22-page
PDF and an 8-slide deck, with staggered modified times. Words are chosen so no
two sit one edit apart, except the pairs a test needs (cryptography and
cryptographic, grey and gray, mitochondria and mitochondrial)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Final

DAY: Final = 86_400
FILLER: Final = " ".join(["pebble"] * 40)

# File name -> the one-letter code the expectations use.
LETTER: Final = {
    "zephyr.md": "Z",
    "quokka.md": "Q",
    "plain.md": "P",
    "report.txt": "R",
    "thesis_draft.pdf": "T",
    "deck.pptx": "D",
    "old.md": "O",
    "recent.md": "N",
}
# Notes: (text, age in days, or None for a fixed date below).
NOTES: Final = {
    "alpha/notes/zephyr.md": (
        "---\nProject: Mobile App\nYear: 2024\nType: Meeting\n"
        "tags: [urgent, meeting]\nDue Date: 2025-06-01\nauthor: dijkstra\n---\n"
        "# Zephyr Notes\n\n## Chapter 4\n\nThe zephyr crossed the tundra before dawn.\n\n"
        "A man in the middle attack intercepts traffic.\n\n### Proof\n\n"
        "The cross-entropy loss measures surprise. Cryptography keeps keys safe. "
        "Normalization helps.\n",
        3,
    ),
    "alpha/notes/quokka.md": (
        "---\nProject: Garden\nYear: 2023\nType: [Idea, Reading List]\n"
        "tags: [private]\n---\n"
        "# Quokka Guide\n\nThe quokka eats marmalade daily.\n\n"
        "The middle man was not in the attack.\n\n"
        f"Cross the river first. {FILLER} Then entropy appears. {FILLER} Finally the loss.\n\n"
        f"A grey lantern. {FILLER} The zephyr is here alone.\n",
        20,
    ),
    "alpha/notes/plain.md": (
        "# Plain Page\n\nObsidian falcon flies over the tundra. A gray lantern glows.\n"
        "Serialization of entropy figures.\n",
        200,
    ),
    "beta/notes/old.md": (
        "---\nProject: Home Renovation\ntags: private\n---\n"
        "# Old Ideas\n\nZephyr marmalade recipes. Mitochondria power cells.\n",
        None,
    ),
    "beta/notes/recent.md": (
        "# Recent Log\n\nQuokka sightings on the tundra. Cryptographic hashing. "
        "Mitochondrial DNA.\n",
        0,
    ),
}
FIXED_DATES: Final = {
    "alpha/docs/report.txt": "2025-03-01",
    "alpha/docs/thesis_draft.pdf": "2024-05-10",
    "beta/slides/deck.pptx": "2024-05-10",
    "beta/notes/old.md": "2023-03-01",
}
PDF_PAGES: Final = {
    1: "Falcon introduction to the thesis.",
    2: "The zephyr and the quokka together.",
    21: "Harbour lantern beacon.",
    22: "Harbour obsidian cliffs.",
}
SLIDES: Final = {1: "Lantern overview", 3: "Attention to obsidian", 7: "Attention to falcon"}


def build(root: Path) -> None:
    """Write the corpus under ``root`` (collections ``alpha`` and ``beta``)."""
    import pymupdf
    from pptx import Presentation

    now = time.time()
    for rel, (text, age) in NOTES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if age is not None:
            os.utime(path, (now - age * DAY, now - age * DAY))
    report = root / "alpha/docs/report.txt"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        "Marmalade and obsidian at the harbour.\n\nSerialization matters. Kubernetes clusters.\n",
        encoding="utf-8",
    )
    pdf = pymupdf.open()
    for n in range(1, 23):
        pdf.new_page().insert_text((72, 72), PDF_PAGES.get(n, f"Page {n} pebble."))
    pdf.set_metadata({"title": "Thesis Draft", "author": "knuth"})
    pdf.save(root / "alpha/docs/thesis_draft.pdf")
    deck = Presentation()
    for n in range(1, 9):
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = SLIDES.get(n, f"Slide {n}")  # pyright: ignore[reportOptionalMemberAccess]
        slide.placeholders[1].text = SLIDES.get(n, "pebble")  # pyright: ignore[reportAttributeAccessIssue]
    (root / "beta/slides").mkdir(parents=True, exist_ok=True)
    deck.save(str(root / "beta/slides/deck.pptx"))
    for rel, iso in FIXED_DATES.items():
        stamp = time.mktime(time.strptime(iso, "%Y-%m-%d"))
        os.utime(root / rel, (stamp, stamp))
