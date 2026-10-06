from __future__ import annotations

from typing import Any

from sqlalchemy import String, inspect as sqlalchemy_inspect
from sqlalchemy.dialects import postgresql

NUL_BYTE = "\x00"

#: ``column.info`` key marking a String/Text column as free-form prose.
FREE_FORM_TEXT_INFO = "free_form_text"

#: ``column.info`` key carrying the NUL policy of a JSONB column.
JSONB_POLICY_INFO = "jsonb_policy"

#: The only JSONB policy implemented: reject the value, never rewrite it.
JSONB_REJECT = "reject"


class NulByteRejectedError(ValueError):
    """Raised when a NUL byte appears in a structured text or JSONB column.

    Structured values are *rejected* rather than rewritten: silently deleting a
    byte from an identifier, hash or reference can collapse two distinct values
    onto one another, break idempotency, or point at the wrong object.  A JSONB
    value is rejected for a database reason instead: PostgreSQL ``jsonb`` input
    turns ``\\u0000`` into SQLSTATE ``22P05`` and aborts the transaction, so the
    value has to be refused in the application where the failure is typed and
    recoverable.  The message deliberately names the column but never echoes the
    offending value.
    """

    def __init__(self, column_label: str) -> None:
        super().__init__(
            f"NUL (0x00) byte is not permitted in structured column {column_label}; "
            "refusing to rewrite an identifier or reference"
        )
        self.column_label = column_label


def sanitize_persisted_text(value: str | None) -> str | None:
    """Strip NUL (0x00) bytes from **free-form** text about to be persisted.

    PostgreSQL ``text``/``varchar`` cannot store NUL, and psycopg 3 rejects the
    parameter with ``DataError: PostgreSQL text fields cannot contain NUL
    (0x00) bytes`` before the statement runs.  Only ever apply this to prose -
    see :func:`ensure_structured_text` for identifiers.
    """
    if value is None:
        return None
    if NUL_BYTE not in value:
        return value
    return value.replace(NUL_BYTE, "")


def ensure_structured_text(value: str | None, column_label: str) -> str | None:
    """Validate an untrusted identifier that is about to be used as a query parameter.

    ``before_flush`` never sees SQL parameters, so query-bound identifiers must
    be checked at the call site.  Unlike :func:`sanitize_persisted_text` this
    never rewrites the value: a lookup key must compare equal to the key that
    will be stored, and neither may be silently altered.
    """
    if value is None:
        return None
    if NUL_BYTE in value:
        raise NulByteRejectedError(column_label)
    return value


