"""Hardened XML parsing for untrusted documents.

EPUB and ODF files are ZIP containers whose XML (``container.xml``, the OPF,
``content.xml`` …) is attacker-controlled when a user indexes a downloaded book
or shared document. Parsing that with lxml's default parser leaves the door open
to entity-expansion ("billion laughs") and external-entity (XXE) attacks, so
every untrusted parse goes through :func:`parse_xml`, which disables entity
resolution, DTD loading and network access.
"""

from __future__ import annotations

from lxml import etree


def _safe_parser() -> etree.XMLParser:
    # resolve_entities=False stops entity expansion (billion laughs) and
    # internal-entity XXE; no_network + no DTD loading block external entities.
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
    )


def parse_xml(data: bytes) -> etree._Element:
    """Parse ``data`` as XML with a hardened parser (safe for untrusted input)."""
    return etree.fromstring(data, parser=_safe_parser())
