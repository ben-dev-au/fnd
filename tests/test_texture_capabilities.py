"""An empty texturising is a fact about the ENGINE as much as about the file.

Measured on this corpus: 18 PDFs hold an empty texture under the current
signature, all written while pymupdf4llm resolved to 1.27.2.3, and every one of
them texturises under 1.28.2. ``texture_signature`` is coarse on purpose — it
does not move with the engine — so without this the emptiness is served forever.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.cache import ExtractionCache
from fnd.extract import pdf
from fnd.extract.base import Block, Chunk

# Resolved through the module on every call, never bound at import:
# tests/test_pdf_extras_optional.py drops ``fnd.extract.pdf`` from sys.modules,
# so a name bound here can outlive the module a later monkeypatch patches.


def _chunk(seq: int = 0, *, body_md: str = "") -> Chunk:
    return Chunk(
        parent_id="abc",
        path="/x.pdf",
        mtime=1,
        kind="pdf",
        body=f"page {seq} flat text",
        body_struct=[Block(kind="p", text="prose")],
        body_md=body_md,
        page=seq + 1,
        chunk_seq=seq,
    )


def _cache(tmp_path: Path) -> ExtractionCache:
    return ExtractionCache(root=tmp_path / "cache")


def test_the_fingerprint_round_trips_through_the_entry(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    seen: list[dict[str, str]] = []
    cache.put("k", [_chunk(body_md="# x")], fingerprint={"engine": "1.27.2.3"})

    cache.get("k", accept=lambda _chunks, recorded: seen.append(recorded) is None)

    assert seen == [{"engine": "1.27.2.3"}]


def test_an_empty_texturising_is_refused_once_the_engine_moves(tmp_path: Path) -> None:
    """The 18-PDF case: written by 1.27.2.3, read back under 1.28.2."""
    cache = _cache(tmp_path)
    cache.put("k", [_chunk()], fingerprint={"engine": "1.27.2.3"})

    got = cache.get(
        "k",
        accept=lambda c, rec: pdf.texture_reusable(c, rec, now={"engine": "1.28.2"}),
    )

    assert got is None


def test_an_empty_texturising_is_kept_while_the_engine_is_the_same(tmp_path: Path) -> None:
    """Otherwise a PDF this engine genuinely cannot texture re-runs it on every
    index — the cost the stamp exists to avoid."""
    cache = _cache(tmp_path)
    cache.put("k", [_chunk()], fingerprint={"engine": "1.28.2"})

    got = cache.get(
        "k",
        accept=lambda c, rec: pdf.texture_reusable(c, rec, now={"engine": "1.28.2"}),
    )

    assert got is not None


def test_a_textured_entry_is_kept_however_the_engine_moved(tmp_path: Path) -> None:
    """Reuse of real texturings is what keeps an upgrade from orphaning the
    corpus, which is why the signature is coarse in the first place."""
    cache = _cache(tmp_path)
    cache.put("k", [_chunk(body_md="# real")], fingerprint={"engine": "1.0.0"})

    got = cache.get(
        "k",
        accept=lambda c, rec: pdf.texture_reusable(c, rec, now={"engine": "1.28.2"}),
    )

    assert got is not None


def test_an_entry_from_before_the_stamp_is_retried_once() -> None:
    """Every entry on disk today records nothing, which is what heals the 18."""
    assert not pdf.texture_reusable([_chunk()], {}, now={"engine": "1.28.2"})


def test_a_run_that_cannot_texture_refuses_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Battery-saver ("Update cache at index time" off) and a machine without the
    extra both run flat-only. Refusing there re-extracts the file on every index
    and never settles, because such a run does not write the entry back."""
    monkeypatch.setattr(pdf, "_skip_structure_extraction", True)

    assert pdf.texture_reusable([_chunk()], {}, now={"engine": "1.28.2"})


def test_the_fingerprint_names_the_engine() -> None:
    fp = pdf.texture_fingerprint()

    assert set(fp) == {"engine"}
    assert isinstance(fp["engine"], str)


def _pdf_with_text(path: Path) -> None:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Real page text from the file itself.")
    doc.save(str(path))
    doc.close()


