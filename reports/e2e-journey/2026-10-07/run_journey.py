#!/usr/bin/env python3
"""Run the e2e listing journey and render reports/e2e-journey/2026-10-07/index.html.

Usage:
  .venv/bin/python reports/e2e-journey/2026-10-07/run_journey.py
  .venv/bin/python reports/e2e-journey/2026-10-07/run_journey.py --render-only
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from html import escape as esc
from pathlib import Path
from typing import Any

import httpx
from pypdf import PdfReader

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

FIXTURES_DIR = REPO / "demo_documents" / "completed"
GROUND_TRUTH_PATH = HERE / "ground_truth.json"
CAPTURES_PATH = HERE / "captures.json"
FINDINGS_PATH = HERE / "findings.json"
HTML_PATH = HERE / "index.html"

API = "http://127.0.0.1:8011/v1"
WEBHOOK = "http://127.0.0.1:5678/webhook/listing-readiness/v1"
HEADERS = {"x-intake-api-key": "dev-intake-key"}
PASS_A_KEY = "e2e-journey-2026-10-07-passA"
PASS_B_KEY = "e2e-journey-2026-10-07-passB-attempt2"

LISTING: dict[str, Any] = {
    "property_address": "123 Main St, Pasadena, CA 91101",
    "apn": "5842-018-024",
    "seller_name": "Jane Seller",
    "property_attributes": {
        "state": "CA",
        "year_built": 1968,
        "property_type": "single_family",
        "hoa": True,
        "solar": True,
        "septic": False,
        "seller_type": "individual",
    },
}

FILES = [
    "demo-ca-tds.pdf",
    "demo-ca-spq.pdf",
    "demo-listing-agreement.pdf",
    "demo-agent-visual-inspection.pdf",
    "demo-ca-nhd.pdf",
    "demo-prelim-title-report.pdf",
]

EXPECTED_TYPES = {
    "demo-ca-tds.pdf": "CA_TDS",
    "demo-ca-spq.pdf": "CA_SPQ",
    "demo-listing-agreement.pdf": "LISTING_AGREEMENT",
    "demo-agent-visual-inspection.pdf": "AGENT_VISUAL_INSPECTION",
    "demo-ca-nhd.pdf": "CA_NHD",
    "demo-prelim-title-report.pdf": "PRELIM_TITLE_REPORT",
}

OWNER_DERIVED = {
    "seller_advisory": "seller",
    "agency_disclosure": "listing agent",
    "wcmd_advisory": "listing agent",
    "lead_disclosure": "seller + listing agent",
    "hoa_package": "third party (HOA / management company)",
    "solar_agreement": "third party (solar provider)",
}

SHARED_FIELDS = ["property_address", "apn", "seller_name", "listing_agent_name", "document_date"]

N8N_SQL = "SELECT id, status, startedAt, stoppedAt FROM execution_entity ORDER BY id DESC LIMIT 20"

_fixture_cache: dict[str, bytes] = {}


def fixture_bytes(name: str) -> bytes:
    if name not in _fixture_cache:
        _fixture_cache[name] = (FIXTURES_DIR / name).read_bytes()
    return _fixture_cache[name]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def probe_docker(container: str, cmd: list[str], timeout: int = 25) -> tuple[str | None, str | None, list[str]]:
    candidates: list[str] = []
    if os.environ.get("DOCKER_HOST"):
        candidates.append(os.environ["DOCKER_HOST"])
    candidates.append(f"unix://{Path.home()}/Library/Containers/com.docker.docker/Data/docker.raw.sock")
    candidates.append("unix:///var/run/docker.sock")
    log: list[str] = []
    for candidate in candidates:
        env = {**os.environ, "DOCKER_HOST": candidate}
        try:
            result = subprocess.run(
                ["docker", "exec", container, *cmd],
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except Exception as error:
            log.append(f"{candidate}: {type(error).__name__}: {error}")
            continue
        if result.returncode == 0:
            return result.stdout, candidate, log
        log.append(f"{candidate}: rc={result.returncode} {result.stderr.strip()[:300]}")
    return None, None, log


def probe_executions() -> tuple[list[dict] | None, list[str], str | None]:
    script = (
        'const {DatabaseSync}=require("node:sqlite");'
        'const db=new DatabaseSync("/home/node/.n8n/database.sqlite",{readOnly:true});'
        f'console.log(JSON.stringify(db.prepare({json.dumps(N8N_SQL)}).all()));'
    )
    stdout, host, log = probe_docker("n8n", ["node", "-e", script])
    if stdout is None:
        return None, log, None
    try:
        return json.loads(stdout), log, host
    except json.JSONDecodeError as error:
        return None, [*log, f"bad json: {error}"], host


def probe_api_config() -> tuple[dict | None, list[str], str | None]:
    script = (
        "import json\n"
        "from app.config import get_settings\n"
        "s = get_settings()\n"
        'print(json.dumps({"llm_provider": s.llm_provider, "llm_model": s.llm_model, '
        '"llm_prompt_version": s.llm_prompt_version}))'
    )
    stdout, host, log = probe_docker(
        "listingreadinessengine-listing-api-1",
        ["python", "-c", script],
        timeout=30,
    )
    if stdout is None:
        return None, log, None
    try:
        return json.loads(stdout.strip().splitlines()[-1]), log, host
    except (json.JSONDecodeError, IndexError) as error:
        return None, [*log, f"bad json: {error}"], host


class Runner:
    def __init__(self) -> None:
        self.client = httpx.Client(headers=HEADERS, timeout=120.0)
        self.errors: list[str] = []

    def call(
        self,
        label: str,
        method: str,
        url: str,
        *,
        into: list[dict],
        json_body: Any = None,
        files: dict | None = None,
        request: Any = None,
        timeout: float | None = None,
        critical: bool = False,
    ) -> dict:
        shown = request if request is not None else json_body
        started = time.perf_counter()
        status: int | None = None
        response: Any = None
        error_text: str | None = None
        try:
            resp = self.client.request(
                method,
                url,
                json=json_body,
                files=files,
                timeout=timeout if timeout is not None else 120.0,
            )
            status = resp.status_code
            try:
                response = resp.json()
            except ValueError:
                response = resp.text[:20000]
        except httpx.HTTPError as exc:
            error_text = f"{type(exc).__name__}: {exc}"
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        rec = {
            "label": label,
            "method": method,
            "url": url,
            "status": status,
            "ms": elapsed,
            "request": shown,
            "response": response,
        }
        if error_text:
            rec["error"] = error_text
            self.errors.append(f"{label}: {error_text}")
        elif status is None or status >= 400:
            self.errors.append(f"{label}: HTTP {status}")
        into.append(rec)
        if critical and (error_text or status is None or status >= 400):
            raise SystemExit(f"critical call failed: {label} -> {status} {error_text or ''} {str(response)[:400]}")
        return rec


def new_stage(n: int, title: str) -> dict:
    return {"n": n, "title": title, "calls": []}


def run_pass_a(runner: Runner) -> dict:
    calls: list[dict] = []
    out: dict[str, Any] = {"webhook_url": WEBHOOK, "idempotency_key": PASS_A_KEY, "calls": calls}
    before, before_log, before_host = probe_executions()
    out["exec_before"] = before
    out["probe_log_before"] = before_log
    documents = []
    full_documents = []
    for name in FILES:
        data = fixture_bytes(name)
        digest = sha256_hex(data)
        documents.append({"filename": name, "bytes": len(data), "sha256": digest})
        full_documents.append(
            {"filename": name, "data_base64": base64.b64encode(data).decode("ascii")}
        )
    payload = {**LISTING, "idempotency_key": PASS_A_KEY, "documents": full_documents}
    summary = {
        **LISTING,
        "idempotency_key": PASS_A_KEY,
        "documents": [
            {**documents[i], "base64_chars": len(full_documents[i]["data_base64"])}
            for i in range(len(FILES))
        ],
    }
    out["payload_summary"] = summary
    rec = runner.call(
        "POST webhook listing-readiness/v1 (full journey: create -> upload -> classify -> extract -> reconcile -> detect -> verdict -> readout)",
        "POST",
        WEBHOOK,
        json_body=payload,
        request=summary,
        timeout=300.0,
        into=calls,
        critical=True,
    )
    out["call"] = rec
    time.sleep(1.0)
    after, after_log, after_host = probe_executions()
    out["exec_after"] = after
    out["probe_log_after"] = after_log
    out["docker_host"] = before_host or after_host
    before_ids = {row["id"] for row in (before or [])}
    new_rows = [row for row in (after or []) if row["id"] not in before_ids]
    out["new_executions"] = new_rows
    response = rec.get("response")
    readout = response.get("readout") if isinstance(response, dict) else None
    out["readout"] = readout
    if isinstance(readout, dict) and readout.get("id"):
        queue = runner.call(
            "GET review-queue (state at webhook completion)",
            "GET",
            f"{API}/listing-files/{readout['id']}/review-queue",
            into=calls,
        )
        out["queue"] = queue.get("response")
    return out


def run_pass_b(runner: Runner, gt: dict) -> dict:
    stages: list[dict] = []
    out: dict[str, Any] = {"idempotency_key": PASS_B_KEY, "stages": stages}

    st = new_stage(1, "Intake — create file, generate requirements, upload documents")
    stages.append(st)
    create = runner.call(
        "POST /listing-files (create)",
        "POST",
        f"{API}/listing-files",
        json_body={**LISTING, "idempotency_key": PASS_B_KEY},
        into=st["calls"],
        critical=True,
    )
    lid = int(create["response"]["id"])
    out["listing_file_id"] = lid
    out["create_status"] = create["status"]
    gen = runner.call(
        "POST generate-requirements (catalog triggers -> requirement list)",
        "POST",
        f"{API}/listing-files/{lid}/generate-requirements",
        into=st["calls"],
    )
    readout1 = runner.call(
        "GET readout (post-generation snapshot)",
        "GET",
        f"{API}/listing-files/{lid}",
        into=st["calls"],
    )
    generated: list[dict] = []
    if isinstance(gen.get("response"), dict) and gen["response"].get("requirements"):
        generated = gen["response"]["requirements"]
        gen["used_as_fallback"] = False
    elif isinstance(readout1.get("response"), dict):
        generated = readout1["response"].get("requirements", [])
        gen["used_as_fallback"] = True
    out["generated"] = generated
    out["generated_keys"] = [g.get("requirement_key") for g in generated]
    out["readout_after_generate"] = readout1.get("response")

    doc_map: list[dict] = []
    for name in FILES:
        data = fixture_bytes(name)
        upload = runner.call(
            f"POST documents (upload {name})",
            "POST",
            f"{API}/listing-files/{lid}/documents",
            files={"file": (name, data, "application/pdf")},
            request={"filename": name, "bytes": len(data), "sha256": sha256_hex(data)},
            into=st["calls"],
            critical=True,
        )
        doc_id = int(upload["response"]["document_id"])
        detail = runner.call(
            f"GET /documents/{doc_id} (record: content_hash, evidence)",
            "GET",
            f"{API}/documents/{doc_id}",
            into=st["calls"],
        )
        dresp = detail.get("response") if isinstance(detail.get("response"), dict) else {}
        doc_map.append(
            {
                "filename": name,
                "document_id": doc_id,
                "upload_status": upload["status"],
                "sha256_local": sha256_hex(data),
                "content_hash_server": dresp.get("content_hash"),
                "file_size_bytes_server": dresp.get("file_size_bytes"),
                "evidence": dresp.get("evidence"),
            }
        )
    out["doc_map"] = doc_map
    first = FILES[0]
    data = fixture_bytes(first)
    dedup = runner.call(
        f"POST documents (dedup re-upload {first}, same bytes -> expect 200 + same id)",
        "POST",
        f"{API}/listing-files/{lid}/documents",
        files={"file": (first, data, "application/pdf")},
        request={"filename": first, "bytes": len(data), "sha256": sha256_hex(data), "expect": "200, same document_id"},
        into=st["calls"],
    )
    dedup_id = dedup.get("response", {}).get("document_id") if isinstance(dedup.get("response"), dict) else None
    out["dedup"] = {
        "status": dedup["status"],
        "document_id": dedup_id,
        "same_id": dedup_id == doc_map[0]["document_id"],
    }

    st = new_stage(2, "Classify — provider call per document")
    stages.append(st)
    for entry in doc_map:
        rec = runner.call(
            f"POST classify {entry['filename']}",
            "POST",
            f"{API}/documents/{entry['document_id']}/classify",
            into=st["calls"],
            critical=True,
        )
        r = rec.get("response") if isinstance(rec.get("response"), dict) else {}
        entry["classified_type"] = r.get("document_type")
        entry["classification_confidence"] = r.get("classification_confidence")
        entry["type_expected"] = EXPECTED_TYPES[entry["filename"]]
        entry["type_ok"] = entry["classified_type"] == entry["type_expected"]

    st = new_stage(3, "Extract — per-document field sets (extracted_data + normalized_data)")
    stages.append(st)
    extracts: dict[str, dict] = {}
    for entry in doc_map:
        rec = runner.call(
            f"POST extract {entry['filename']}",
            "POST",
            f"{API}/documents/{entry['document_id']}/extract",
            into=st["calls"],
            critical=True,
        )
        r = rec.get("response") if isinstance(rec.get("response"), dict) else {}
        extracts[entry["filename"]] = {
            "extracted_data": r.get("extracted_data", {}),
            "normalized_data": r.get("normalized_data", {}),
        }
    out["extracts"] = extracts

    st = new_stage(4, "Normalize — shared-fact view (derived from stage-3 responses)")
    stages.append(st)
    normalize_rows: list[dict] = []
    for name in FILES:
        extracted = extracts[name]["extracted_data"]
        normalized = extracts[name]["normalized_data"]
        expected = gt["docs"][name]["normalized"]
        for field in SHARED_FIELDS:
            in_expected = field in expected
            in_actual = field in normalized
            agrees = in_expected == in_actual and (not in_expected or expected[field] == normalized[field])
            normalize_rows.append(
                {
                    "document": name,
                    "field": field,
                    "extracted_raw": extracted.get(field, "(absent)"),
                    "normalized_api": normalized.get(field, "(absent)"),
                    "expected_normalized": expected.get(field, "(absent)"),
                    "agrees": agrees,
                }
            )
    out["normalize_rows"] = normalize_rows
    st["calls"].append(
        {
            "label": "derived: extracted_data -> normalized_data vs ground_truth.json (5 shared fact fields x 6 documents)",
            "method": "DERIVED",
            "url": "stage-3 responses x ground_truth.json",
            "status": None,
            "ms": 0.0,
            "request": {"fields": SHARED_FIELDS, "documents": FILES},
            "response": {
                "agreed": sum(1 for r in normalize_rows if r["agrees"]),
                "total": len(normalize_rows),
                "rows": normalize_rows,
            },
        }
    )

    st = new_stage(5, "Resolve — property identity and pre-reconcile review state")
    stages.append(st)
    ro5 = runner.call(
        "GET readout (pre-reconcile: unmatched_document_ids)",
        "GET",
        f"{API}/listing-files/{lid}",
        into=st["calls"],
    )
    q5 = runner.call(
        "GET review-queue (pre-reconcile: every document unmatched, reason no_evidence)",
        "GET",
        f"{API}/listing-files/{lid}/review-queue",
        into=st["calls"],
    )
    out["readout_pre_reconcile"] = ro5.get("response")
    out["queue_pre_reconcile"] = q5.get("response")
    identity_rows = []
    for name in FILES:
        normalized = extracts[name]["normalized_data"]
        identity_rows.append(
            {
                "document": name,
                "apn_matches_file": normalized.get("apn") == "5842018024",
                "address_matches_file": normalized.get("property_address") == "123 main st pasadena ca 91101",
                "seller_matches_file": normalized.get("seller_name") == "jane seller",
            }
        )
    queue5 = out["queue_pre_reconcile"] if isinstance(out["queue_pre_reconcile"], dict) else {}
    mismatch_docs = [
        u for u in queue5.get("unmatched_documents", []) if u.get("reason") == "property_mismatch"
    ]
    st["calls"].append(
        {
            "label": "derived: document-vs-file identity (normalized facts) + property_mismatch count from review-queue",
            "method": "DERIVED",
            "url": "stage-3 normalized_data x listing-file identity",
            "status": None,
            "ms": 0.0,
            "request": {"file": {"apn": "5842018024", "address": "123 main st pasadena ca 91101", "seller": "jane seller"}},
            "response": {
                "rows": identity_rows,
                "property_mismatch_documents": mismatch_docs,
                "note": "is_property_mismatch is evaluated during reconcile; a mismatched document would surface here with reason property_mismatch",
            },
        }
    )

    st = new_stage(6, "Reconcile — match documents to requirements")
    stages.append(st)
    rec = runner.call(
        "POST reconcile (evidence creation + RECEIVED transitions)",
        "POST",
        f"{API}/listing-files/{lid}/reconcile",
        into=st["calls"],
        critical=True,
    )
    out["reconcile"] = rec.get("response")
    ro6 = runner.call(
        "GET readout (post-reconcile requirement states)",
        "GET",
        f"{API}/listing-files/{lid}",
        into=st["calls"],
    )
    q6 = runner.call(
        "GET review-queue (post-reconcile: bindings via state_reason, unmatched empty)",
        "GET",
        f"{API}/listing-files/{lid}/review-queue",
        into=st["calls"],
    )
    out["readout_mid"] = ro6.get("response")
    out["queue_mid"] = q6.get("response")

    st = new_stage(7, "Rule check (derived) — catalog x readout join")
    stages.append(st)
    from app.services.requirement_catalog import CA_CATALOG_V1

    state_by_key: dict[str, dict] = {}
    if isinstance(out["readout_mid"], dict):
        state_by_key = {r["requirement_key"]: r for r in out["readout_mid"].get("requirements", [])}
    generated_keys = set(out["generated_keys"])
    rule_rows: list[dict] = []
    for rule in CA_CATALOG_V1:
        key = rule["requirement_key"]
        fired = key in generated_keys
        state_row = state_by_key.get(key, {})
        state = state_row.get("state", "(absent from readout)")
        status = rule["status"]
        if not fired:
            decision, rationale = "n/a", "rule trigger did not fire for this property"
        elif state == "VERIFIED":
            decision, rationale = "pass", "verified by human review"
        elif state == "RECEIVED":
            decision, rationale = "pass", "evidence document received, awaiting human verification"
        elif state == "EXCEPTION":
            decision, rationale = "fail", "requirement in EXCEPTION"
        elif state == "PENDING":
            if status in ("REQUIRED", "CONDITIONALLY_REQUIRED"):
                decision, rationale = "fail", "blocking requirement with no matching evidence document"
            else:
                decision, rationale = "warn", "non-blocking requirement pending"
        else:
            decision, rationale = "fail", f"unexpected state {state}"
        rule_rows.append(
            {
                "requirement_key": key,
                "requirement_type": rule["requirement_type"],
                "status": status,
                "trigger": rule["trigger"] or "always",
                "fired": fired,
                "state": state,
                "decision": decision,
                "rationale": rationale,
                "source": rule["source"],
            }
        )
    out["rule_rows"] = rule_rows
    counts = {"pass": 0, "fail": 0, "warn": 0, "n/a": 0}
    for row in rule_rows:
        counts[row["decision"]] += 1
    out["rule_counts"] = counts
    missing_rows = [
        {
            "requirement_key": row["requirement_key"],
            "requirement_type": row["requirement_type"],
            "status": row["status"],
            "source": row["source"],
            "owner_derived": OWNER_DERIVED.get(row["requirement_key"], "n/a"),
            "trigger": row["trigger"],
            "state": row["state"],
        }
        for row in rule_rows
        if row["state"] == "PENDING"
    ]
    out["missing_rows"] = missing_rows
    st["calls"].append(
        {
            "label": "derived: CA catalog v1 x generate-requirements x readout states (pass = RECEIVED/VERIFIED, fail = PENDING blocking / EXCEPTION, warn = PENDING non-blocking)",
            "method": "DERIVED",
            "url": "app/services/requirement_catalog.py x stage-1/6 responses",
            "status": None,
            "ms": 0.0,
            "request": {"catalog_version": "1", "jurisdiction": "CA"},
            "response": {"counts": counts, "rows": rule_rows},
        }
    )

    st = new_stage(8, "Detect exceptions — R1 conflicts, R2 signatures, R3 overdue")
    stages.append(st)
    det = runner.call(
        "POST detect-exceptions",
        "POST",
        f"{API}/listing-files/{lid}/detect-exceptions",
        into=st["calls"],
        critical=True,
    )
    out["detect_before"] = det.get("response")

    st = new_stage(9, "Human review — queue, verdict before verification, verify x6")
    stages.append(st)
    qb = runner.call(
        "GET review-queue (before verification)",
        "GET",
        f"{API}/listing-files/{lid}/review-queue",
        into=st["calls"],
    )
    out["queue_before"] = qb.get("response")
    vb = runner.call(
        "POST verdict (BEFORE verification)",
        "POST",
        f"{API}/listing-files/{lid}/verdict",
        into=st["calls"],
        critical=True,
    )
    out["verdict_before"] = vb.get("response")
    pending = (out["queue_before"] or {}).get("pending_verification", []) if isinstance(out["queue_before"], dict) else []
    verifies: list[dict] = []
    for item in pending:
        rec = runner.call(
            f"POST verify {item['requirement_key']} (requirement_id {item['requirement_id']})",
            "POST",
            f"{API}/requirements/{item['requirement_id']}/verify",
            json_body={
                "note": (
                    f"e2e journey {PASS_B_KEY}: verified {item['requirement_key']} "
                    "against matched evidence (simulated human review)"
                )
            },
            into=st["calls"],
            critical=True,
        )
        r = rec.get("response") if isinstance(rec.get("response"), dict) else {}
        verifies.append(
            {
                "requirement_id": item["requirement_id"],
                "requirement_key": item["requirement_key"],
                "state": r.get("state"),
                "verdict_mid": r.get("verdict"),
                "reason_mid": r.get("reason"),
                "status": rec["status"],
            }
        )
    out["verifies"] = verifies
    det2 = runner.call(
        "POST detect-exceptions (re-run after verification)",
        "POST",
        f"{API}/listing-files/{lid}/detect-exceptions",
        into=st["calls"],
    )
    out["detect_after"] = det2.get("response")

    st = new_stage(10, "Verdict — AFTER verification, final readout and queue")
    stages.append(st)
    va = runner.call(
        "POST verdict (AFTER verification)",
        "POST",
        f"{API}/listing-files/{lid}/verdict",
        into=st["calls"],
        critical=True,
    )
    out["verdict_after"] = va.get("response")
    ra = runner.call(
        "GET readout (final state)",
        "GET",
        f"{API}/listing-files/{lid}",
        into=st["calls"],
    )
    out["readout_after"] = ra.get("response")
    qa = runner.call(
        "GET review-queue (final: pending_verification empty)",
        "GET",
        f"{API}/listing-files/{lid}/review-queue",
        into=st["calls"],
    )
    out["queue_after"] = qa.get("response")
    return out


def page_text(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def normalize_text(raw: str) -> str:
    return "\n".join(line.strip() for line in raw.split("\n"))


def run_check(text: str, check: dict) -> tuple[bool, str]:
    kind = check["t"]
    if kind == "presence":
        ok = check["n"] in text
        return ok, "" if ok else f"needle not found: {check['n'][:90]!r}"
    if kind == "assoc":
        idx = text.find(check["a"])
        if idx < 0:
            return False, f"anchor not found: {check['a'][:80]!r}"
        window = text[idx : idx + check["w"]]
        ok = check["n"] in window
        return ok, "" if ok else f"needle not within {check['w']} chars of anchor: {check['n'][:70]!r}"
    if kind == "pre":
        idx = text.find(check["n"])
        if idx < 0:
            return False, f"needle not found: {check['n'][:80]!r}"
        prefix = check["p"]
        got = text[max(0, idx - len(prefix)) : idx]
        ok = got.endswith(prefix)
        return ok, "" if ok else f"expected prefix {prefix!r} immediately before needle; got {got[-40:]!r}"
    if kind == "forbid_win":
        idx = text.find(check["a"])
        if idx < 0:
            return False, f"anchor not found: {check['a']!r}"
        end = idx + check.get("w", 0)
        if check.get("end"):
            found = text.find(check["end"], idx)
            if found >= 0:
                end = found
        if end <= idx:
            return False, "forbid_win check has no window (missing w and end anchor)"
        window = text[idx:end]
        if check.get("strip"):
            window = window.replace(check["strip"], "", 1)
        import re

        match = re.search(check["rx"], window)
        ok = match is None
        return ok, "" if ok else f"forbidden pattern matched: {match.group(0)!r}"
    return False, f"unknown check type {kind!r}"


def field_match(expected: Any, actual: Any) -> tuple[bool, str]:
    if expected is None:
        if actual in (None, "", []):
            return True, "null-agrees"
        return False, "fabricated (expected null)"
    if isinstance(expected, bool):
        if actual is expected:
            return True, "boolean"
        return False, "boolean mismatch"
    if isinstance(expected, (int, float)):
        try:
            return abs(float(actual) - float(expected)) <= 0.01, "numeric"
        except (TypeError, ValueError):
            return False, "not numeric"
    if isinstance(expected, str):
        if actual is None:
            return False, "missing (expected value, got null)"
        actual_str = str(actual)
        if actual_str == expected:
            return True, "exact"
        if len(actual_str) >= 6 and (expected.startswith(actual_str) or actual_str.startswith(expected)):
            return True, "prefix"
        return False, "different value"
    return actual == expected, "exact"


def build_scorecard(gt: dict, pass_b: dict) -> dict:
    extracts = pass_b["extracts"]
    doc_map = pass_b["doc_map"]
    layer1_docs: dict[str, dict] = {}
    l1_totals = {"checks": 0, "passed": 0, "failed": 0, "tp": 0, "fn": 0, "fp": 0}
    for name in FILES:
        text = normalize_text(page_text(FIXTURES_DIR / name))
        rows = []
        tp = fn = fp = tn = 0
        for check in gt["docs"][name]["checks"]:
            ok, detail = run_check(text, check)
            rows.append({"id": check["id"], "t": check["t"], "ok": ok, "detail": detail})
            if check["t"] == "forbid_win":
                if ok:
                    tn += 1
                else:
                    fp += 1
            else:
                if ok:
                    tp += 1
                else:
                    fn += 1
        layer1_docs[name] = {
            "checks": len(rows),
            "passed": tp + tn,
            "failed": fn + fp,
            "tp": tp,
            "fn": fn,
            "fp": fp,
            "tn": tn,
            "rows": rows,
        }
        l1_totals["checks"] += len(rows)
        l1_totals["passed"] += tp + tn
        l1_totals["failed"] += fn + fp
        l1_totals["tp"] += tp
        l1_totals["fn"] += fn
        l1_totals["fp"] += fp

    layer2_docs: dict[str, dict] = {}
    l2_totals = {"expected": 0, "matched": 0, "missing": 0, "mismatch": 0, "fabricated": 0, "extra": 0}
    norm_totals = {"expected": 0, "matched": 0, "missing": 0, "mismatch": 0, "extra": 0}
    norm_rows_all: list[dict] = []
    for name in FILES:
        expected = gt["docs"][name]["layer2"]
        actual = extracts[name]["extracted_data"]
        rows = []
        matched = missing = mismatch = fabricated = 0
        for field, exp_value in expected.items():
            act_value = actual.get(field)
            ok, rule = field_match(exp_value, act_value)
            if ok:
                matched += 1
            elif exp_value is None:
                fabricated += 1
                rule = "fabricated"
            elif act_value is None or field not in actual:
                missing += 1
            else:
                mismatch += 1
            rows.append(
                {
                    "field": field,
                    "expected": exp_value,
                    "actual": act_value,
                    "rule": rule,
                    "ok": ok,
                }
            )
        extras = [
            {"field": k, "actual": v, "rule": "extra (not in ground truth)", "ok": False}
            for k, v in actual.items()
            if v not in (None, "") and k not in expected
        ]
        rows.extend(extras)
        layer2_docs[name] = {
            "expected": len(expected),
            "matched": matched,
            "missing": missing,
            "mismatch": mismatch,
            "fabricated": fabricated,
            "extra": len(extras),
            "rows": rows,
        }
        l2_totals["expected"] += len(expected)
        l2_totals["matched"] += matched
        l2_totals["missing"] += missing
        l2_totals["mismatch"] += mismatch
        l2_totals["fabricated"] += fabricated
        l2_totals["extra"] += len(extras)

        expected_norm = gt["docs"][name]["normalized"]
        actual_norm = extracts[name]["normalized_data"]
        norm_rows = []
        n_matched = n_missing = n_mismatch = n_extra = 0
        for field in SHARED_FIELDS:
            in_expected = field in expected_norm
            in_actual = field in actual_norm
            exp_v = expected_norm.get(field, "(absent)")
            act_v = actual_norm.get(field, "(absent)")
            if in_expected and in_actual and expected_norm[field] == actual_norm[field]:
                ok, rule = True, "exact"
                n_matched += 1
            elif in_expected and not in_actual:
                ok, rule = False, "missing"
                n_missing += 1
            elif not in_expected and in_actual:
                ok, rule = False, "extra (normalized carries a field ground truth does not)"
                n_extra += 1
            elif in_expected:
                ok, rule = False, "different value"
                n_mismatch += 1
            else:
                ok, rule = True, "absent-in-both"
                n_matched += 1
            norm_rows.append(
                {"field": field, "expected": exp_v, "actual": act_v, "rule": rule, "ok": ok}
            )
        norm_rows_all.append({"document": name, "rows": norm_rows})
        norm_totals["expected"] += sum(1 for f in SHARED_FIELDS if f in expected_norm)
        norm_totals["matched"] += n_matched
        norm_totals["missing"] += n_missing
        norm_totals["mismatch"] += n_mismatch
        norm_totals["extra"] += n_extra

    byte_rows = [
        {
            "filename": e["filename"],
            "sha256_local": e["sha256_local"],
            "content_hash_server": e["content_hash_server"],
            "equal": e["sha256_local"] == e["content_hash_server"],
            "bytes": len(fixture_bytes(e["filename"])),
            "pages": len(PdfReader(str(FIXTURES_DIR / e["filename"])).pages),
        }
        for e in doc_map
    ]
    return {
        "layer1": {"docs": layer1_docs, "totals": l1_totals},
        "layer2": {"docs": layer2_docs, "totals": l2_totals},
        "normalized": {"rows": norm_rows_all, "totals": norm_totals},
        "byte_identity": byte_rows,
        "classification": [
            {
                "filename": e["filename"],
                "expected": e["type_expected"],
                "actual": e["classified_type"],
                "confidence": e["classification_confidence"],
                "ok": e["type_ok"],
            }
            for e in doc_map
        ],
    }


def collect_meta() -> dict:
    commit = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    config, config_log, config_host = probe_api_config()
    fixtures = []
    for name in FILES:
        data = fixture_bytes(name)
        fixtures.append(
            {
                "filename": name,
                "bytes": len(data),
                "sha256": sha256_hex(data),
                "pages": len(PdfReader(str(FIXTURES_DIR / name)).pages),
            }
        )
    gt = json.loads(GROUND_TRUTH_PATH.read_text())
    return {
        "run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": commit,
        "dirty_tree": bool(dirty),
        "dirty_entries": dirty.splitlines()[:20],
        "config": config,
        "config_source": (
            f"docker exec {config_host} ... get_settings()" if config else "unavailable"
        ),
        "config_log": config_log,
        "fixtures": fixtures,
        "ground_truth_checks": sum(len(v["checks"]) for v in gt["docs"].values()),
        "idempotency_keys": {"pass_a": PASS_A_KEY, "pass_b": PASS_B_KEY},
        "endpoints": {"api": API, "webhook": WEBHOOK},
    }


def run_all() -> dict:
    gt = json.loads(GROUND_TRUTH_PATH.read_text())
    runner = Runner()
    cap: dict[str, Any] = {"meta": collect_meta()}
    cap["pass_a"] = run_pass_a(runner)
    cap["pass_b"] = run_pass_b(runner, gt)
    cap["scorecard"] = build_scorecard(gt, cap["pass_b"])
    cap["meta"]["runner_errors"] = runner.errors
    return cap


CSS = """
:root { --bg:#f6f7f9; --card:#ffffff; --ink:#1b1f24; --muted:#5b6570; --line:#d9dee3;
        --ok:#137a3d; --okbg:#e4f5ea; --bad:#b3261e; --badbg:#fbeae9; --warnbg:#fff6e0; --warn:#8a6100; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; }
main { max-width:1100px; margin:0 auto; padding:24px 20px 80px; }
header.top { max-width:1100px; margin:0 auto; padding:28px 20px 6px; }
h1 { font-size:24px; margin:0 0 4px; }
h2 { font-size:19px; margin:34px 0 10px; }
h3 { font-size:16px; margin:22px 0 8px; }
.sub { color:var(--muted); margin:2px 0; }
section.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
               padding:16px 18px; margin:14px 0; }
