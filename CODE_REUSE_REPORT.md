# Code Reuse Report: Financial Document Intake Engine → Listing Readiness Engine (V0)
**Date:** 2026-10-06 · **Source:** `/Users/chiragmalhotra/Documents/ChatGPT/Financial Document Intake & Decision Engine` (read live from the Mac)
**Purpose:** Tell the coding agent exactly what to reuse, what to adapt, and what to build new for the listing workflow V0 (PRD §17).

## 1. What the existing codebase is

A Python/FastAPI document-intake and decision engine (SQLAlchemy + PostgreSQL, Alembic migrations, Docker Compose, background worker). Its pipeline processes one document at a time through twelve stages (`app/domain/enums.py → ProcessingStage`):

`EMAIL_INGESTION → ATTACHMENT_RETRIEVAL → DOCUMENT_PARSING → OCR → CLASSIFICATION → APPLICABILITY → CUSTOMER_RESOLUTION → DUPLICATE_DETECTION → FIELD_VALIDATION → BUSINESS_DECISION → CRM_ACTION → COMPLETE`

Each stage records a `ProcessingAttempt` (provider, status, quality score, failure code); each pipeline execution is a `ProcessingRun`; every state change emits an immutable `AuditEvent`. Business outcomes (`BusinessOutcome`): `READY_FOR_PROCESSING / PROCESSED / DUPLICATE / NEEDS_REVIEW / NOT_APPLICABLE / FAILED`.

## 2. Stage-by-stage reuse map (V0 pipeline → existing component)

V0 pipeline (PRD §17): upload → parse → classify → extract → normalize → resolve → reconcile → requirement generation + 10–15 CA rules → exceptions → readiness verdict.

| V0 stage | Existing component | Verdict | Notes |
|---|---|---|---|
| Upload / intake | `pipeline.process_email`, `services/storage.py` (content hash), idempotent intake on `source_email_id` | **Adapt** | Intake source differs (upload vs email) but the idempotent-intake pattern, `LocalDocumentStorage`, and SHA-256 content hashing transfer directly. |
| Parse | `adapters/document_parser.py` (format parse, `text_sufficient` flag → skip OCR) | **Reuse as-is** | Format-agnostic; the parse-then-OCR-fallback branch is exactly what listing PDFs/scans need. |
| Classify | `services/classification.py` (`LLMDocumentClassifier`) + `adapters/llm.py` | **Reuse architecture, swap taxonomy** | Keep: confidence + normalized confidence, ambiguities, warnings, per-type Pydantic schemas, `applicable` flag. Swap: `DocumentType` enum (INVOICE/REMITTANCE/… → LISTING_AGREEMENT, CA_TDS, CA_NHD, HOA_PACKAGE, …) and per-type field schemas (→ `TDSFields`, `NHDFields`, `ListingAgreementFields`, …). |
| Extract | `_record_extracted_fields` → `ExtractedField` rows (field_name/value/type/source/confidence, per attempt + run) | **Reuse as-is** | The per-field provenance model is precisely what §8.3 demands. |
| Normalize (new stage) | `services/semantic_fields.py` (`SemanticFieldResolver`, `FieldMapping`: source_field → canonical_field, UNAMBIGUOUS/AMBIGUOUS + evidence) | **Reuse + extend** | The canonical-*field* mapping is the seed. Extend to canonical *values*: address ("123 Main St." → "123 Main Street"), area ("2,410 SF" → 2410), dates. Reconciliation must compare canonicals, never raw strings. |
| Resolve (property/seller) | `services/customer_resolution.py` (`resolve_customer`: strong vs supporting evidence tiers; conflict → needs_review) | **Reuse pattern directly** | Rename the concept: seller/property identity resolution with strong (deed, government ID) vs supporting (questionnaire, email) evidence tiers. The conflict-escalation logic is identical. |
| Reconcile | — (no equivalent; closest is `duplicate_key`) | **New** | Cross-document + cross-source comparison on canonical values. New engine; feed it normalized fields. |
| Requirement generation + CA rules | `services/validation.py` (`REQUIRED_FIELDS` per type → `missing_fields`) + `services/decision_engine.py` (`decide()`: ordered rules → outcome + reason string) | **Reuse both patterns** | `validation.py` is the seed for requirement-evidence checking (extend: per-*requirement* required evidence, not just per-type fields). `decide()`'s ordered-rule → outcome + human-readable reason discipline becomes the readiness verdict. |
| Exceptions | `document_lifecycle.route_to_review` (stage + reason + details → NEEDS_REVIEW) | **Reuse as-is** | |
| Human review | `route_to_review` + `AuditEvent` | **Reuse + extend** | Add override-with-reason → re-validate affected stages (current review routing is terminal; listing needs the resolve/override loop from PRD §7). |
| Readiness verdict | `decision_engine.decide` | **Extend** | Ordered rules stay; outcomes become the 3-way verdict: Ready / Conditionally ready / Not ready (Completeness → Consistency → Compliance). |
| Audit trail | `services/audit.py` + `AuditEvent` (correlation IDs, JSONB-hardened) | **Reuse as-is** | This is the PRD's immutable audit requirement, already built. |
| Async / retries | `services/work_queue.py` (lease-based claim, `skip_locked`) + `services/retry.py` (exponential backoff + jitter, retryable vs permanent) + `app/worker.py` | **Reuse as-is** | V0 can run sync; keep the queue for batch scale. |
| Version resolution (duplicates across channels) | `services/duplicate_detection.py` (`duplicate_key`) + `services/business_identity.py` (`claim_business_identity`: OWNER/DUPLICATE/NOT_APPLICABLE, race-safe via IntegrityError) | **Reuse** | `duplicate_key` → version key per requirement; the claim pattern (first-writer-wins ownership) becomes current-version/superseded resolution. |