def _seed(
    tmp_path: Path, pdf_path: Path, fingerprint: dict[str, str], *, signature: str = "old-sig"
) -> ExtractionCache:
    """A cache holding an EMPTY texturising for this file, under ``signature``."""
    from fnd.cache import sha256_file

    cache = _cache(tmp_path)
    stale = Chunk(
        parent_id="old",
        path=str(pdf_path),
        mtime=1,
        kind="pdf",
        body="CACHED SENTINEL BODY",
        body_struct=[Block(kind="p", text="CACHED SENTINEL BODY")],
        body_md="",
        page=1,
        chunk_seq=0,
    )
    key = cache.build_key(content_sha256=sha256_file(pdf_path), extractor_signature=signature)
    cache.put(key, [stale], fingerprint=fingerprint)
    return cache


def test_extract_re_texturises_an_entry_under_the_current_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path the 18 real entries take. They sit under the CURRENT signature,
    so they arrive at ``cache.get(key)`` — seeding under an old signature tests
    durable reuse instead, and leaves this line unguarded."""
    from fnd.extract import pdf

    pdf_path = tmp_path / "doc.pdf"
    _pdf_with_text(pdf_path)
    cache = _seed(tmp_path, pdf_path, {"engine": "1.27.2.3"}, signature=pdf.texture_signature())
    monkeypatch.setattr(pdf, "_cache_singleton", cache)
    monkeypatch.setattr(pdf, "_force_fresh_texture", False)

    chunks = list(pdf.extract(pdf_path))

    assert chunks, "extraction produced nothing"
    assert "CACHED SENTINEL BODY" not in chunks[0].body, "served the stale empty texturising"
    assert "Real page text" in chunks[0].body


def test_extract_keeps_an_empty_entry_from_the_same_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PDF that genuinely cannot texturise under this engine must not re-run
    it on every index — the cost the stamp exists to avoid."""
    from fnd.extract import pdf

    pdf_path = tmp_path / "doc.pdf"
    _pdf_with_text(pdf_path)
    cache = _seed(tmp_path, pdf_path, pdf.texture_fingerprint(), signature=pdf.texture_signature())
    monkeypatch.setattr(pdf, "_cache_singleton", cache)
    monkeypatch.setattr(pdf, "_force_fresh_texture", False)

    chunks = list(pdf.extract(pdf_path))

    assert chunks
    assert "CACHED SENTINEL BODY" in chunks[0].body, "re-ran the engine with nothing to gain"


def test_a_written_entry_records_what_produced_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamp has to be WRITTEN, not just read. An entry saved without it
    records no capabilities, so an empty texturising is refused on every index
    and the engine re-runs forever on a PDF that cannot texturise here.

    Asserted on the entry as it lands on disk: the heavy extraction runs in a
    subprocess, so a spy on the texturiser in this process never fires.
    """
    import json

    from fnd.cache import sha256_file
    from fnd.extract import pdf

    pdf_path = tmp_path / "doc.pdf"
    _pdf_with_text(pdf_path)
    cache = _cache(tmp_path)
    monkeypatch.setattr(pdf, "_cache_singleton", cache)
    monkeypatch.setattr(pdf, "_force_fresh_texture", False)

    list(pdf.extract(pdf_path))

    key = cache.build_key(
        content_sha256=sha256_file(pdf_path), extractor_signature=pdf.texture_signature()
    )
    blob = json.loads(cache.entry_path(key).read_text(encoding="utf-8"))
    assert blob["fingerprint"] == pdf.texture_fingerprint()


def test_a_refused_entry_is_not_read_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Durable reuse globs every signature for this content, which includes the
    entry the current-signature read just refused — a second full decode of the
    same blob (143 chunks on the largest of the 18) for no possible gain."""
    from fnd.extract import pdf

    pdf_path = tmp_path / "doc.pdf"
    _pdf_with_text(pdf_path)
    cache = _seed(tmp_path, pdf_path, {"engine": "1.27.2.3"}, signature=pdf.texture_signature())
    monkeypatch.setattr(pdf, "_cache_singleton", cache)
    monkeypatch.setattr(pdf, "_force_fresh_texture", False)

    list(pdf.extract(pdf_path))

    assert cache.misses == 1, f"the refused entry was decoded {cache.misses} times"
