#!/usr/bin/env python3
"""Generate the listing demo document set.

Creates 24 synthetic PDFs under demo_documents/:
- templates/ : blank field-structure templates (one per listing document type)
- completed/ : filled dummy documents with consistent fictional data

The completed documents carry the FICTIONAL TRAINING DATA marker so the mock
LLM's demo-fixture profiles recognize them during demos.  They are SYNTHETIC:
not official California Association of Realtors forms (those are copyrighted
and members-only).  Field structure follows the engine's listing schemas
(app/schemas.py: ListingDocumentFields + per-type deltas).

Deterministic field content: re-running reproduces the same documents.
Requires: pip install reportlab
"""

from __future__ import annotations

import os

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

MARKER = "FICTIONAL TRAINING DATA"
COMPLETED_FOOTER = (
    "SYNTHETIC DEMO DOCUMENT \u2014 fictional data for demonstration only. "
    "Not an official California Association of Realtors form."
)
TEMPLATE_FOOTER = (
    "SYNTHETIC TEMPLATE \u2014 field structure for demo purposes only. "
    "Not an official California Association of Realtors form."
)

# (label, completed value)
BASE_FIELDS: list[tuple[str, str | None]] = [
    ("Property address", "123 Main St, Pasadena, CA 91101"),
    ("APN (Assessor's Parcel Number)", "5842-018-024"),
    ("Seller name", "Jane Seller"),
    ("Document date", "2026-09-15"),
    ("Listing agent", "Alex Agent"),
    ("Brokerage", "Demo Realty"),
    ("Signatures present", "Yes"),
]

DOCUMENTS: list[dict] = [
    {
        "key": "listing-agreement",
        "title": "Residential Listing Agreement",
        "form": "Synthetic template (modeled on CAR RLA)",
        "deltas": [
            ("Listing price", "$1,250,000"),
            ("Listing start date", "2026-09-15"),
            ("Listing end date", "2027-03-15"),
        ],
    },
    {
        "key": "seller-advisory",
        "title": "Seller's Advisory",
        "form": "Synthetic template (modeled on CAR SA)",
        "deltas": [("Advisory acknowledged", "Yes")],
    },
    {
        "key": "agency-disclosure",
        "title": "Disclosure Regarding Real Estate Agency Relationships",
        "form": "Synthetic template (modeled on CAR AD)",
        "deltas": [
            ("Agency relationship", "Seller agency"),
            ("Disclosure signed", "Yes"),
        ],
    },
    {
        "key": "ca-tds",
        "title": "Transfer Disclosure Statement",
        "form": "Synthetic template (modeled on CAR TDS)",
        "base_overrides": {"Document date": None, "Listing agent": None, "Brokerage": None},
        "deltas": [
            ("Disclosure signed", "Yes"),
            ("Disclosure date", "2026-09-16"),
        ],
    },
    {
        "key": "ca-spq",
        "title": "Seller Property Questionnaire",
        "form": "Synthetic template (modeled on CAR SPQ)",
        "base_overrides": {"Document date": None, "Listing agent": None, "Brokerage": None},
        "deltas": [
            ("Questionnaire complete", "Yes"),
            ("Completion date", "2026-09-16"),
        ],
    },
    {
        "key": "agent-visual-inspection",
        "title": "Agent Visual Inspection Disclosure",
        "form": "Synthetic template (modeled on CAR AVID)",
        "base_overrides": {"Document date": "2026-09-17"},
        "deltas": [
            ("Inspection date", "2026-09-17"),
            ("Inspection complete", "Yes"),
        ],
    },
    {
        "key": "ca-nhd",
        "title": "Natural Hazard Disclosure Report",
        "form": "Synthetic template (modeled on NHD report)",
        "base_overrides": {"Document date": None},
        "deltas": [
            ("Report date", "2026-09-14"),
            ("Hazards disclosed", "Yes"),
        ],
    },
    {
        "key": "wcmd-advisory",
        "title": "Water-Conserving Plumbing Fixtures & CO Detector Advisory",
        "form": "Synthetic template (modeled on CAR WCMD)",
        "deltas": [("Fixtures compliant", "Yes")],
    },
    {
        "key": "lead-disclosure",
        "title": "Lead-Based Paint Disclosure",
        "form": "Synthetic template (federal, pre-1978 housing)",
        "base_overrides": {"Document date": None},
        "deltas": [
            ("Pamphlet acknowledged", "Yes"),
            ("Disclosure date", "2026-09-16"),
        ],
    },
    {
        "key": "hoa-package",
        "title": "HOA Resale Package",
        "form": "Synthetic template (CC&Rs, budget, minutes)",
        "base_overrides": {
            "Document date": None,
            "Listing agent": None,
            "Brokerage": None,
            "Signatures present": None,
        },
        "deltas": [
            ("HOA name", "Pasadena Oaks HOA"),
            ("Package date", "2026-09-20"),
        ],
    },
    {
        "key": "prelim-title-report",
        "title": "Preliminary Title Report",
        "form": "Synthetic template",
        "base_overrides": {
            "Document date": None,
            "Listing agent": None,
            "Brokerage": None,
            "Signatures present": None,
        },
        "deltas": [
            ("Title company", "Demo Title Co"),
            ("Report date", "2026-09-18"),
        ],
    },
    {
        "key": "solar-agreement",
        "title": "Solar Ownership / Financing Agreement",
        "form": "Synthetic template",
        "base_overrides": {"Document date": "2024-05-01", "Listing agent": None, "Brokerage": None},
        "deltas": [
            ("Ownership type", "Leased"),
            ("Provider name", "SunRun"),
        ],
    },
]


