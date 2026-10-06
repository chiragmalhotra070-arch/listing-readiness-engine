# Gmail/n8n intake contract

This is the V1 boundary for Gmail ingestion. No Gmail or n8n business rules are
implemented in FastAPI beyond intake validation, persistence, and queueing.

## Request

n8n sends one `POST /v1/emails/intake` request per Gmail message, using
`multipart/form-data`:

- `metadata`: JSON matching `EmailIntakeRequest`.
- `files`: one part for each entry in `metadata.documents`, in the same order.
- `X-Intake-API-Key`: the configured shared intake credential.
- Optional `X-Correlation-ID` and `X-N8N-Execution-ID` headers.

`metadata.source_email_id` is the stable Gmail message/event identifier. n8n
must reuse the exact same value when Gmail retries delivery; timestamps are not
valid substitutes. `DocumentInput.source_attachment_id` identifies an
attachment within that message and should be stable across retries.

Supported V1 attachment suffixes are `.pdf`, `.xlsx`, and `.xlsm`. ZIP files are
not part of this contract.

## JSON transport adapter

When n8n cannot safely construct dynamic repeated multipart file fields, it may
send one JSON request to `POST /v1/ingest/n8n`. The request uses the same
`X-Intake-API-Key`, `X-Correlation-ID`, and `X-N8N-Execution-ID` headers as the
multipart endpoint. Each document contains `content_base64`; FastAPI decodes
and validates that content, stores it through the existing document storage,
constructs the existing `EmailIntakeRequest`, and invokes the same
`DocumentPipeline`.

The adapter is transport-only. It does not classify documents, run OCR or LLM
analysis, resolve customers, perform duplicate or ownership logic, or make
decisions. `source_email_id` remains the idempotency key.

## Acknowledgement

HTTP `201` means FastAPI has durably accepted the intake: the email, documents,
and one PostgreSQL `document_work_items` row per document have committed. In
async mode the response documents remain pending; no processing result is
implied by the acknowledgement.

A repeated delivery with the same `source_email_id` returns the original email
and documents with `idempotent: true`; it does not create new logical intake,
document, or work-item rows. The API key is still required for every delivery.

## Responsibilities

n8n/Gmail detects the message, reads metadata and attachment bytes, sends the
request, and retries orchestration when the acknowledgement is not received.
FastAPI owns storage, persistence, asynchronous processing, customer/ownership
resolution, duplicate detection, validation, decisions, audit, and worker
state.
