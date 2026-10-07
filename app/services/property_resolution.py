"""Stage 2: resolve a document against the file's property identity.

V0 is exact-after-normalization: the document's canonical APN and
property address (from ``documents.normalized_data``) are compared with
the listing file's own columns.  A document that carries a fact *and*
disagrees with the file's counterpart is quarantined -- excluded from
reconciliation matching so a wrong-property document can never satisfy a
requirement -- and surfaces as ``property_mismatch`` in the review queue.
Documents missing a fact, or files missing the column, simply do not
mismatch (no county/MLS lookups; that is a V0 boundary).
"""

from __future__ import annotations

from app.db.models import Document, ListingFile
from app.services.normalization import SHARED_FACT_FIELDS, document_normalized_fields

#: (document fact field, listing-file column) pairs that define identity.
RESOLVABLE_FIELDS: tuple[str, ...] = ("apn", "property_address")


def property_mismatch_fields(document: Document, listing_file: ListingFile) -> tuple[str, ...]:
    """Identity fields where the document and the file disagree.

    Empty when the document matches, lacks the fact, or the file lacks
    its column -- only an actual disagreement quarantines.
    """
    normalized = document_normalized_fields(document)
    mismatches: list[str] = []
    for field_name in RESOLVABLE_FIELDS:
        document_value = normalized.get(field_name)
        file_value = getattr(listing_file, field_name, None)
        if document_value is None or file_value is None:
            continue
        canonical_file = SHARED_FACT_FIELDS[field_name](file_value)
        if canonical_file is not None and document_value != canonical_file:
            mismatches.append(field_name)
    return tuple(mismatches)


def is_property_mismatch(document: Document, listing_file: ListingFile) -> bool:
    """True when the document's property identity disagrees with the file's."""
    return bool(property_mismatch_fields(document, listing_file))


#: The review queue's closed reason vocabulary for unmatched documents.
UNMATCHED_PROPERTY_MISMATCH = "property_mismatch"
UNMATCHED_NO_EVIDENCE = "no_evidence"


def unmatched_reason(document: Document, listing_file: ListingFile) -> str:
    """Why this document is unmatched: quarantined (Stage 2) or simply unbound."""
    if is_property_mismatch(document, listing_file):
        return UNMATCHED_PROPERTY_MISMATCH
    return UNMATCHED_NO_EVIDENCE
