"""Value-level normalization for the shared extracted facts (Stage 1).

The classifier persists provider-shaped values (``5842-018-024``,
``123 Main Street, ...``), but the consistency rules compare facts
*across* documents and against the listing file's own columns, so the
five shared fact fields are canonicalised once into
``documents.normalized_data`` at extraction time.  Comparison is exact
after this pass -- no fuzzy or phonetic matching (V0 boundary).

Readers must go through :func:`document_normalized_fields`, which falls
back to normalizing ``extracted_data`` on the fly for rows extracted
before this slice.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Callable, Optional

from app.services.text_sanitization import ensure_jsonb_value

if TYPE_CHECKING:
    from app.db.models import Document

#: Street-suffix canonicalisation (post-office style): token -> abbreviation.
_STREET_SUFFIXES: dict[str, str] = {
    "street": "st",
    "avenue": "ave",
    "road": "rd",
    "boulevard": "blvd",
    "drive": "dr",
    "lane": "ln",
    "court": "ct",
    "place": "pl",
    "circle": "cir",
    "parkway": "pkwy",
    "highway": "hwy",
    "freeway": "fwy",
    "expressway": "expy",
    "terrace": "ter",
    "trail": "trl",
    "square": "sq",
    "alley": "aly",
    "junction": "jct",
}

#: Punctuation becomes a separator so tokens never fuse ("St., Pas" -> "st pas").
_PUNCT_TO_SPACE = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")

#: Date formats worth recognizing; anything else is kept verbatim (V0).
_DATE_FORMATS: tuple[str, ...] = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y")


def normalize_address(value: Any) -> Optional[str]:
    """Lowercase, strip punctuation, standardise street suffixes, collapse spaces."""
    if value is None:
        return None
    text = _WHITESPACE.sub(" ", _PUNCT_TO_SPACE.sub(" ", str(value).casefold())).strip()
    return " ".join(_STREET_SUFFIXES.get(token, token) for token in text.split(" "))


def normalize_apn(value: Any) -> Optional[str]:
    """Assessor parcel number without dashes/spaces, uppercased."""
    if value is None:
        return None
    return re.sub(r"[-\s]", "", str(value)).upper()


def normalize_person_name(value: Any) -> Optional[str]:
    """Lowercase, punctuation to spaces, collapse whitespace."""
    if value is None:
        return None
    text = _PUNCT_TO_SPACE.sub(" ", str(value).casefold())
    return _WHITESPACE.sub(" ", text).strip()


def normalize_date(value: Any) -> Optional[str]:
    """ISO date when one can be parsed, otherwise the trimmed original."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(text, date_format).date().isoformat()
        except ValueError:
            continue
    return text


#: The shared fact fields, keyed by their ``ListingDocumentFields`` name.
SHARED_FACT_FIELDS: dict[str, Callable[[Any], Optional[str]]] = {
    "property_address": normalize_address,
    "apn": normalize_apn,
    "seller_name": normalize_person_name,
    "listing_agent_name": normalize_person_name,
    "document_date": normalize_date,
}


def normalize_extracted_data(extracted_data: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Canonical forms of the shared fact fields present in ``extracted_data``.

    Absent fields and null values are omitted: ``normalized_data`` carries
    only comparable facts, never holes.
    """
    if not extracted_data:
        return {}
    normalized: dict[str, Any] = {}
    for field_name, normalize in SHARED_FACT_FIELDS.items():
        if field_name not in extracted_data or extracted_data[field_name] is None:
            continue
        value = normalize(extracted_data[field_name])
        if value not in (None, ""):
            normalized[field_name] = value
    return normalized


def document_normalized_fields(document: "Document") -> dict[str, Any]:
    """The document's canonical fact fields.

    Reads ``documents.normalized_data`` when present; rows extracted before
    this slice normalize their ``extracted_data`` on the fly instead.
    """
    if document.normalized_data is not None:
        return document.normalized_data
    return normalize_extracted_data(document.extracted_data)


def ensure_extracted_fields(extracted_data: dict[str, Any]) -> dict[str, Any]:
    """Validate raw provider fields and their canonical view together.

    Every ``extracted_data`` persist site (routes classify/extract, the
    pipeline's OCR and classification stages) calls this and assigns both
    columns with its other validated writes, so the two columns can never
    diverge.  Returns the canonical view.  Validation runs here; sites
    assign only after *all* their JSONB writes have validated, so a
    rejection anywhere leaves the document exactly as it was.
    """
    ensure_jsonb_value(extracted_data, "documents.extracted_data")
    normalized = normalize_extracted_data(extracted_data)
    ensure_jsonb_value(normalized, "documents.normalized_data")
    return normalized
