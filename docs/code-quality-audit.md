# Code-Quality & Simplification Audit — Financial Document Intake & Decision Engine

> **Status:** proposed work, not yet ticketed. **Date:** 2026-06-05.
> Produced by a read-only audit session (no files were modified during the audit).
> Process routing per Ask Matt: this document is the survey artifact
> (`/improve-codebase-architecture`'s output, gathered manually). Next steps are
> `/to-tickets` for the mechanical stream (sections B/C/T, commit series 1–9),
> `/grill-with-docs` for the P2 judgment stream (C1, C5–C7, M4, K3 — ADR first),
> closing with `/code-review` per ticket and a `/retro` on the session.

**Scope:** `app/` (3596 L), `tests/` (7168 L, 27 files), `scripts/`, `devtools/`.
Lifecycle boundary (`document_lifecycle.py`), retry policy, Run/Attempt/queue, CRM
decisioning, and API transport separations treated as invariants.

**Baseline:** `.venv/bin/python -m pytest -q` with
`ENGINE_PG_TEST_DATABASE_URL="postgresql+psycopg://engine:engine@localhost:5433/financial_pgtest"`
→ **473 passed, 0 skipped**. Every P0 claim below was re-verified by grep/read.

**Priorities:** P0 = clearly dead · P1 = obvious low-risk consolidation ·
P2 = design judgment · P3 = leave alone.
**Tally:** 11 P0 clusters · 13 P1 · 16 P2 · 13 P3 = **53 candidates**.

---

## Part 1 — Candidates, grouped by category

### Category 1 — Dead code

**D1. Delete `app/adapters/parser.py` (entire file)** `P0`
- What: 23-line module, no functions imported anywhere. · Evidence: zero importers (grep `adapters.parser`/`from app.adapters import parser` over app/tests/scripts/devtools → no hits). · Action: delete file + any `__init__` export. · Risk: none.

**D2. Delete `pipeline.py:490-498 _find_duplicate`** `P0`
- What: private helper with candidate-key loop. · Evidence: only hit repo-wide is its own `def`. · Action: delete (it also contains the would-be N+1 `db.get` at `:501`). · Risk: none (behavior never reachable).

**D3. Delete `classification.py:41-46, 72-97` alias machinery** `P0`
- What: `CANONICAL_FIELD_ALIASES` + `_normalize_canonical_aliases` static method. · Evidence: 3 total hits, all inside the dead machinery itself; duplicates live logic in `semantic_fields.py:20-32 ALIASES`. · Action: delete both; confirm `semantic_fields` remains the single alias source of truth. · Risk: none (no caller).

**D4. Delete 4 unused enum members — `enums.py:19,45,55,56`** `P0`
- What: `ProcessingStatus.COMPLETED_WITH_FAILURE`, `ProcessingStage.ARCHIVE_EXTRACTION/AUDIT_SYNC/NOTIFICATION`. · Evidence: 0 refs repo-wide for the three stage members; `COMPLETED_WITH_FAILURE` has 2 hits but both are raw **string literals** for `Email.status` (`pipeline.py:212`, `test_lifecycle_contract.py:327`) — not the member. No `sa.Enum` DB constraint exists anywhere (grepped models + migrations), so no migration needed. · Action: delete 4 lines. · Risk: **low, flag in commit**: `validate_schema.py:137` enumerates trigger values (not these) — verified unaffected.

**D5. Delete `config.py:12 app_env`** `P0`
- What: `app_env: str = "development"` field. · Evidence: sole hit repo-wide. · Action: delete. · Risk: none.

**D6. Delete unreachable `ocr.py:60-61`** `P0`
- What: `if profile == "ocr_both_timeout": raise …`. · Evidence: `:56` already raises for the same profile; the `:60` guard can never be true. · Action: delete branch. · Risk: none.

**D7. Delete 6 dead mock-profile aliases** `P0`
- What: alternate fixture names OR-ed into profile checks. · Evidence (quoted-literal usage counts in tests/scripts/devtools): `llm.py:176 "llm_timeout"`, `:177 "llm_rate_limit"`, `:179 "llm_unavailable"`, `:181 "schema_validation_failure"`, `crm.py:16 "retryable_crm_failure"`, `crm.py:18 "permanent_crm_failure"` → **0 uses each** (apparent hits were a test function name and a `llm_timeout_seconds` config key). · Action: drop the dead half of each `in {…}` set. · Risk: none.

**D8. Delete 4 unused adapter/DTO fields** `P1`
- What/Why: `LLMRequest.document_id` (`llm.py:19`, constructed, never read by adapter), `OCRRequest.context` (`ocr.py:20`, never passed by any constructor call), `RetryDecision.failure_type` (`retry.py:13`, constructed at `:28/:32`, zero reads — callers use only `.retryable/.exhausted/.next_attempt_at`), `ExtractionResult.provider_version` (`ocr.py:30`, read only to be written into evidence at `pipeline.py:384,397` as always-`None` from mocks). · Action: remove fields; for `provider_version` also drop the two evidence keys (or set it in mocks first if the evidence key is contractually wanted — decide per-field). · Risk: low; `provider_version` touches persisted `evidence` JSON — call it out in the commit.

**D9. Delete 4 unused parameters** `P1`
- `work_queue.py:13 enqueue_document(run_id=…)` — no caller passes it; `:51 complete_work_item(now=)`, `:57 fail_work_item(now=)` — zero call sites pass `now` (only `claim_next_work_item(now=)` is exercised); `retry.py:26 RetryPolicy.schedule(now=)` — both call sites (`pipeline.py:310,483`) omit it. · Action: remove params (keep `claim_next_work_item(now=)` — used). · Risk: low; previously no-touch files — flagged per audit rules; no schema/DB impact.

### Category 2 — Unreachable / duplicated branches

**B1. `worker.py:66-71` identical branches** `P1`
- What: `elif document_is_terminal(document): complete_work_item(item)` / `else: complete_work_item(item)`. · Evidence: read in full — bodies identical. · Action: collapse to unconditional `complete_work_item(item)` after the PENDING_RETRY branch. · Risk: none; add a comment that non-terminal non-pending states are drained on next lease (behavior unchanged).

**B2. `retry.py:40-42` duplicate branch** `P1`
- What: `if code in permanent_codes: return (PERMANENT, False)` followed by `return (PERMANENT, False)` fallback. · Action: delete the explicit branch (keep the set only if used for docs/validation — it isn't). · Risk: none; lives in retry.py (flagged).

**B3. Dead arm in `retry_allowed` — `pipeline.py:269`** `P1`
- What: set includes `StageStatus.RETRYABLE_FAILURE`. · Evidence: that value is written to **ProcessingAttempt.status / audit status** (`:320,:372,:373,:416,:417`) but **never** to `document.stage_status` (grep for assignments → 0). The enum member itself is alive — do **not** delete it; only the document-status arm is dead. · Action: remove from the set (or leave with a comment). · Risk: none today; slightly guards future writers.

**B4. Six dead mock-profile branches** — same as D7, filed here because they are dead `if` arms, not whole fields.

### Category 3 — Cross-layer duplication (routes ↔ pipeline)

**X1. Error ladder ×3 — `routes.py:290-295, 300-305, 310-313`** `P1`
- What: `except LookupError→404 / except ValueError→409` repeated per action route; `process_document` has **only** the `LookupError` half. · Evidence: `process_document_action` currently raises only `LookupError` (`pipeline.py:274`) → no live bug, but `retry_document` (`:238`) and `reprocess_document` (`:254,258,261`) prove the `ValueError`→409 contract; drift = future 500s. · Action: one helper `_document_action(fn, db)` (or decorator) mapping both exceptions; apply to all 3 routes. · Risk: low — response codes unchanged; add a test asserting 409 for ineligible retry.

**X2. Route calls private service method — `routes.py:271` → `pipeline._document_result`** `P2`
- What: GET endpoint reaches into a private method. · Action: promote to a public `pipeline.document_result(...)` (rename only). · Risk: none; improves the layer seam without moving logic.

**X3. Double document fetch — `routes.py:268,276,283` vs `pipeline.py:234,250,272`** `P2`
- What: route does `db.get`+404; service re-fetches and raises `LookupError`. · Evidence: 10 `db.get(Document…)` sites total (incl. `worker.py:39`, `audit.py:21`, `pipeline.py:501`). · Action: **partial** — let routes rely on the service's `LookupError` (fold into X1) and delete route-side fetches; keep the service guards (worker calls pipeline with no route). · Risk: medium-low — must keep 404 status and detail strings; covered by X1's tests.

**X4. `DocumentPipeline(get_settings())` rebuilt per request — `routes.py:249,271,278,285,291,301,311`** `P1`
- What: 7 handlers construct the pipeline (re-creating OCR adapter, parser, storage) per request; `worker.py:50` reuses one. · Evidence: read `routes.py:247-313`. · Action: `functools.lru_cache`d `get_pipeline()` or FastAPI dependency; settings stay `lru_cache`d as today. · Risk: low — pipeline is stateless per run (holds `retry_policy`, adapters); verify no per-request state before caching.

**X5. Validation→422 block ×5 — `routes.py:151,173,177,190,241`** `P1`
- What: `except ValidationError/ValueError → validation_detail(…) → HTTP 422`. · Action: extract `raise_validation(detail)` helper; keep per-site detail strings. · Risk: none (message text unchanged).

**X6. Auth dependency on all 8 routes — `routes.py:254…308`** `P1`
- What: `dependencies=[Depends(require_intake_api_key)]` copy-pasted 8×. · Action: move to `APIRouter(dependencies=[…])` at declaration. · Risk: low — keep the *same* dependency object; don't merge with any future key.

**X7. Transport write-back duplicated — `routes.py:159-167` vs `211-222`** `P2`
- What: hash/storage/size/attachment-id/mime reconstruction duplicated between multipart and n8n paths; n8n also calls `validate_file_content`+`store_content` separately though `validate_and_store_content` (`:129`) exists. · Action: share the write-back as one helper; adopt `validate_and_store_content` on n8n. · Risk: medium — validation **order** (metadata before file) is a security invariant (see D4).

### Category 4 — Intra-module duplication

**M1. Failure ternary quadruplets in `pipeline.py`** `P1`
- What: same condition evaluated up to 4× per catch block: attempt status, audit status, `failure_type`, event type — at `:316-320` (CRM), `:371-373` (OCR), `:416-417` (LLM). · Evidence: read in full; `:416`/`:417` alone repeat the identical nested ternary. · Action: compute `status = …`, `event = …`, `ft = …` once at the top of each `except`, then reuse (locals, not a new abstraction — the three sites differ in details dicts). · Risk: none (pure refactor, same values).

**M2. attempt + audit pair pattern ×~6** `P2`
- What: `_attempt(...)` followed by `record_audit(...)` with overlapping kwargs throughout the funnel. · Action: **don't** build a combined helper — details dicts diverge per stage; note in D. Optional: a tiny `_attempt_and_audit` only if it can take `details` opaque (measure later). · Risk of doing it: high abstraction for little gain.

**M3. `value_type_of` duplicated — `pipeline.py:37` vs `scripts/backfill_processing_runs.py:88`** `P2`
- What: identical helper in app and script. · Action: import from `app.services.pipeline` (or move to a shared util module) in the script. · Risk: low — script already imports app code.

**M4. Envelope schemas redeclare 7 fields — `schemas.py:94-100` vs `115-121`** `P2`
- What: `EmailIntakeRequest` / `N8nIngestRequest` share no base. · Action: common base **plus** n8n-specific extras — do not merge fully (client must not send `content_hash`/`storage_reference` on n8n). · Risk: medium — field-acceptance changes; API contract tests first.

**M5. `_document_result` hand-built map vs `model_validate` — `pipeline.py:522` vs `529,532`** `P3` — two mapping styles in one module, but the hand-built map injects computed fields (`customer_type`, `business_reference`, `retry_allowed`). Keep; only rename (X2).

**M6. `schemas.py:130,141,149,160,170` five `*Fields` models repeat `currency` + `extra="forbid"`** `P3` — Pydantic sugar; a base class saves 5 lines, hurts scannability. Leave.

### Category 5 — Error handling & API mapping

**E1. See X1 (ladder) and X5 (422 block).** Additional:

**E2. Two error-mapping conventions — `routes.py:247` vs `288-313`** `P3`
- What: intake wraps once via `run_intake`; action routes inline `except`. · Action: optional — fold actions into one wrapper once X1 lands. · Risk: low; do it as part of X1, not separately.

**E3. 413 detail wording ×3 with 2 phrasings — `routes.py:110,202`, `main.py:38`** `P3`
- What: `"attachment exceeds {N} bytes"` built in 3 places, slight wording drift. · Action: one constant. · Risk: none; test asserts on strings may need sync.

**E4. Literal repeat of validation detail — `routes.py:152,174`** `P3` — same `invalid=/malformed=` kwargs twice; fold into X5's helper.

### Category 6 — Complexity hotspots

**C1. `_run_from_current_stage` — `pipeline.py:327-472` (~146 L)** `P2`
- What: one linear method carrying 6 phases (parse → extract → classify → customer resolve → dedupe/validate → decide) with multiple early returns. · Action: split into private phase methods (`_parse_and_extract`, `_classify`, `_resolve_and_decide`) that take/return the shared locals — **no** new state object. · Risk: medium churn; do it **after** all P0/P1 lands (it conflicts with everything); each phase split its own commit with the full suite green.

**C2. Mega-mapping `pipeline.py:522` (`_document_result`)** `P2`
- What: ~40-kwarg single-line constructor (1000+ chars). · Action: assign locals above the return (readability only, same fields). · Risk: none — but do not convert to `model_validate` (D-section).

**C3. `_response` manual re-map — `pipeline.py:526`** `P2` — re-declares every `EmailIntakeResponse` field from `Email`. Action: map once from the ORM (`EmailIntakeResponse.model_validate(email)`-style *if* field names align — verify; `documents=` list still needs the loop). Risk: low, contract-test first.

**C4. `_attempt` mega-signature — `pipeline.py:500`** `P3` — 12 params, 6 defaulted-optional groups. Leave unless C1's split surfaces a natural grouping.

**C5. Sync-budget inline block — `pipeline.py:224-229`** `P2` / **D-section** — deliberately not a `fail_permanently` call: it preserves `decision_reason`, changes no `current_stage`, and writes **no** FINAL_FAILURE audit event (helper would add one). Consolidation = behavior change. Options: keep inline (recommended) or add `fail_permanently(..., audit=False, keep_decision_reason=True)` — only if a third caller appears.

**C6. CRM write blocks vs lifecycle helpers — `pipeline.py:286-288, 297-303, 312-320`** `P2` / **D-section**
- Real deltas vs `begin_stage/complete/schedule_retry/fail_permanently`: CRM success omits `next_attempt_at=None` and sets `decision=PROCESSED`; CRM failure keeps `decision_reason` untouched, keeps `document.retryable = retryable` (True even when exhausted — helper would set False), writes **no** audit event, and uses `stage_status = PERMANENT_FAILURE if not retryable else (FINAL_FAILURE if exhausted else RETRYABLE_FAILURE)` (`:318`) — including a `RETRYABLE_FAILURE` written to *attempt* status only. · Action: **do not merge mechanically.** Either (a) leave inline with a comment pointing at the deltas, or (b) extend the lifecycle module with CRM-specific variants *after* pinning current behavior with tests. Risk of merging as-is: silent retry-eligibility and audit regressions.

**C7. Parse-failure block — `pipeline.py:334-342`** `P2` — behavior-equivalent to `fail_permanently` **except** `stage_status=PERMANENT_FAILURE` vs helper's `FINAL_FAILURE` (today equivalent: `document_is_terminal` keys off `overall_status`, `retry_allowed` excludes both; `_run_outcome` reports FAILED either way). Consolidate only with an explicit `stage_status` parameter — or accept the vocabulary unification and update the one test that pins `PERMANENT_FAILURE`. Prefer: leave, document in GLOSSARY terms (PERMANENT vs FINAL are synonyms today — accidental complexity worth an ADR).

**C8. OCR→classification handoff — `pipeline.py:404-406`** `P2` — `current_stage=CLASSIFICATION, stage_status=SUCCESS, processing=IN_PROGRESS`: a "stage succeeded, enter next" transition the lifecycle module has no function for (`begin_stage` sets `IN_PROGRESS`, `advance_stage` clears `next_attempt_at`). If more handoffs exist, add `enter_stage()`; today it's one site → note only.

**C9. Local `import hashlib` inside method — `pipeline.py:259`** `P3` — move to module imports when touching the file.

### Category 7 — Performance (evidence-only; no benchmarks run)

**P1. N+1 in `_document_result` — `pipeline.py:518,521,526`** `P1`
- What: per document → latest-attempt scalar + `db.get(Customer, …)`; `:526` runs the whole thing per document in an intake list. · Evidence: read code; up to ~50 extra queries on a 25-doc intake (bounded, but pure overhead). · Action: batch-load attempts (one `IN` query) + customers (one `IN` query) for list responses; keep the single-doc path as-is. · Risk: low; verify attempt-ordering (`latest` = max id / started_at) matches current scalar subquery.

**P2. Per-request pipeline construction — see X4** `P1` — also re-creates OCR adapter/parser/storage per call.

**P3. `_stage_attempt_count` materializes all rows — `pipeline.py:514-515`** `P2` — `len(list(db.scalars(select(...))))` loads every attempt row for a count; `func.count` is used **nowhere** in `app/`. Action: `select(func.count()).where(...)`. Risk: none. (Callers: `:285`, `:482`.)

**P4. `attempts()`/`audit()` fetch all rows — `pipeline.py:528-532`** `P3` — unbounded but retention-shaped; leave unless tables grow (no evidence they do).

### Category 8 — Test-suite simplification

**T1. Vacuous assertion — `test_ocr_provider.py:111`** `P0`
- `assert "UploadFile" not in inspect.signature(...).return_annotation.__class__.__name__` — under lazy annotations `return_annotation` is the **string** `"...OCRResult"`, so `__class__.__name__ == "str"`; assert is always true. · Action: delete line, or assert `"UploadFile" not in get_type_hints(...)` properly.

**T2. Byte-identical asserts — `test_llm_classification.py:556` and `:558`** `P0` — same `"Do not duplicate canonical fields…"` string twice. Action: delete one.

**T3. Redundant parametrized tests — `test_text_sanitization.py`** `P1`
- Keep `test_none_is_preserved` (`:50`) and the oracle `test_only_nul_bytes_are_removed` (`:67`, ALL_TEXTS). Subsumed by it: `test_clean_text_is_returned_unchanged` (`:55`), `test_nul_bytes_are_removed` (`:60`), `test_result_is_identical_byte_for_byte…` (`:72`), `test_length_shrinks_by_exactly…` (`:78`); fold `test_nul_only_text_becomes_empty_string` (`:82`) by adding `"\x00\x00\x00"` to `NUL_TEXTS` first. · Why safe: `:67`'s oracle (`value.replace("\x00","")`) implies equality, str-ness, encoding-equality and length for every input. · Risk: low; if you want one explicit edge-case name, keep `:82` as-is and delete only 60/72/78.

**T4. 24 unused imports across 12 test files** `P0` — full list: `test_business_rules.py:4 Customer`; `test_e2e_inbox.py:15 WorkItemStatus`; `test_gmail_intake.py:8 select`; `test_jsonb_nul_boundary.py:35 Session`, `:54 validate_persisted_object`; `test_n8n_ingest.py:12 Settings`, `:15 WorkItemStatus`, `:17 DocumentWorker`; `test_ocr_provider.py:3 hashlib`, `:15 Document`; `test_persistence_boundary_sanitization.py:30 Path`, `:52 EmailIntakeResponse`, `:56 sanitize_persisted_text`; `test_real_files.py:5 zipfile`, `:14 get_settings`; `test_resilience.py:5 select`, `:6 Session`, `:9 AuditEvent/Document/ProcessingAttempt/SideEffectOperation`; `test_retry_accounting.py:4 Path`; `test_seed_demo_data.py:13 Customer`; `test_work_queue.py:5 select`. · Action: delete; one commit, suite green is the check.

**T5. Repeated per-file boilerplate** `P2` — `session_factory`/`factory` fixtures, `FailOnceProvider`/`AlwaysFailProvider` fakes, `payload()` builders redeclared across most of the 27 files while `conftest.py` (89 L) only isolates `client` storage (`:67-89`). · Action: move shared fakes/fixtures to `conftest.py` incrementally (start with `FailOnceProvider` — highest copy count). · Risk: low; do file-by-file so failures are attributable.

**T6. Coverage overlap: `test_work_queue.py` vs `tests/test_lifecycle_contract.py`** `P3` — both pin the terminal-document work-item rule (worker contract vs queue contract). Keep both, cross-reference in comments; only merge if a future change makes them drift.

**T7. Largest files** `P3` — `test_persistence_boundary_sanitization.py` (1076 L), `test_api_input_boundary.py` (825), `test_llm_classification.py` (801), `test_jsonb_nul_boundary.py` (700): split only when edited for other reasons.

### Category 9 — Config & unused knobs

**K1. `config.py:12 app_env`** `P0` — see D5.
**K2. Unused parameters** `P1` — see D9 (plus `claim_next_work_item(now=)` which *is* used — keep).
**K3. `get_settings()` ~10+ call sites, never `Depends`-injected — `routes.py:25,65,72,101,107,121,160,202,249`** `P2` — tests must clear the `lru_cache` to vary settings. Action: optional DI via `Depends(get_settings)`; benefit is test ergonomics, not runtime. Risk: medium churn across tests; low value → schedule last or skip.
**K4. `RUN_TRIGGERS` contains `"LEGACY"` — `pipeline.py:33`** `P3` / **D-section** — NOT dead: documented in `models.py:153` and enumerated by `validate_schema.py:137`. Leave.

### Category 10 — Schema / API surface consistency

**S1. Mapping-style split (hand-built vs `model_validate`)** `P2` — see M5/D3: keep the hand-built `_document_result`; consistency is not worth losing computed fields.
**S2. `DocumentResult.id` and `.document_id` always the same value — `schemas.py:224,238`, set at `pipeline.py:522`** `P3` — external contract may consume both; deprecate only with versioning. Leave.
**S3. Envelope base class — see M4** `P2`.
**S4. `from_attributes` declared in schema *and* passed at `model_validate` — `schemas.py:272,285` vs `pipeline.py:529,532`** `P3` — harmless redundancy; pick one when touching either.
**S5. Response contract declared 3× (`response_model=`, annotation, return) — `routes.py:266…309`** `P3` — FastAPI idiom; leave.
**S6. `attempts()`/`audit()` are pure read APIs behind routes that already 404'd — `pipeline.py:528-532`, `routes.py:274-285`** `P2` — acceptable layering (service owns queries); leave unless X3 is executed (then routes keep their 404, service keeps guards).
**S7. n8n path doesn't reuse `validate_and_store_content` — `routes.py:207,212` vs `129`** `P2` — see X7; fold into the transport helper work.

---

## Part 2 — Sections

### A. Highest-value opportunities

1. **Dead-code sweep (D1–D7, B1–B3)** — 11 P0 clusters, zero behavior risk, immediately shrinks the surface the next refactor can trip over; `_find_duplicate` and `parser.py` in particular are traps for readers.
2. **Test hygiene batch (T1–T4)** — removes a vacuous test (a test that can't fail is worse than no test), a duplicate assert, redundant parametrizations, and 24 dead imports; ~1 hour, pure confidence gain.
3. **Route micro-refactor (X1, X5, X6 + E4)** — one exception-mapping helper, one 422 helper, router-level auth dependency; fixes the latent `process`→500 drift; each is its own tiny commit.
4. **Performance pair (P1 + X4)** — batch the N+1 in `_document_result` and cache the per-request pipeline; the only findings with direct runtime evidence, both low-risk.
5. **Branch deadwood (B1, B2, B3, D8, D9)** — identical branches and never-passed parameters that make call sites lie about their contracts.
6. **Failure-handler ternaries (M1)** — computes each derived value once; pure locals refactor, three sites, unlocks cleaner C1 later.
7. **`_run_from_current_stage` phase split (C1)** — biggest readability win in the repo, but highest churn: do it *after* 1–6.
8. **Envelope/transport consolidation (M4, X7, S7)** — real API-layer duplication; needs contract tests first, so it sits last.

### B. Safe deletions (clearly dead — execute in order)

`parser.py` (D1) · `_find_duplicate` (D2) · `classification.py` alias machinery (D3) · 4 enum members (D4 — flag: no `sa.Enum` constraint, no migration) · `app_env` (D5) · `ocr.py:60-61` (D6) · 6 mock aliases (D7/B4) · `LLMRequest.document_id` + `OCRRequest.context` + `RetryDecision.failure_type` (D8; `provider_version` only after deciding the evidence key) · `enqueue_document(run_id=)`, `complete_work_item(now=)`, `fail_work_item(now=)`, `schedule(now=)` (D9) · `retry.py:40-42` (B2) · `retry_allowed`'s `RETRYABLE_FAILURE` arm (B3 — **member stays**) · worker's duplicate `else` (B1) · test imports + vacuous/duplicate asserts (T1, T2, T4).

### C. Duplication worth consolidating

Route exception ladder (X1) · 422 validation block (X5) · router-level auth dependency (X6) · per-request pipeline construction (X4) · failure ternary quadruplets (M1) · shared test fakes/fixtures → `conftest.py` (T5) · `value_type_of` (M3) · n8n validate+store reuse (X7/S7) · envelope field base *with* n8n kept stricter (M4) · `_stage_attempt_count` → `func.count` (P3) · list-response batch loads (P1) · `_document_result` locals (C2) · `_response` mapping (C3).

### D. Duplication that must NOT be consolidated

- **Sync-budget block (`pipeline.py:224-229`)** — vs `fail_permanently` it preserves `decision_reason`, doesn't touch `current_stage`, and writes no audit event. Merging changes observable history (see C5).
- **CRM write blocks (`:286-288, 297-303, 312-320`)** — 4 real deltas vs the lifecycle helpers: `decision_reason` untouched, `retryable` stays True on exhaustion, no audit event, `PROCESSED`-vs-`complete()` and `next_attempt_at` differences. Consolidation is a behavior project, not a cleanup (see C6).
- **`_document_result` hand-built mapping** — injects `customer_type`, `business_reference`, `retry_allowed`; plain `model_validate` would silently drop them (agent F4/M5).
- **Route `db.get`+404 *and* service `LookupError`** — `worker.py` calls the pipeline with no route; both guards are load-bearing. Only drop the *route-side fetch* if X1 makes the exception mapping single-sourced (see X3).
- **Multipart vs n8n parser flows** — validation order (metadata before file) is a security invariant; share helpers, never the flow.
- **`EmailIntakeRequest` vs `N8nIngestRequest` full merge** — n8n must not accept `content_hash`/`storage_reference` from the client.
- **Layered body-size checks (`main.py:38` vs `routes.py:65`)** — chunked-vs-header defence in depth; docstrings state intent.
- **attempt+audit pair pattern (M2)** — details dicts diverge per stage; a combiner abstraction costs more than it saves.
- **`PERMANENT_FAILURE` vs `FINAL_FAILURE` stage_status vocabulary (C7)** — equivalent today; unification is a semantic decision → ADR first, code after.
- **`LEGACY` trigger (K4)** — documented + schema-validated; not dead.
- **The three document status dimensions** — `processing_status` / `stage_status` / `overall_status` are load-bearing for `document_is_terminal` (work_queue), `_run_outcome`, and `retry_allowed`; never collapsed.
- **Dual mock-profile alias families** (`provider_*` vs domain-specific names) — keep both *families* where used; only the 6 zero-use strings (D7) die.

### E. Complexity hotspots (ranked)

1. `pipeline.py:327-472 _run_from_current_stage` — 146 L, 6 phases (C1).
2. `pipeline.py:522 _document_result` — 1000+ char constructor (C2).
3. Failure ternary clusters `:316-320`, `:371-373`, `:416-417` (M1).
4. CRM lifecycle blocks `:286-342` — four hand-rolled state machines beside the new module (C6/C7).
5. `pipeline.py:500 _attempt` — 12-param signature (C4).
6. `pipeline.py:526 _response` — manual envelope re-map (C3).
7. Route-layer copy patterns (X1/X5/X6) — breadth, not depth.

### F. Performance (evidence-only — no benchmarks run)

- **N+1:** latest-attempt + customer per document in `_document_result`/`_response` (`:518,521,526`) → up to ~50 extra queries per 25-doc intake (P1). Only finding with a clear query-count mechanism.
- **Per-request reconstruction:** 7 handlers build pipeline + OCR adapter + parser + storage per request (X4/P2).
- **`_stage_attempt_count`:** materializes all attempt rows to count them (`:514-515`); `func.count` appears nowhere in `app/` (P3).
- **Not a perf issue:** `attempts()`/`audit()` full fetches (retention-bounded) — recorded so nobody "optimizes" them blind.

### G. Test-suite simplification

Order of value: **(1)** delete the vacuous assert (T1) — a green check that can never fail. **(2)** T2 duplicate, **(3)** T3 redundancy (keep `:67` oracle; fold `:82`'s edge into `NUL_TEXTS` first), **(4)** T4's 24 unused imports (list in B), **(5)** T5 — lift `FailOnceProvider`/`session_factory`/`payload()` into `conftest.py` incrementally so the 12 files that redeclare them stop drifting, **(6)** T6 cross-reference the work-queue/lifecycle-contract overlap rather than merging. No suite splits or framework changes; baseline stays 473 (PG suite) after every commit. Note: suite is 2.3× the app's size — appropriate for a boundary-heavy system; the redundancy above is the only accidental part.

### H. Incremental commit sequence (each green at 473 passed)

1. **Dead app modules:** delete `parser.py`, `_find_duplicate`, `classification.py` alias machinery (D1–D3).
2. **Dead config/branches:** `app_env`, `ocr.py:60-61`, 6 mock aliases (D5, D6, D7).
3. **Dead params/fields:** D8 (`provider_version` split into its own commit if kept/dropped) + D9 + `retry.py:40-42` (flagged: previously no-touch files).
4. **Enum members:** D4 alone, commit message states "no sa.Enum DB constraint; no migration".
5. **Branch tidy:** worker identical branch, `retry_allowed` arm (B1, B3).
6. **Test hygiene:** T1 + T2 + T3 + T4 (imports last — trivially safe).
7. **Test fixtures → conftest:** T5, one shared fake per commit.
8. **Route layer:** X1 (+ E4, E2) → X5 → X6, one helper per commit, each with a status-code assertion test.
9. **Perf:** P1 batch loads → X4 pipeline cache → P3 `func.count`.
10. **Pipeline readability:** M1 ternary locals → C2/C3 local-variable rewrites → C9 import move.
11. **P2s, individually, only if still wanted:** C1 phase split (its own mini-series), M4 envelope base, X7/S7 transport helper, K3 settings DI.
12. **Explicitly deferred:** C5, C6, C7 (behavior-change projects → ADR first).

### I. What NOT to touch

`document_lifecycle.py`'s seven functions and the refactor's deliberate inline writes (sync-budget, CRM, `DocumentFormatError`) · retry policy semantics + `ProcessingRun`/`ProcessingAttempt` attribution safety net (`pipeline.py:504-508`) · `work_queue` lease/retry protocol · the three status dimensions · layered body-size checks · manual `DocumentResult` mapping · multipart/n8n validation order · per-route API-key dependency granularity · `LEGACY` trigger · both document-existence guards (route 404 + service `LookupError`) · `PROCESSING_STARTED` audit emission · Email roll-up (sync by design) · mock-profile alias *families* · `PERMANENT_FAILURE`/`FINAL_FAILURE` vocabulary (until an ADR decides) · `schemas.py`'s `id`+`document_id` pair (external contract).

---

## What to clean up next, and why

**Do these four, in order — they're where risk-adjusted value concentrates:**

1. **Commit series 1–5 (dead code + dead branches)** — every item is provably unreachable, needs no new tests, and removes the three worst reader traps (`parser.py`, `_find_duplicate`, the phantom lifecycle writes in CRM). ~1 hour, zero behavior risk.
2. **Commit series 6–7 (test hygiene)** — the vacuous assert at `test_ocr_provider.py:111` is actively lying to you; the 24 dead imports and `test_text_sanitization` redundancy are pure noise. Fixes the *oracle* before you trust any later refactor.
3. **Commit series 8–9 (route ladder + perf)** — the X1 helper closes a real contract gap (`process` route missing `ValueError`→409), and P1+X4 are the only findings with a concrete runtime mechanism (per-row queries, per-request adapter construction).
4. **Commit series 10 (pipeline readability)** — M1 then C2/C3 are safe mechanical wins that make C1 (the 146-line phase split) tractable afterward.

**Deliberately last / never:** C1 only after everything above; C5–C7 (CRM/sync-budget/vocabulary) are *behavior* decisions requiring an ADR and pinning tests, not cleanup — the deltas documented in D are exactly the kind of regression a "mechanical" consolidation would ship silently.

**Budget sanity:** items 1–3 are roughly a day of work total and clear out ~40% of the noise in the codebase; items 1–4 end with `pipeline.py` meaningfully shorter, every dead branch gone, and the route layer single-sourced — without touching a single one of the boundaries the lifecycle refactor just established.