## 3. New builds required (gaps — nothing reusable exists)

1. **Requirement engine** — the four-layer model (CORE / JURISDICTIONAL / FEDERAL / PROPERTY-SELLER-TRANSACTION_CONDITIONAL) generating the request list from property attributes + the versioned CA catalog. Biggest new build; `validation.py` is only the seed pattern.
2. **Reconciliation engine** — cross-document and cross-source canonical comparison producing conflicts with sources, values, and next actions.
3. **Value normalization** — address/area/date canonicalization (extends `semantic_fields.py`).
4. **ListingFile aggregate** — the financial engine is document-centric (`Email` is the closest thing to a container). The listing workflow needs a file-level aggregate binding documents to a property + seller + readiness state + the open request list.
5. **Per-type Pydantic schemas** for listing documents (`TDSFields`, `NHDFields`, `ListingAgreementFields`, `HOAPackageFields`, …) mirroring `InvoiceFields` etc. in `app/schemas.py`.
6. **Override → re-validate loop** in the lifecycle (see §2, Human review row).

## 4. Patterns to preserve (the "professional manner" part)

- **Stage/enum-driven pipeline**: extend `ProcessingStage` (add `NORMALIZE`, `PROPERTY_RESOLUTION`, `RECONCILE`, `REQUIREMENT_CHECK`, `READINESS_VERDICT`) — do not fork a second pipeline.
- **Run/attempt/audit accounting**: every execution is a `ProcessingRun`, every stage a `ProcessingAttempt`, every transition an `AuditEvent`. This *is* the PRD's audit trail — keep it intact.
- **Decision discipline**: ordered rules, each returning outcome + human-readable reason (`decision_engine.decide` style). No boolean soup; no unexplained verdicts.
- **Validate-before-assign**: the NUL-byte/JSONB hardening in `text_sanitization.py` and pipeline ingress checks — keep for every new JSONB write.
- **Idempotency everywhere**: intake dedupe keys, `SideEffectOperation` idempotency keys — reuse for re-validation runs so reprocessing never double-counts.
- **Confidence-gated automation**: low-confidence extraction → human confirmation, never auto-trust (matches PRD §8.3).

## 5. Suggested build order for the agent

1. Domain: extend enums (`DocumentType` → listing types; `ProcessingStage` += new stages; `BusinessOutcome` += 3-way verdict), add `ListingFile`/`Requirement`/`RequirementRule` models + migration.
2. Per-type Pydantic schemas for the CA MVP document set.
3. Wire the existing pipeline stages (parse → classify → extract) to the new taxonomy — V0's first running slice.
4. `semantic_fields.py` extension: value normalization.
5. `customer_resolution.py` pattern → property/seller resolution.
6. Requirement engine (new) + `validation.py` pattern → requirement-evidence check.
7. Reconciliation engine (new).
8. `decision_engine.py` extension → 3-way readiness verdict (Completeness/Consistency/Compliance).
9. Override → re-validate loop in `document_lifecycle.py`.
10. API routes + worker wiring (mirror `app/api/routes.py`).

## 6. Honest assessment

Roughly **70% of V0 already exists** in this codebase: the pipeline skeleton, stage accounting, audit trail, classification-with-confidence architecture, extraction provenance, duplicate/version resolution, retry/queue infrastructure, and the decision-engine discipline. The ~30% that is genuinely new — the requirement engine, reconciliation, value normalization, and the ListingFile aggregate — is exactly the product's IP (per the PRD: "the requirement engine, not the PDF parser"). The agent should be instructed to treat the existing engine as the chassis and build the listing-specific intelligence on top of it, not beside it.
