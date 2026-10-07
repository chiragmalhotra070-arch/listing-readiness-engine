# Listing Readiness Engine — Agent Handover

You are the implementing engineer on this repo. A senior (Muse, off-machine) scopes the work and writes the specs; you write the code, run the tests, debug, and commit. Communication flows through the user: briefs arrive as paste-ready prompts, and you report back results, diffs, blockers, and questions the same way.

## 1. Objective

Portfolio project #1 for an AI automation engineering practice niched specifically to **US real estate** (deliberate narrow focus). The product is an AI listing coordinator for realtors/brokerages.

- **Thesis:** "From scattered listing files to a defensible go/no-go." The engine tells the realtor how ready — or how mismatched — a listing file is *before* anything gets uploaded to MLS/compliance.
- **Architecture:** two workflows, one engine. Workflow 1 (now): listing readiness. Workflow 2 (later): transaction coordination on the same engine.
- **Pipeline (10 stages):** Intake → Classify → Extract → Normalize → Resolve → Reconcile → Rule-check → Detect exceptions → Human review → Readiness verdict.
- **Readiness dimensions:** completeness, consistency, compliance. **File-level verdicts** (`ReadinessVerdict`): `READY` / `CONDITIONALLY_READY` / `NOT_READY` — stored on `ListingFile`, never on a document. The old document-level `BusinessOutcome` is untouched.
- **Core product rule:** the required-document list is *generated* from jurisdiction + property + seller + transaction + brokerage facts via a versioned rule catalog — never hard-coded. California-first, residential single-family MVP. Texas → Florida → Arizona after CA. No fifty-state checklist.

## 2. How we work

- **Senior (off-machine):** scopes slices, writes implementation briefs, does research, reviews diffs, answers relayed questions. Does not write files to this Mac.
- **You (on this Mac):** implement, run `pytest`, debug, iterate, commit. Your test run is the verification gate.
- **Product decisions are the senior's:** catalog scope, verdict taxonomy, predicate semantics, roadmap order. If a brief looks wrong, flag it and propose — don't unilaterally change it.
- **This handover doc is a deliberate exception** to the no-file-writes rule: it was explicitly requested as a co-working artifact. The code-writing boundary stands.

## 3. Repo & environment

- **Source of truth:** `/Users/chiragmalhotra/Documents/Listing Readiness Engine`
- **GitHub:** `https://github.com/chiragmalhotra070-arch/listing-readiness-engine` (private)
- **Last known HEAD:** `267e34c` — verify with `git log`.
- **Stack:** Python 3.11, FastAPI, SQLAlchemy 2, PostgreSQL 16, Alembic. Tests: `pytest`, SQLite-backed except 23 Postgres-only skips.
- **Docker:** one compose package (`db` + `listing-api` + `worker`) in the repo root — the api service is named `listing-api` so its network alias never collides with the financial stack's `api`. Listing ports: API **host 8011 → container 8010**; Postgres **host 5454 → container 5432**. The financial-engine stack (8010/5432) and `schema-audit-pg` (5433) are separate projects — **never touch them**.
- **Compose env:** `LLM_PROMPT_VERSION=v2` (must stay v2; v1 is the old financial prompt).
- **Alembic:** single head `0016_listing_idempotency_key`; 16 tables; `listing_engine` database.
- **Mac Docker note:** if the `docker` CLI hangs, the default socket is wedged — re-run with `DOCKER_HOST=$HOME/Library/Containers/com.docker.docker/Data/docker.raw.sock` (same daemon, no restart). Never restart Docker Desktop while production n8n containers are running.

## 4. Journey so far