def jsonb_contains_nul(value: Any) -> bool:
    """Recursively look for a NUL byte anywhere inside a JSON-compatible value.

    PostgreSQL ``jsonb`` input turns ``\\u0000`` into SQLSTATE ``22P05``
    (``unsupported Unicode escape sequence``) and aborts the whole transaction,
    which is how a single bad extraction value came to break error and audit
    persistence as well.  Detection therefore happens on the Python value,
    before SQLAlchemy serialises it and long before the driver sees it.

    Keys, values, list/tuple items and nested containers are all inspected;
    ``None``, numbers and booleans are text-free by definition.  The value is
    never inspected for anything except a NUL byte and is never modified.
    """
    if isinstance(value, str):
        return NUL_BYTE in value
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and NUL_BYTE in key:
                return True
            if jsonb_contains_nul(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(jsonb_contains_nul(item) for item in value)
    return False


def ensure_jsonb_value(value: Any, column_label: str) -> None:
    """Reject a NUL-bearing value that is about to be persisted as JSONB.

    Mirrors :func:`ensure_structured_text`: validate at the call site when the
    value is built, so nothing is ever mutated and PostgreSQL never gets the
    chance to raise ``22P05``.  ``None`` and NUL-free values pass untouched.
    """
    if jsonb_contains_nul(value):
        raise NulByteRejectedError(column_label)


def is_jsonb_column(column: Any) -> bool:
    """True for a column persisted as PostgreSQL ``jsonb``.

    Explicit ``column.info`` metadata is the primary marker, consistent with
    ``free_form_text``.  A column that is *modelled* as JSONB but carries no
    marker still counts: the absence of a policy fails closed rather than
    silently disabling the check.  Plain ``json`` columns - deliberately out of
    scope this phase - return ``False`` and keep their existing behaviour.
    """
    if JSONB_POLICY_INFO in column.info:
        return True
    column_type = column.type
    if isinstance(column_type, postgresql.JSONB):
        return True
    variants = getattr(column_type, "_variant_mapping", None) or {}
    return any(isinstance(variant, postgresql.JSONB) for variant in variants.values())


def _nul_bearing_string_columns(obj: Any):
    """Yield ``(attribute, column, value)`` for loaded String/Text attributes holding a NUL.

    JSON/JSONB columns are handled by :func:`_nul_bearing_jsonb_columns`,
    primary keys are skipped, and an attribute is skipped when it is not in
    ``state.dict`` - i.e. it is expired or never loaded - so the hook never
    forces a lazy load during a flush.  An attribute that is about to be
    written is always present in ``state.dict``.
    """
    state = sqlalchemy_inspect(obj, raiseerr=False)
    mapper = getattr(state, "mapper", None)
    if mapper is None:
        return
    instance_dict = state.dict
    for attribute in mapper.column_attrs:
        columns = attribute.columns
        if len(columns) != 1 or not isinstance(columns[0].type, String):
            continue
        if columns[0].primary_key or attribute.key not in instance_dict:
            continue
        value = instance_dict[attribute.key]
        if isinstance(value, str) and NUL_BYTE in value:
            yield attribute, columns[0], value


def _nul_bearing_jsonb_columns(obj: Any):
    """Yield ``(attribute, column)`` for loaded JSONB attributes whose value holds a NUL.

    Only the *loaded* attributes are inspected, for the same reason as in
    :func:`_nul_bearing_string_columns`: a flush must never trigger a lazy
    load.  Nothing is rewritten - the caller raises, and the in-memory value is
    left exactly as it was.
    """
    state = sqlalchemy_inspect(obj, raiseerr=False)
    mapper = getattr(state, "mapper", None)
    if mapper is None:
        return
    instance_dict = state.dict
    for attribute in mapper.column_attrs:
        columns = attribute.columns
        if len(columns) != 1 or not is_jsonb_column(columns[0]):
            continue
        if columns[0].primary_key or attribute.key not in instance_dict:
            continue
        value = instance_dict[attribute.key]
        if value is not None and jsonb_contains_nul(value):
            yield attribute, columns[0]


def validate_persisted_object(obj: Any) -> None:
    """Raise :class:`NulByteRejectedError` for any structured or JSONB column carrying a NUL.

    Every String/Text column that is *not* flagged ``free_form_text`` is
    structured, so this fails closed: a newly added column is protected before
    anyone classifies it.  Every JSONB column is rejected as well, because
    PostgreSQL would otherwise abort the transaction with ``22P05``; plain
    ``json`` columns are deliberately left alone this phase.
    """
    for _attribute, column, _value in _nul_bearing_string_columns(obj):
        if column.info.get(FREE_FORM_TEXT_INFO):
            continue
        raise NulByteRejectedError(f"{column.table.name}.{column.name}")
    for _attribute, column in _nul_bearing_jsonb_columns(obj):
        raise NulByteRejectedError(f"{column.table.name}.{column.name}")


def sanitize_persisted_object(obj: Any) -> int:
    """Apply :func:`sanitize_persisted_text` to free-form columns only.

    Structured and JSONB columns are left untouched - they are the
    responsibility of :func:`validate_persisted_object`.  An attribute is
    rewritten only when the value actually changes, so clean rows are never
    dirtied.

    Returns the number of attributes rewritten.
    """
    rewritten = 0
    for attribute, column, value in _nul_bearing_string_columns(obj):
        if not column.info.get(FREE_FORM_TEXT_INFO):
            continue
        setattr(obj, attribute.key, sanitize_persisted_text(value))
        rewritten += 1
    return rewritten


def sanitize_session_text(session: Any, flush_context: Any = None, instances: Any = None) -> None:
    """``before_flush`` hook: enforce the text invariant at the persistence boundary.

    Registered once in :mod:`app.db.session`, so every ORM insert or update -
    intake, worker, retry, reprocess, audit, attempts, scripts - passes through
    here.  Validation runs over the whole pending set before anything is
    rewritten, so a rejection never leaves partially rewritten objects behind.
    JSONB columns are validated in that same first pass and are never mutated:
    a NUL-bearing value raises while the in-memory object still holds it
    unchanged, which keeps PostgreSQL from ever seeing ``22P05``.
    """
    pending = list(session.new) + list(session.dirty)
    for obj in pending:
        validate_persisted_object(obj)
    for obj in pending:
        sanitize_persisted_object(obj)
