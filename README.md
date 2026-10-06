# Listing Readiness Engine (V0)

AI workflow engine for US real estate **listing preparation** — California-first, residential single-family.
Thesis: *from scattered listing files to a defensible go/no-go.*

## Where this code came from

This project starts from the proven chassis of the **Financial Document Intake & Decision Engine**
(`Documents/ChatGPT/Financial Document Intake & Decision Engine`), copied 2026-10-06.
Roughly 70% of V0 already exists in that chassis: the stage-driven pipeline, run/attempt/audit
accounting, LLM classification with confidence, extraction provenance, entity resolution,
duplicate/version claiming, retry + work-queue infrastructure, and the ordered-rule decision engine.

**The 30% that is new — the product's actual IP — is the listing intelligence on top:**
the requirement engine (four-layer model), reconciliation across documents and sources,
value normalization, and the `ListingFile` aggregate.

## Contract for the coding agent

1. **Extend, don't fork.** Add stages to `ProcessingStage`, outcomes to `BusinessOutcome`,
   models beside the existing ones. Do not build a second pipeline.
2. **Keep the accounting.** Every execution is a `ProcessingRun`, every stage a
   `ProcessingAttempt`, every transition an `AuditEvent`. This is the PRD's audit trail.
3. **Decision discipline.** Ordered rules, each returning outcome + human-readable reason
   (see `services/decision_engine.py`). No boolean soup, no unexplained verdicts.
4. **Validate before assign.** Keep the JSONB/NUL hardening (`services/text_sanitization.py`)
   on every new JSONB write.
5. **Idempotency everywhere.** Reuse intake dedupe keys and `SideEffectOperation`
   idempotency keys so re-validation runs never double-count.
6. **Confidence-gated automation.** Low-confidence extraction routes to human confirmation,
   never auto-trust.

## What's in here

- `app/` — FastAPI service: `domain/` (enums), `services/` (pipeline, classification,
  validation, decision engine, resolution, dedup, audit, retry, work queue),
  `adapters/` (document parser, OCR with provider failover, LLM, CRM, customer lookup),
  `api/` (routes), `db/` (SQLAlchemy models), `worker.py`, `config.py`, `schemas.py`.
- `migrations/` — Alembic migrations (financial-engine baseline; add listing models here).
- `tests/` — existing test suite as patterns; adapt as the taxonomy changes.
- `docs/` — prior code-quality audit and intake contract (email-specific parts are stale context).
- `CODE_REUSE_REPORT.md` — the full stage-by-stage reuse map. **Read this first.**

## Deliberately left behind

`devtools/` (email e2e tooling), `scripts/` (old-project backfill/seed),
`ENVIRONMENT_*.md`, `DEMO_SEED_SPECIFICATION.md` — all specific to the financial engine.

## V0 pipeline target

upload → parse → classify → extract → **normalize** → **resolve** → **reconcile** →
**requirement generation + CA rules** → exceptions → human review → **readiness verdict**
(Ready / Conditionally ready / Not ready).

Build order: see `CODE_REUSE_REPORT.md` §5.
