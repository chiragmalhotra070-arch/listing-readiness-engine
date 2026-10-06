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
- **Last known HEAD:** `a9c6bd8` — verify with `git log`; uncommitted work (slices 2–4, demo docs, compose changes) has piled up since.
- **Stack:** Python 3.11, FastAPI, SQLAlchemy 2, PostgreSQL 16, Alembic. Tests: `pytest`, SQLite-backed except 23 Postgres-only skips.
- **Docker:** one compose package (`db` + `api` + `worker`) in the repo root. Listing ports: API **host 8011 → container 8010**; Postgres **host 5454 → container 5432**. The financial-engine stack (8010/5432) and `schema-audit-pg` (5433) are separate projects — **never touch them**.
- **Compose env:** `LLM_PROMPT_VERSION=v2` (must stay v2; v1 is the old financial prompt).
- **Alembic:** single head `0014_listing_readiness_domain`; 16 tables; `listing_engine` database.
- **Mac Docker note:** if the `docker` CLI hangs, the default socket is wedged — re-run with `DOCKER_HOST=$HOME/Library/Containers/com.docker.docker/Data/docker.raw.sock` (same daemon, no restart). Never restart Docker Desktop while production n8n containers are running.

## 4. Journey so far

- **Slice 0 — chassis cleanup:** financial-engine chassis copied in; stale tests removed; app/db/compose renamed to the listing engine. Suite: 399 passed.
- **Slice 1 — domain foundation (commit `a9c6bd8`):** listing enums; 5 new pipeline stages (`NORMALIZE`, `PROPERTY_RESOLUTION`, `RECONCILE`, `REQUIREMENT_CHECK`, `READINESS_VERDICT`); `ReadinessVerdict`; `RequirementType` (8 layers) / `RequirementStatus` (4) / `RequirementState`; `EvidenceSource`; models `ListingFile`, `RequirementRule` (versioned, unique on key+version), `Requirement` (unique on file+key, state CHECK), `Evidence`; `Document.listing_file_id` + nullable `email_id`; migration `0014`. Suite: 409 passed.
- **Slice 2 — extraction schemas:** taxonomy corrected to **12 listing doc types** (`LISTING_AGREEMENT`, `SELLER_ADVISORY`, `AGENCY_DISCLOSURE`, `CA_TDS`, `CA_SPQ`, `AGENT_VISUAL_INSPECTION`, `CA_NHD`, `WCMD_ADVISORY`, `LEAD_DISCLOSURE`, `HOA_PACKAGE`, `PRELIM_TITLE_REPORT`, `SOLAR_AGREEMENT`). `ListingDocumentFields` base (7 shared fields) + 12 subclasses (1–3 fields each). Suite: 440 passed.
- **Slice 3 — classifier taxonomy swap:** LLM system prompt rewritten listing-first (prompt **v2**); 12 mock listing profiles; `_LISTING_FIELD_HINTS` fallback (shared base fields alone → `UNKNOWN` → human review, never misclassification). Financial types kept as honest out-of-scope fallbacks. Suite: 457 passed.
- **Demo documents:** `demo_documents/generate_demo_documents.py` (reportlab) builds 12 blank templates + 12 completed fictional dummies (123 Main St, Pasadena CA 91101 · APN 5842-018-024 · Jane Seller · Alex Agent · Demo Realty). Completed dummies registered in `_DEMO_FIXTURE_PROFILES`. **You must generate the PDFs on this Mac** (`pip install reportlab && python demo_documents/generate_demo_documents.py`) — VM→Mac binary transfer isn't supported. Suite: 483 passed.
- **Docker bring-up:** verified end-to-end — API healthy on `127.0.0.1:8011`, DB on `5454`, worker stable, Alembic at head, `/health` 200. Known **tolerated** race: the worker crash-loops until migrations finish (`depends_on` without a healthcheck) — it self-resolves; do not "fix" compose unless asked.
- **Slice 4 — requirement engine (IN PROGRESS — your immediate task):** see §6.

## 5. Key files

| Area | Path |
|---|---|
| Enums | `app/domain/enums.py` — `DocumentType` (12), `ProcessingStage`, `ReadinessVerdict`, `RequirementType` (8), `RequirementStatus` (4), `RequirementState`, `EvidenceSource` |
| Models | `app/db/models.py` — `ListingFile` (`property_attributes` JSONB fact sheet drives requirement generation), `RequirementRule`, `Requirement`, `Evidence`, `Document` |
| Classify/extract | `app/services/classification.py`, `app/services/semantic_fields.py`, `app/schemas.py` |
| LLM | `app/adapters/llm.py` — prompt v2, mock profiles, `_DEMO_FIXTURE_PROFILES` |
| Requirement catalog | `app/services/requirement_catalog.py` — CA v1, 12 versioned rules |
| Requirement engine | `app/services/requirement_engine.py` — predicate evaluator + `generate()` |
| Migration | `migrations/versions/0014_listing_readiness_domain.py` |

## 6. Immediate task: verify Slice 4

Three new files are in the repo — a senior's draft, yours to verify:

- `app/services/requirement_catalog.py` — CA v1 catalog: 12 versioned `RequirementRule` dicts, `ensure_ca_catalog()` (idempotent seed), `validate_catalog()`.
- `app/services/requirement_engine.py` — JSON predicate evaluator (`evaluate_trigger`, `validate_trigger_shape`), `select_rules()` (jurisdiction + effective-window + per-key highest version + trigger), `RequirementEngine.generate()` (idempotent get-or-create; never modifies existing requirements).
- `tests/test_requirement_engine.py` — ~47 tests, SQLite-backed like `test_listing_domain.py`.

Logic already smoke-tested on a Linux mirror (12/9/0/0 rule selection as designed). The SQLite DB tests have not run anywhere.

**Steps:**

1. `python -m pytest tests/test_requirement_engine.py -q` — fix failures in implementation or tests, whichever is actually wrong. Spec intent is the tiebreaker: missing facts never fire rules; re-generation never clobbers existing requirement state; CA jurisdiction only (no TX catalog yet).
2. Full suite: `python -m pytest -q` — baseline pre-slice was **483 passed / 23 skipped**; expect ≈530 passed, 0 failures.
3. Report: new test count, full suite result, anything you fixed and why.
4. Commit on green per repo convention.

Do not change the 12-rule catalog scope or predicate semantics without flagging it — those are product decisions.

## 7. Plan ahead (proposed — senior specs each slice)

- **Slice 5:** evidence ↔ requirement matching (reconciliation) — match classified documents to requirements via `satisfied_by`, `PENDING → RECEIVED` transitions, confidence gating per the `Evidence` model.
- **Slice 6:** readiness verdict computation — aggregate requirement states into `READY` / `CONDITIONALLY_READY` / `NOT_READY` + `readiness_reason` on `ListingFile`.
- **Slice 7:** pipeline wiring — run requirement generation + verdict inside the worker's `REQUIREMENT_CHECK` / `READINESS_VERDICT` stages.
- **Later:** TX catalog, then FL/AZ; transaction-coordinator workflow on the same engine.
- **Explicitly parked (do NOT build):** seller-facing secure upload portal — discussed as a side note, not in the plan.
- **End-game hardening (not now):** DB healthcheck/migration gate, restart policies, log rotation, backups, monitoring.

## 8. Standing rules

- **Semantic honesty:** never imply integrations, legal universality, or backend behavior not implemented or verified.
- CA-first. The requirement list is generated, not hard-coded. Do not pretend all documents are equally or legally required.
- Official CAR forms are copyrighted — synthetic reconstructions only, visibly labeled as such.
- **Test gate:** full `pytest` suite green before commit.