def _fields_for(doc: dict) -> list[tuple[str, str | None]]:
    overrides = doc.get("base_overrides", {})
    fields = [(label, overrides.get(label, value)) for label, value in BASE_FIELDS]
    fields.extend(doc["deltas"])
    return fields


def _draw_pdf(path: str, title: str, form: str, fields: list[tuple[str, str | None]], *, completed: bool) -> None:
    c = canvas.Canvas(path, pagesize=LETTER)
    width, height = LETTER
    y = height - 72

    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, y, title)
    y -= 22
    c.setFont("Helvetica-Oblique", 10)
    c.drawString(72, y, form)
    y -= 18
    c.setFont("Helvetica", 9)
    c.drawString(72, y, COMPLETED_FOOTER if completed else TEMPLATE_FOOTER)
    y -= 28

    c.setFont("Helvetica", 11)
    for label, value in fields:
        if completed:
            line = f"{label}: {value if value is not None else '--'}"
        else:
            line = f"{label}: ______________________________"
        c.drawString(72, y, line)
        y -= 20
        if y < 100:
            c.showPage()
            y = height - 72
            c.setFont("Helvetica", 11)

    if completed:
        y -= 12
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(72, y, MARKER)

    c.showPage()
    c.save()


def main() -> None:
    root = os.path.dirname(os.path.abspath(__file__))
    templates_dir = os.path.join(root, "templates")
    completed_dir = os.path.join(root, "completed")
    os.makedirs(templates_dir, exist_ok=True)
    os.makedirs(completed_dir, exist_ok=True)

    for doc in DOCUMENTS:
        fields = _fields_for(doc)
        _draw_pdf(
            os.path.join(templates_dir, f"template-{doc['key']}.pdf"),
            doc["title"],
            doc["form"],
            fields,
            completed=False,
        )
        _draw_pdf(
            os.path.join(completed_dir, f"demo-{doc['key']}.pdf"),
            doc["title"],
            doc["form"],
            fields,
            completed=True,
        )
        print(f"wrote template-{doc['key']}.pdf / demo-{doc['key']}.pdf")

    print(f"done: {len(DOCUMENTS)} templates + {len(DOCUMENTS)} completed dummies")


if __name__ == "__main__":
    main()