table { border-collapse:collapse; width:100%; margin:8px 0; font-size:13.5px; }
th, td { border:1px solid var(--line); padding:5px 8px; text-align:left; vertical-align:top; }
th { background:#eef1f4; font-weight:600; }
pre { background:#0f141a; color:#e6edf3; padding:10px 12px; border-radius:8px; overflow:auto;
      font:12.5px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; margin:6px 0; }
code { font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace; background:#eef1f4; padding:1px 4px; border-radius:4px; }
.b { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px; font-weight:600; }
.b-ok { background:var(--okbg); color:var(--ok); }
.b-bad { background:var(--badbg); color:var(--bad); }
.b-warn { background:var(--warnbg); color:var(--warn); }
.b-neutral { background:#eef1f4; color:var(--muted); }
.call { border-top:1px dashed var(--line); padding:8px 0; }
.chead { display:flex; flex-wrap:wrap; gap:8px; align-items:baseline; }
.m { font:600 11px ui-monospace,monospace; background:#e8edf3; border-radius:4px; padding:2px 6px; }
.m-derived { background:#efe6fb; color:#5b3fa8; }
.chead .lbl { color:var(--muted); font-size:13px; }
.chead .ms { color:var(--muted); font-size:12px; }
details { margin:4px 0; }
summary { cursor:pointer; color:#35506b; font-size:13px; }
.k { font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin-top:6px; }
.badge-row { display:flex; flex-wrap:wrap; gap:10px; margin:8px 0; }
.ok { color:var(--ok); font-weight:600; } .bad { color:var(--bad); font-weight:600; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
@media (max-width:800px) { .grid2 { grid-template-columns:1fr; } }
tr.badrow td { background:var(--badbg); }
tr.okrow td { background:#f2faf4; }
.note { background:var(--warnbg); border:1px solid #ead9a6; border-radius:8px; padding:10px 12px;
        font-size:13.5px; margin:10px 0; }
.foot { color:var(--muted); font-size:12.5px; margin-top:40px; border-top:1px solid var(--line); padding-top:12px; }
nav.toc { max-width:1100px; margin:0 auto; padding:0 20px; font-size:13.5px; }
nav.toc a { color:#35506b; margin-right:14px; text-decoration:none; }
nav.toc a:hover { text-decoration:underline; }
.kv-summary td:first-child { width:34%; font-weight:600; }
"""


def jpre(obj: Any) -> str:
    return f"<pre>{esc(json.dumps(obj, indent=2, ensure_ascii=False))}</pre>"


def status_badge(status: int | None) -> str:
    if status is None:
        return '<span class="b b-bad">ERR</span>'
    cls = "b-ok" if status < 400 else "b-bad"
    return f'<span class="b {cls}">{status}</span>'


def call_html(rec: dict) -> str:
    method = rec.get("method", "?")
    mcls = "m m-derived" if method == "DERIVED" else "m"
    parts = [
        '<div class="call"><div class="chead">',
        f'<span class="{mcls}">{esc(str(method))}</span>',
        f"<span>{esc(str(rec.get('url', '')))}</span>",
        status_badge(rec.get("status")),
        f'<span class="ms">{rec.get("ms", 0)} ms</span>',
        f'<span class="lbl">{esc(rec.get("label", ""))}</span>',
        "</div>",
    ]
    if rec.get("error"):
        parts.append(f'<div class="bad">error: {esc(rec["error"])}</div>')
    parts.append("<details><summary>request / response</summary>")
    parts.append(f'<div class="k">request</div>{jpre(rec.get("request"))}')
    parts.append(f'<div class="k">response</div>{jpre(rec.get("response"))}')
    parts.append("</details></div>")
    return "\n".join(parts)


def verdict_pretty(verdict: dict | None) -> str:
    if not isinstance(verdict, dict):
        return "(not captured)"
    keys = ["verdict", "reason", "blocking_pending", "blocking_exceptions", "pending_verification", "advisory"]
    return json.dumps({k: verdict.get(k) for k in keys}, indent=2, ensure_ascii=False)


def state_counts(readout: dict | None) -> dict:
    counts = {"RECEIVED": 0, "VERIFIED": 0, "PENDING": 0, "EXCEPTION": 0}
    for req in (readout or {}).get("requirements", []):
        state = req.get("state")
        if state in counts:
            counts[state] += 1
    counts["total"] = len((readout or {}).get("requirements", []))
    return counts


def summary_rows(cap: dict) -> list[tuple[str, str, str]]:
    pa = cap["pass_a"]
    pb = cap["pass_b"]
    sc = cap["scorecard"]
    rows: list[tuple[str, str, str]] = []
    webhook = pa.get("call", {})
    new_execs = pa.get("new_executions") or []
    exec_txt = (
        ", ".join(f"#{e['id']} {e['status']}" for e in new_execs) or "none detected"
    )
    rows.append(
        (
            "Pass A — n8n webhook journey",
            f"HTTP {webhook.get('status')} in {webhook.get('ms')} ms; n8n execution(s): {exec_txt}",
            "ok" if webhook.get("status") == 200 and any(e.get("status") == "success" for e in new_execs) else "bad",
        )
    )
    dedup = pb.get("dedup", {})
    byte_rows = sc["byte_identity"]
    hash_ok = all(r["equal"] for r in byte_rows)
    rows.append(
        (
            "Pass B — documents",
            f"listing file #{pb.get('listing_file_id')} (create HTTP {pb.get('create_status')}), "
            f"6 uploaded, dedup HTTP {dedup.get('status')} same_id={dedup.get('same_id')}, "
            f"sha256 == server content_hash {sum(r['equal'] for r in byte_rows)}/6",
            "ok" if hash_ok and dedup.get("same_id") else "bad",
        )
    )
    gen_keys = pb.get("generated_keys") or []
    rows.append(("Requirements generated", f"{len(gen_keys)} of 12 CA catalog rules fired", "ok" if len(gen_keys) == 12 else "warn"))
    recon = pb.get("reconcile") or {}
    rows.append(
        (
            "Reconcile",
            f"evidence_created={recon.get('evidence_created')}, "
            f"moved_to_received={len(recon.get('requirements_moved_to_received') or [])}, "
            f"unmet={len(recon.get('unmet_requirement_keys') or [])}, "
            f"unmatched={len(recon.get('unmatched_document_ids') or [])}",
            "ok" if recon.get("evidence_created") == 6 else "warn",
        )
    )
    vb = pb.get("verdict_before") or {}
    va = pb.get("verdict_after") or {}
    rows.append(("Verdict BEFORE verification", f"{vb.get('verdict')} — {vb.get('reason')}", "bad" if vb.get("verdict") == "NOT_READY" else "ok"))
    rows.append(("Verdict AFTER verification", f"{va.get('verdict')} — {va.get('reason')}", "bad" if va.get("verdict") == "NOT_READY" else "ok"))
    det = pb.get("detect_before") or {}
    rows.append(
        (
            "Detect exceptions",
            f"conflicts={len(det.get('conflicts') or [])}, missing_signatures={len(det.get('missing_signatures') or [])}, "
            f"overdue={len(det.get('overdue') or [])}, exceptions_raised={det.get('exceptions_raised')}",
            "ok" if not (det.get("conflicts") or det.get("missing_signatures") or det.get("overdue")) else "warn",
        )
    )
    l1 = sc["layer1"]["totals"]
    rows.append(
        (
            "Layer 1 — text fidelity vs PDF text layer",
            f"{l1['passed']}/{l1['checks']} checks passed (recall {l1['tp']}/{l1['tp'] + l1['fn']}, "
            f"failed negative assertions {l1['fp']})",
            "ok" if l1["failed"] == 0 else "bad",
        )
    )
    l2 = sc["layer2"]["totals"]
    denom = l2["matched"] + l2["missing"] + l2["mismatch"]
    rows.append(
        (
            "Layer 2 — extracted_data vs generator ground truth",
            f"{l2['matched']}/{l2['expected']} expected fields matched "
            f"(missing {l2['missing']}, mismatched {l2['mismatch']}, fabricated {l2['fabricated']}, extra {l2['extra']}); "
            f"recall {l2['matched']}/{denom}",
            "ok" if l2["missing"] == 0 and l2["mismatch"] == 0 and l2["fabricated"] == 0 else "bad",
        )
    )
    n = sc["normalized"]["totals"]
    rows.append(
        (
            "Normalize — normalized_data vs expected canonical forms",
            f"{n['matched']}/{n['matched'] + n['missing'] + n['mismatch']} shared-fact cells agree "
            f"(extra {n['extra']})",
            "ok" if n["missing"] == 0 and n["mismatch"] == 0 and n["extra"] == 0 else "bad",
        )
    )
    return rows


def render_summary(cap: dict) -> str:
    rows = []
    for label, text, cls in summary_rows(cap):
        rows.append(f"<tr><td>{esc(label)}</td><td class='{cls}'>{esc(text)}</td></tr>")
    return (
        '<section class="card" id="summary"><h2>Run summary</h2>'
        '<table class="kv-summary"><tr><th>Check</th><th>Observed outcome</th></tr>'
        + "".join(rows)
        + "</table></section>"
    )


def render_meta(cap: dict) -> str:
    meta = cap["meta"]
    config = meta.get("config") or {}
    fixtures = "".join(
        f"<tr><td>{esc(f['filename'])}</td><td>{f['bytes']} B</td><td>{f['pages']}</td>"
        f"<td><code>{f['sha256'][:16]}…</code></td></tr>"
        for f in meta["fixtures"]
    )
    dirty_note = ""
    if meta.get("dirty_tree"):
        dirty_note = (
            '<div class="note">Working tree had uncommitted changes at run time '
            f"(this report was being authored): <code>{esc(', '.join(meta.get('dirty_entries', [])[:5]))}</code></div>"
        )
    return f"""
<section class="card" id="meta">
<h2>Run metadata</h2>
<table class="kv-summary">
<tr><td>Run timestamp (UTC)</td><td>{esc(meta['run_at_utc'])}</td></tr>
<tr><td>Engine commit (HEAD at run time)</td><td><code>{esc(meta['commit'])}</code></td></tr>
<tr><td>LLM provider / model / prompt version</td><td>{esc(str(config.get('llm_provider')))} / {esc(str(config.get('llm_model')))} / {esc(str(config.get('llm_prompt_version')))}
 <span class="b b-neutral">source: {esc(meta['config_source'])}</span></td></tr>
<tr><td>API base</td><td><code>{esc(meta['endpoints']['api'])}</code></td></tr>
<tr><td>Webhook</td><td><code>{esc(meta['endpoints']['webhook'])}</code></td></tr>
<tr><td>Idempotency keys</td><td>Pass A <code>{esc(meta['idempotency_keys']['pass_a'])}</code>,
 Pass B <code>{esc(meta['idempotency_keys']['pass_b'])}</code></td></tr>
<tr><td>Ground-truth checks</td><td>{meta['ground_truth_checks']} Layer-1 assertions across 6 documents</td></tr>
</table>
{dirty_note}
<h3>Fixtures</h3>
<table><tr><th>File</th><th>Bytes</th><th>Pages</th><th>sha256 (local)</th></tr>{fixtures}</table>
</section>"""


def render_pass_a(cap: dict) -> str:
    pa = cap["pass_a"]
    execs = "".join(
        f"<tr><td>#{e['id']}</td><td>{esc(str(e['status']))}</td><td>{esc(str(e.get('startedAt')))}</td>"
        f"<td>{esc(str(e.get('stoppedAt')))}</td></tr>"
        for e in (pa.get("new_executions") or [])
    ) or "<tr><td colspan=4>no new executions detected</td></tr>"
    probe_log = pa.get("probe_log_before", []) + pa.get("probe_log_after", [])
    calls = "".join(call_html(rec) for rec in pa.get("calls", []))
    readout = pa.get("readout")
    verdict = (readout or {}).get("verdict") if isinstance(readout, dict) else None
    reason = (readout or {}).get("reason") if isinstance(readout, dict) else None
    return f"""
<section class="card" id="pass-a">
<h2>Pass A — n8n webhook journey (orchestrated end-to-end)</h2>
<p class="sub">One webhook call drives create &rarr; generate &rarr; upload &rarr; classify &rarr; extract
&rarr; reconcile &rarr; detect-exceptions &rarr; verdict &rarr; readout inside n8n (workflow
<code>54mDZXfkhnuJPhlY</code>), then returns the final readout as the response body.</p>
<table class="kv-summary">
<tr><td>Webhook response</td><td>{status_badge(pa['call']['status'])} in {pa['call']['ms']} ms</td></tr>
<tr><td>Readout verdict inside webhook response</td><td><span class="b b-bad">{esc(str(verdict))}</span> {esc(str(reason))}</td></tr>
</table>
<h3>n8n execution (sqlite probe, before/after diff)</h3>
<table><tr><th>Execution id</th><th>Status</th><th>Started</th><th>Stopped</th></tr>{execs}</table>
<details><summary>docker probe log</summary>{jpre(probe_log)}</details>
<h3>Captured calls</h3>
{calls}
<h3>Readout returned by the webhook</h3>
{jpre(readout)}
<h3>Review queue at webhook completion</h3>
{jpre(pa.get('queue'))}
</section>"""


def render_stage(stage: dict) -> str:
    calls = "".join(call_html(rec) for rec in stage["calls"])
    return (
        f'<section class="card" id="stage-{stage["n"]}">'
        f'<h3>Stage {stage["n"]} — {esc(stage["title"])}</h3>'
        f"{calls}</section>"
    )


def render_rule_check(cap: dict) -> str:
    pb = cap["pass_b"]
    rows = []
    for row in pb["rule_rows"]:
        cls = {"pass": "okrow", "fail": "badrow", "warn": ""}.get(row["decision"], "")
        badge_cls = {"pass": "b-ok", "fail": "b-bad", "warn": "b-warn", "n/a": "b-neutral"}[row["decision"]]
        trigger = row["trigger"] if isinstance(row["trigger"], str) else json.dumps(row["trigger"])
        rows.append(
            f'<tr class="{cls}"><td><code>{esc(row["requirement_key"])}</code></td>'
            f"<td>{esc(row['requirement_type'])}</td><td>{esc(row['status'])}</td>"
            f"<td>{esc(trigger)}</td><td>{'yes' if row['fired'] else 'no'}</td>"
            f"<td>{esc(row['state'])}</td>"
            f'<td><span class="b {badge_cls}">{esc(row["decision"])}</span></td>'
            f"<td>{esc(row['rationale'])}</td><td>{esc(row['source'])}</td></tr>"
        )
    counts = pb["rule_counts"]
    return f"""
<section class="card" id="rule-check">
<h2>Rule check table (DERIVED — no rule-check endpoint exists)</h2>
<p class="sub">DERIVED join of <code>app/services/requirement_catalog.py</code> (CA catalog v1) with the
stage-1 generate-requirements response and the stage-6 readout states. pass = RECEIVED or VERIFIED;
fail = PENDING on a blocking status, or EXCEPTION; warn = PENDING non-blocking.
Counts: pass {counts['pass']}, fail {counts['fail']}, warn {counts['warn']}, n/a {counts['n/a']}.</p>
<table>
<tr><th>Requirement key</th><th>Type</th><th>Status</th><th>Trigger</th><th>Fired</th><th>State</th>
<th>Decision</th><th>Rationale</th><th>Source (statute / form family)</th></tr>
{"".join(rows)}
</table>
</section>"""


def render_verdict(cap: dict) -> str:
    pb = cap["pass_b"]
    mid = state_counts(pb.get("readout_mid"))
    final = state_counts(pb.get("readout_after"))
    det = pb.get("detect_before") or {}
    det_after = pb.get("detect_after") or {}
    n = cap["scorecard"]["normalized"]["totals"]
    counts = pb["rule_counts"]
    queue_mid = pb.get("queue_mid") or {}
    bindings = [
        {"requirement_key": i["requirement_key"], "state_reason": i.get("state_reason")}
        for i in queue_mid.get("pending_verification", [])
    ]
    missing_rows = "".join(
        f"<tr><td><code>{esc(r['requirement_key'])}</code></td><td>{esc(r['requirement_type'])}</td>"
        f"<td>{esc(r['status'])}</td><td>{esc(r['trigger'] if isinstance(r['trigger'], str) else json.dumps(r['trigger']))}</td>"
        f"<td>{esc(r['source'])}</td><td>{esc(r['owner_derived'])}</td></tr>"
        for r in pb.get("missing_rows", [])
    )
    vb = pb.get("verdict_before")
    va = pb.get("verdict_after")
    detect_equal = json.dumps(det, sort_keys=True) == json.dumps(det_after, sort_keys=True)
    return f"""
<section class="card" id="verdict">
<h2>Verdict — before vs after human verification</h2>
<div class="grid2">
<div><div class="k">BEFORE verify (stage 9 POST /verdict)</div>{esc(verdict_pretty(vb))}</div>
<div><div class="k">AFTER verify x6 (stage 10 POST /verdict)</div>{esc(verdict_pretty(va))}</div>
</div>
<h3>Verify calls (stage 9)</h3>
<table><tr><th>requirement_id</th><th>key</th><th>state after</th><th>verdict mid-run</th></tr>
{"".join(f'<tr><td>{v["requirement_id"]}</td><td><code>{esc(v["requirement_key"])}</code></td><td>{esc(str(v["state"]))}</td><td>{esc(str(v["verdict_mid"]))}</td></tr>' for v in pb.get("verifies", []))}
</table>

<h3>Dimensions (DERIVED — <code>VerdictResponse</code> carries no dimension fields)</h3>
<table>
<tr><th>Dimension</th><th>Derived from</th><th>Value</th></tr>
<tr><td>Completeness — requirements by state (post-reconcile, stage 6)</td><td>GET readout (stage 6)</td>
<td>total {mid['total']}, RECEIVED {mid['RECEIVED']}, VERIFIED {mid['VERIFIED']}, PENDING {mid['PENDING']}, EXCEPTION {mid['EXCEPTION']};
unmatched documents {len((pb.get('readout_mid') or {}).get('unmatched_document_ids', []))}</td></tr>
<tr><td>Completeness — requirements by state (final, stage 10)</td><td>GET readout (stage 10)</td>
<td>total {final['total']}, RECEIVED {final['RECEIVED']}, VERIFIED {final['VERIFIED']}, PENDING {final['PENDING']}, EXCEPTION {final['EXCEPTION']}</td></tr>
<tr><td>Consistency — R1 conflicts</td><td>POST detect-exceptions (stage 8)</td>
<td>{len(det.get('conflicts', []))} conflicts {jpre(det.get('conflicts', []))}</td></tr>
<tr><td>Consistency — R2 missing signatures</td><td>POST detect-exceptions (stage 8)</td>
<td>{len(det.get('missing_signatures', []))} findings (engine checks <code>signatures_present is False</code>; None means unknown, not unsigned)</td></tr>
<tr><td>Consistency — R3 overdue</td><td>POST detect-exceptions (stage 8)</td>
<td>{len(det.get('overdue', []))} findings</td></tr>
<tr><td>Consistency — detect re-run after verify</td><td>stage 8 vs stage 9 response</td>
<td>{"identical" if detect_equal else "differs (see stage captures)"}; exceptions_raised {det.get('exceptions_raised')} &rarr; {det_after.get('exceptions_raised')}</td></tr>
<tr><td>Consistency — shared-fact normalization</td><td>stage-4 derived table</td>
<td>{n['matched']} of {n['matched'] + n['missing'] + n['mismatch']} cells agree</td></tr>
<tr><td>Compliance — rule check</td><td>derived rule table</td>
<td>pass {counts['pass']}, fail {counts['fail']}, warn {counts['warn']}, n/a {counts['n/a']}</td></tr>
<tr><td>Compliance — blocking lists (after)</td><td>POST verdict (stage 10)</td>
<td>blocking_pending {len(va.get('blocking_pending', []))} keys, blocking_exceptions {len(va.get('blocking_exceptions', []))},
pending_verification {len(va.get('pending_verification', []))}, advisory {len(va.get('advisory', []))}</td></tr>
</table>

<h3>Requirements still missing after the run (state PENDING)</h3>
<p class="sub">State columns are live API values; the <b>Owner</b> column is DERIVED by this report
(the engine's <code>Requirement.owner</code> field is never populated).</p>
<table><tr><th>Requirement</th><th>Type</th><th>Status</th><th>Trigger that fired</th><th>Source</th><th>Owner (DERIVED)</th></tr>
{missing_rows}</table>

<h3>Requirement &rarr; document bindings (from review-queue <code>state_reason</code>)</h3>
{jpre(bindings)}
</section>"""


def render_scorecard(cap: dict) -> str:
    sc = cap["scorecard"]
    l1 = sc["layer1"]
    l2 = sc["layer2"]
    norm = sc["normalized"]
    byte_rows = "".join(
        f'<tr class="{"okrow" if r["equal"] else "badrow"}"><td>{esc(r["filename"])}</td>'
        f"<td>{r['bytes']} B / {r['pages']} pp</td><td><code>{r['sha256_local'][:20]}…</code></td>"
        f"<td><code>{str(r['content_hash_server'])[:20]}…</code></td><td>{'yes' if r['equal'] else 'NO'}</td></tr>"
        for r in sc["byte_identity"]
    )
    cls_rows = "".join(
        f'<tr class="{"okrow" if c["ok"] else "badrow"}"><td>{esc(c["filename"])}</td><td>{esc(c["expected"])}</td>'
        f"<td>{esc(str(c['actual']))}</td><td>{c['confidence']}</td><td>{'yes' if c['ok'] else 'NO'}</td></tr>"
        for c in sc["classification"]
    )
    lt = l1["totals"]
    l1_recall = lt["tp"] + lt["fn"]
    l1_prec = lt["tp"] + lt["fp"]
    l2t = l2["totals"]
    l2_denom = l2t["matched"] + l2t["missing"] + l2t["mismatch"]
    l2_prec_denom = l2t["matched"] + l2t["mismatch"] + l2t["extra"] + l2t["fabricated"]

    l1_doc_sections = []
    for name in FILES:
        d = l1["docs"][name]
        failures = [r for r in d["rows"] if not r["ok"]]
        fail_html = ""
        if failures:
            fail_html = "<table><tr><th>Check</th><th>Type</th><th>Failure</th></tr>" + "".join(
                f'<tr class="badrow"><td><code>{esc(r["id"])}</code></td><td>{esc(r["t"])}</td><td>{esc(r["detail"])}</td></tr>'
                for r in failures
            ) + "</table>"
        body_rows = "".join(
            f'<tr class="{"okrow" if r["ok"] else "badrow"}"><td><code>{esc(r["id"])}</code></td>'
            f"<td>{esc(r['t'])}</td><td>{'pass' if r['ok'] else 'FAIL'}</td><td>{esc(r['detail'])}</td></tr>"
            for r in d["rows"]
        )
        l1_doc_sections.append(
            f"<details><summary>{esc(name)} — {d['passed']}/{d['checks']} passed "
            f"(tp {d['tp']}, fn {d['fn']}, fp {d['fp']})</summary>"
            f"{fail_html}<table><tr><th>Check id</th><th>Type</th><th>Result</th><th>Detail</th></tr>{body_rows}</table></details>"
        )

    l2_doc_sections = []
    for name in FILES:
        d = l2["docs"][name]
        body_rows = "".join(
            f'<tr class="{"okrow" if r["ok"] else "badrow"}"><td><code>{esc(r["field"])}</code></td>'
            f"<td>{esc(json.dumps(r['expected'], ensure_ascii=False))}</td>"
            f"<td>{esc(json.dumps(r['actual'], ensure_ascii=False))}</td>"
            f"<td>{esc(r['rule'])}</td></tr>"
            for r in d["rows"]
        )
        l2_doc_sections.append(
            f"<details><summary>{esc(name)} — matched {d['matched']}/{d['expected']} "
            f"(missing {d['missing']}, mismatch {d['mismatch']}, fabricated {d['fabricated']}, extra {d['extra']})</summary>"
            f"<table><tr><th>Field</th><th>Expected (generator)</th><th>Actual (extracted_data)</th><th>Match rule</th></tr>"
            f"{body_rows}</table></details>"
        )

    norm_sections = []
    for doc in norm["rows"]:
        body_rows = "".join(
            f'<tr class="{"okrow" if r["ok"] else "badrow"}"><td><code>{esc(r["field"])}</code></td>'
            f"<td>{esc(str(r['expected']))}</td><td>{esc(str(r['actual']))}</td><td>{esc(r['rule'])}</td></tr>"
            for r in doc["rows"]
        )
        norm_sections.append(
            f"<details><summary>{esc(doc['document'])}</summary>"
            f"<table><tr><th>Shared field</th><th>Expected normalized</th><th>API normalized_data</th><th>Rule</th></tr>"
            f"{body_rows}</table></details>"
        )

    return f"""
<section class="card" id="scorecard">
<h2>Measurement scorecard</h2>

<h3>Byte identity — local fixture file vs server-stored content</h3>
<table><tr><th>Document</th><th>Size / pages</th><th>sha256 (local, also the text-layer source)</th>
<th>content_hash (GET /documents/&#123;id&#125;)</th><th>Equal</th></tr>{byte_rows}</table>

<h3>Classification (stage 2)</h3>
<table><tr><th>Document</th><th>Expected type</th><th>Actual type</th><th>Confidence</th><th>OK</th></tr>{cls_rows}</table>

<h3>Layer 1 — text fidelity vs the PDF text layer</h3>
<p class="sub">Text source: pypdf extraction of the exact uploaded bytes (identity verified above),
line-edge normalized. Check types: <code>presence</code> (substring),
<code>assoc</code> (needle within N chars of an anchor),
<code>pre</code> (exact prefix line immediately before the needle),
<code>forbid_win</code> (regex must NOT match inside the window — a match is a failed negative assertion).
recall = tp / (tp + fn) = {lt['tp']} / {l1_recall}; precision = tp / (tp + fp) = {lt['tp']} / {l1_prec}.
Raw counts, no rounding. Grand total: {lt['passed']} / {lt['checks']} checks passed.</p>
<div class="note"><b>Measurement notes.</b> Checkbox glyphs in the TDS/SPQ and the NHD Part-1 Yes/No column are
drawn as vector graphics and do not appear in the text layer, so those checks assert the text that does exist
(item labels, comments, the B10 correction line, Part-2 matrix row association). RLA section 5 compensation
fields are blank (<code>____%</code>) by fixture design; the check asserts no percentage is present there.
The engine has no severity concept and no per-document resolve endpoint — see the derived panels and Findings.</div>
{"".join(l1_doc_sections)}

<h3>Layer 2 — extracted_data vs generator ground truth</h3>
<p class="sub">Match rules: exact string; numeric within 0.01; boolean equality; null-agreement
(expected null requires actual null — a value where truth says null counts as <i>fabricated</i>);
prefix (either side starts with the other, minimum 6 chars — covers shortened vendor names such as
<code>Demo Title Co</code> vs the printed <code>Demo Title Co (fictional)</code>).
recall = matched / (matched + missing + mismatch) = {l2t['matched']} / {l2_denom};
precision = matched / (matched + mismatch + extra + fabricated) = {l2t['matched']} / {l2_prec_denom}.</p>
{"".join(l2_doc_sections)}

<h3>Normalize — normalized_data (stage 3 API response)</h3>
<p class="sub">The engine's normalization maps only the five shared fact fields
(<code>property_address</code>, <code>apn</code>, <code>seller_name</code>, <code>listing_agent_name</code>,
<code>document_date</code>); other extracted fields never appear in <code>normalized_data</code>.
Totals: {norm['totals']['matched']} agree, {norm['totals']['missing']} missing, {norm['totals']['mismatch']} differ, {norm['totals']['extra']} extra.</p>
{"".join(norm_sections)}
</section>"""


def render_findings(findings: dict | None) -> str:
    if not findings:
        return (
            '<section class="card" id="findings"><h2>Findings</h2>'
            "<p class=\"sub\">findings.json not yet authored — run the journey first, then author findings "
            "from the captures and re-render with --render-only.</p></section>"
        )
    items = findings.get("findings", [])
    rows = "".join(
        f'<tr><td><code>{esc(f.get("id", ""))}</code></td><td>{esc(f.get("severity", "n/a (engine has no severity concept)"))}</td>'
        f"<td>{esc(f.get('title', ''))}</td><td>{esc(f.get('evidence', ''))}</td><td>{esc(f.get('impact', ''))}</td></tr>"
        for f in items
    ) or "<tr><td colspan=5>none</td></tr>"
    watch = "".join(
        f'<tr class="{"okrow" if w.get("confirmed") else "badrow"}"><td><code>{esc(w.get("id", ""))}</code></td>'
        f"<td>{esc(w.get('hypothesis', ''))}</td><td>{esc(str(w.get('actual', '')))}</td>"
        f"<td>{'confirmed' if w.get('confirmed') else 'NOT confirmed'}</td></tr>"
        for w in findings.get("watchlist", [])
    )
    notes = "".join(f"<li>{esc(n)}</li>" for n in findings.get("notes", []))
    return f"""
<section class="card" id="findings">
<h2>Findings</h2>
<table><tr><th>Id</th><th>Severity</th><th>Title</th><th>Evidence (captured response)</th><th>Impact</th></tr>{rows}</table>
<h3>Watchlist vs observed</h3>
<table><tr><th>Id</th><th>Hypothesis</th><th>Observed</th><th>Verdict</th></tr>{watch}</table>
{f"<h3>Notes</h3><ul>{notes}</ul>" if notes else ""}
</section>"""


def render(cap: dict, findings: dict | None) -> str:
    meta = cap["meta"]
    stages = "".join(render_stage(s) for s in cap["pass_b"]["stages"])
    cfg = meta.get("config") or {}
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Listing readiness — e2e journey measurement 2026-10-07</title>
<style>{CSS}</style></head>
<body>
<header class="top">
<h1>Listing readiness — end-to-end journey measurement</h1>
<p class="sub">Run 2026-10-07 · engine <code>{esc(meta['commit'][:10])}</code> ·
provider {esc(str(cfg.get('llm_provider')))} · prompt {esc(str(cfg.get('llm_prompt_version')))} ·
6 fixture documents · Pass A (n8n webhook) + Pass B (stage APIs)</p>
</header>
<nav class="toc"><a href="#summary">Summary</a><a href="#meta">Metadata</a><a href="#pass-a">Pass A</a>
<a href="#stage-1">Stages</a><a href="#rule-check">Rule check</a><a href="#verdict">Verdict</a>
<a href="#scorecard">Scorecard</a><a href="#findings">Findings</a></nav>
<main>
{render_summary(cap)}
{render_meta(cap)}
{render_pass_a(cap)}
<h2 id="stages">Pass B — measured stage-API run</h2>
<p class="sub">Each stage records the request body, response body and wall-clock duration of every API call.</p>
{stages}
{render_rule_check(cap)}
{render_verdict(cap)}
{render_scorecard(cap)}
{render_findings(findings)}
<footer class="foot">Every request/response body on this page is read from
<code>captures.json</code>, captured live from the local stack during this run. Panels labeled DERIVED are
computed by <code>run_journey.py</code> from those captures plus the repository's catalog source; they are not
engine API outputs. Layer-1 truth comes from <code>ground_truth.json</code>, authored from the fixture PDFs'
text layer before the run.</footer>
</main></body></html>"""


def load_findings() -> dict | None:
    if FINDINGS_PATH.exists():
        return json.loads(FINDINGS_PATH.read_text())
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-only", action="store_true", help="re-render index.html from captures.json + findings.json")
    args = parser.parse_args()
    if args.render_only:
        if not CAPTURES_PATH.exists():
            raise SystemExit(f"missing {CAPTURES_PATH}")
        cap = json.loads(CAPTURES_PATH.read_text())
    else:
        cap = run_all()
        CAPTURES_PATH.write_text(json.dumps(cap, indent=2, ensure_ascii=False))
        print(f"wrote {CAPTURES_PATH}")
    html_text = render(cap, load_findings())
    HTML_PATH.write_text(html_text)
    sc = cap.get("scorecard", {})
    l1 = sc.get("layer1", {}).get("totals", {})
    l2 = sc.get("layer2", {}).get("totals", {})
    print(f"wrote {HTML_PATH} ({len(html_text)} bytes)")
    print(
        f"layer1 {l1.get('passed')}/{l1.get('checks')}  layer2 {l2.get('matched')}/{l2.get('expected')}  "
        f"runner_errors={len(cap.get('meta', {}).get('runner_errors', []))}"
    )
    for err in cap.get("meta", {}).get("runner_errors", []):
        print(f"  error: {err}")


if __name__ == "__main__":
    main()
