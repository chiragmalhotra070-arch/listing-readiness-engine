# Listing demo documents

24 synthetic PDFs for demos and reference while building the listing engine.

- `templates/` — 12 blank field-structure templates, one per listing document type.
  Show what a complete document of each type looks like, field by field.
- `completed/` — 12 filled dummy documents with consistent fictional data
  (123 Main St, Pasadena CA 91101 · APN 5842-018-024 · seller Jane Seller ·
  agent Alex Agent · brokerage Demo Realty).

## v2: realistic multi-page fixtures

v2 (2026-10) replaced the v1 one-pagers with structurally realistic,
multi-page documents: paragraphed sections, checkbox matrices, terms grids,
budget and payment tables, signature blocks, and per-page initials. The
completed TDS and SPQ are visibly hand-completed (marked boxes, pen
explanations, one correction) while templates show the blank equivalents.

Page counts are part of the fixture contract (both `template-` and `demo-`
share them; asserted in `tests/test_demo_documents.py`):

| Document | Pages | Document | Pages |
|---|---|---|---|
| listing-agreement | 7 | ca-nhd (statement + report) | 4 |
| seller-advisory | 2 | wcmd-advisory (sign-only) | 1 |
| agency-disclosure | 2 | lead-disclosure | 3 |
| ca-tds | 3 | hoa-package (representative subset) | 10 |
| ca-spq | 4 | prelim-title-report | 9 |
| agent-visual-inspection | 3 | solar-agreement | 11 |

Real HOA packages run 50–300+ pages; ours is a representative subset. The
SPQ special-topics pages (8–11) are best-effort topic approximations, not
the official form (see the builder comment in the generator).

## Important

These are **synthetic**. They are not official California Association of
Realtors forms — CAR forms (RLA, TDS, SPQ, NHD, SA, AD, AVID, WCMD) are
copyrighted and available only to CAR members via zipForm, so they cannot be
downloaded or redistributed here. Field structure follows the engine's own
listing schemas (`app/schemas.py`: `ListingDocumentFields` + per-type deltas),
which were derived from the listing-document research
(`workspace/research_notes/listing-document-standards-20261006-1222/`).

Every page is labeled synthetic so a dummy is never mistaken for a real form.

## The completed set is machine-usable

Each completed dummy carries the `FICTIONAL TRAINING DATA` marker and is
registered in `app/adapters/llm.py` (`_DEMO_FIXTURE_PROFILES`, keyed by exact
filename). Ingesting one through the mock provider classifies it to its
listing type with the fixture's canned fields — no `mock_profile` needed.
This is the same pattern the chassis used for its financial sample library.

## Regenerating

```
pip install reportlab
python demo_documents/generate_demo_documents.py
```

Deterministic field content: re-running reproduces the same documents.