- **Slice 0 — chassis cleanup:** financial-engine chassis copied in; stale tests removed; app/db/compose renamed to the listing engine. Suite: 399 passed.
- **Slice 1 — domain foundation (commit `a9c6bd8`):** listing enums; 5 new pipeline stages (`NORMALIZE`, `PROPERTY_RESOLUTION`, `RECONCILE`, `REQUIREMENT_CHECK`, `READINESS_VERDICT`); `ReadinessVerdict`; `RequirementType` (8 layers) / `RequirementStatus` (4) / `RequirementState`; `EvidenceSource`; models `ListingFile`, `RequirementRule` (versioned, unique on key+version), `Requirement` (unique on file+key, state CHECK), `Evidence`; `Document.listing_file_id` + nullable `email_id`; migration `0014`. Suite: 409 passed.
- **Slice 2 — extraction schemas:** taxonomy corrected to **12 listing doc types** (`LISTING_AGREEMENT`, `SELLER_ADVISORY`, `AGENCY_DISCLOSURE`, `CA_TDS`, `CA_SPQ`, `AGENT_VISUAL_INSPECTION`, `CA_NHD`, `WCMD_ADVISORY`, `LEAD_DISCLOSURE`, `HOA_PACKAGE`, `PRELIM_TITLE_REPORT`, `SOLAR_AGREEMENT`). `ListingDocumentFields` base (7 shared fields) + 12 subclasses (1–3 fields each). Suite: 440 passed.
- **Slice 3 — classifier taxonomy swap:** LLM system prompt rewritten listing-first (prompt **v2**); 12 mock listing profiles; `_LISTING_FIELD_HINTS` fallback (shared base fields alone → `UNKNOWN` → human review, never misclassification). Financial types kept as honest out-of-scope fallbacks. Suite: 457 passed.
- **Demo documents:** `demo_documents/generate_demo_documents.py` (reportlab) builds 12 blank templates + 12 completed fictional dummies (123 Main St, Pasadena CA 91101 · APN 5842-018-024 · Jane Seller · Alex Agent · Demo Realty). Completed dummies registered in `_DEMO_FIXTURE_PROFILES`. **You must generate the PDFs on this Mac** (`pip install reportlab && python demo_documents/generate_demo_documents.py`) — VM→Mac binary transfer isn't supported. Suite: 483 passed.
- **Docker bring-up:** verified end-to-end — API healthy on `127.0.0.1:8011`, DB on `5454`, worker stable, Alembic at head, `/health` 200. Known **tolerated** race: the worker crash-loops until migrations finish (`depends_on` without a healthcheck) — it self-resolves; do not "fix" compose unless asked.
- **Slice 4 — requirement engine (commit `1a8bd46`):** CA v1 catalog (12 versioned rules), predicate evaluator, idempotent `generate()`. Suite: ≈530 passed.
- **Slice 5 — reconciliation (commits `618a8de`, `da443fc`):** `satisfied_by` matching, `PENDING → RECEIVED`, confidence gating; `UNKNOWN` documents reported as unmatched.
- **Slice 6 — readiness verdict (commit `f160edb`):** aggregates requirement states into `READY` / `CONDITIONALLY_READY` / `NOT_READY` + reason on `ListingFile`.
- **Slice 7 — stage endpoints + n8n (commits `4805319`, `7eed078`):** the eight listing stage endpoints (create → generate-requirements → upload → classify → extract → reconcile → detect-exceptions → verdict) plus the `Listing Readiness v1` workflow (export at `n8n/workflows/listing-readiness-v1.json`, live id `54mDZXfkhnuJPhlY`).
- **Slice 8 — human review queue (commit `a6d9b6f`):** review-queue read + verify/flag action endpoints.
- **Slice 9 — consistency detection (commit `c5b5be4`):** cross-document consistency stage.
- **Close-out batch (commit `93df903`):** api DNS alias fixed; listing OCR routed through the failover; 4 OCR tests green; live OCR proof (image-only PDF → doc 51, `tesseract` quality 0.954, audit `OCR fallback completed`).
- **Retry & idempotency hardening (commit `267e34c`, audit-backed brief):** `idempotency_key` on `POST /v1/listing-files` (same key → 200 + existing file; partial unique index; migration `0016`); content-hash dedup inline in the listing upload (same bytes → 200 + existing document); `listing_llm_status` maps `needs_review → 422`, retryable (incl. chassis `classify_error` taxonomy) → `503`, else `500`; `POST …/processing-failed` records `{stage, reason, attempt}` into `Document.processing_failures` (JSONB) + `PROCESSING_FAILED` audit + review-queue `processing_failed` bucket; n8n classify/extract retry 3× (fixed 2000 ms waits — n8n has no exponential backoff) with `onError continueErrorOutput`, report nodes POST the failure and loop back so an exhausted document never stalls reconcile/verdict/respond. Suite: **626 passed / 23 skipped**. Live proofs: idempotent create 201→200, same-bytes upload 201→200, webhook fired twice → one file (12 docs, `CONDITIONALLY_READY`), failure E2E (`provider_timeout.pdf` first) → 503 ×3 → bucket entry → next document still classified.

## 5. Key files

| Area | Path |
|---|---|
| Enums | `app/domain/enums.py` — `DocumentType` (12), `ProcessingStage`, `ReadinessVerdict`, `RequirementType` (8), `RequirementStatus` (4), `RequirementState`, `EvidenceSource` |
| Models | `app/db/models.py` — `ListingFile` (`property_attributes` JSONB fact sheet drives requirement generation), `RequirementRule`, `Requirement`, `Evidence`, `Document` |
| Classify/extract | `app/services/classification.py`, `app/services/semantic_fields.py`, `app/schemas.py` |
| LLM | `app/adapters/llm.py` — prompt v2, mock profiles, `_DEMO_FIXTURE_PROFILES` |
| Requirement catalog | `app/services/requirement_catalog.py` — CA v1, 12 versioned rules |
| Requirement engine | `app/services/requirement_engine.py` — predicate evaluator + `generate()` |
| Migration | `migrations/versions/` — head `0016_listing_idempotency_key` (0014 domain, 0015 consistency, 0016 idempotency + `processing_failures`) |

## 6. Current state

- **Suite:** `626 passed / 23 skipped` (`.venv/bin/python -m pytest -q`). Green is the commit gate.
- **Alembic:** head `0016_listing_idempotency_key`, applied on the live DB.
- **Issue tracker:** GitHub Issues via `gh` (see `docs/agents/issue-tracker.md`) — currently empty; briefs arrive as paste-ready prompts, not issues.
- **n8n:** `Listing Readiness v1` published (workflow `54mDZXfkhnuJPhlY`), 19 nodes, export kept in sync in `n8n/workflows/listing-readiness-v1.json`. Known export artifacts: `active: false`, canvas positions differ from live.
- **Canvas note:** the per-document loop cluster cannot form a valid node group (loop-back edge + error branches violate single-entry/single-exit), so it is intentionally ungrouped — flagged `[pre-existing]` by the MCP validator.

## 7. Open threads & plan ahead

- **Dashboard:** parked — do not build unless the senior says so.
- **This hardening slice:** landed in `267e34c` (see §4); no follow-up work queued.
- **Later (senior specs each):** TX → FL → AZ catalogs; transaction-coordinator workflow on the same engine.
- **Explicitly parked (do NOT build):** seller-facing secure upload portal.
- **End-game hardening (not now):** DB healthcheck/migration gate, restart policies, log rotation, backups, monitoring.

## 8. Standing rules

- **Semantic honesty:** never imply integrations, legal universality, or backend behavior not implemented or verified.
- CA-first. The requirement list is generated, not hard-coded. Do not pretend all documents are equally or legally required.
- Official CAR forms are copyrighted — synthetic reconstructions only, visibly labeled as such.
- **Test gate:** full `pytest` suite green before commit.
