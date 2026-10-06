from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AuditEvent, Document, Email
from app.services.text_sanitization import ensure_jsonb_value


def record_audit(db: Session, *, event_type: str, status: str, message: str, document_id: int | None = None, email_id: int | None = None, details: dict[str, Any] | None = None) -> None:
    # ``audit_events.details`` is JSONB: a NUL nested in provider diagnostics or
    # classification evidence would otherwise reach PostgreSQL's jsonb parser and
    # abort the transaction with 22P05 - taking the very error record that should
    # have explained the failure down with it.  Validate before the row exists so
    # the rejection is typed and the offending payload is never echoed.
    ensure_jsonb_value(details, "audit_events.details")
    correlation_id = None
    n8n_execution_id = None
    if document_id is not None:
        document = db.get(Document, document_id)
        if document is not None:
            email_id = email_id or document.email_id
    if email_id is not None:
        email = db.get(Email, email_id)
        if email is not None:
            correlation_id = email.correlation_id
            n8n_execution_id = email.n8n_execution_id
    db.add(AuditEvent(email_id=email_id, document_id=document_id, event_type=event_type, status=status, message=message, details=details, correlation_id=correlation_id, n8n_execution_id=n8n_execution_id))
