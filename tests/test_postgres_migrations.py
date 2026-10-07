"""PostgreSQL-backed migration tests.

These run only when ``ENGINE_PG_TEST_DATABASE_URL`` is set.  The URL must
point at a database this suite is allowed to **wipe**: every test drops and
recreates the ``public`` schema first.

    docker exec schema-audit-pg psql -U engine -d postgres \
        -c "CREATE DATABASE financial_pgtest;"
    ENGINE_PG_TEST_DATABASE_URL=postgresql+psycopg://engine:engine@localhost:5433/financial_pgtest \
        python3 -m pytest tests/test_postgres_migrations.py -q
"""

from __future__ import annotations

import os

import pytest

DATABASE_URL = os.environ.get("ENGINE_PG_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="ENGINE_PG_TEST_DATABASE_URL is not set (PostgreSQL migration tests need a scratch database)",
)


@pytest.fixture(scope="module")
def migrated():
    """Drop the schema, run every migration, and hand back a connection factory.

    ``migrations/env.py`` force-sets ``sqlalchemy.url`` from
    ``get_settings().database_url``, so the DATABASE_URL environment variable
    (and the settings cache) must be pointed at the scratch database - the
    alembic ``Config`` URL alone is ignored.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.db.session import Base  # noqa: F401  (ensures models are imported)

    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = DATABASE_URL
    get_settings.cache_clear()

    engine = create_engine(DATABASE_URL)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))

        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        cfg = Config(os.path.join(root, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(root, "migrations"))
        command.upgrade(cfg, "head")

        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
        get_settings.cache_clear()
        engine.dispose()


def test_alembic_reaches_a_single_head(migrated):
    from sqlalchemy import create_engine, text

    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        rows = [row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))]
    engine.dispose()
    assert rows == ["0018_requirement_owner"]


def test_reference_architecture_is_not_created(migrated):
    from sqlalchemy import create_engine, inspect

    engine = create_engine(DATABASE_URL)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    for forbidden in ("reference_customers", "reference_emails", "reference_documents", "document_field_values", "field_change_events"):
        assert forbidden not in tables


def test_dead_document_columns_are_dropped(migrated):
    from sqlalchemy import create_engine, inspect

    engine = create_engine(DATABASE_URL)
    columns = {column["name"] for column in inspect(engine).get_columns("documents")}
    engine.dispose()
    assert columns.isdisjoint({"total_amount", "currency", "document_date"})


def _seed(session_factory, source_email_id: str = "pg-test"):
    from datetime import datetime, timezone

    from app.db.models import Document, Email, ProcessingAttempt, ProcessingRun

    with session_factory() as db:
        email = Email(source_email_id=source_email_id, received_at=datetime.now(timezone.utc), sender_email="pg@example.test", status="RECEIVED")
        db.add(email)
        db.flush()
        document = Document(
            email_id=email.id,
            document_name="pg.pdf",
            mime_type="application/pdf",
            processing_status="PENDING",
            current_stage="DOCUMENT_PARSING",
            stage_status="PENDING",
            overall_status="PENDING",
            duplicate=False,
            document_type="UNKNOWN",
        )
        db.add(document)
        db.flush()
        run = ProcessingRun(document_id=document.id, trigger="INTAKE", started_at=datetime.now(timezone.utc), outcome="COMPLETED")
        db.add(run)
        db.flush()
        attempt = ProcessingAttempt(
            document_id=document.id,
            run_id=run.id,
            attempt_number=1,
            stage="OCR",
            provider="tesseract",
            started_at=datetime.now(timezone.utc),
            status="SUCCESS",
        )
        db.add(attempt)
        db.flush()
        db.commit()
        return document.id, run.id, attempt.id


def test_run_id_is_enforced_not_null(migrated):
    import pytest
    from sqlalchemy import text

    document_id, run_id, attempt_id = _seed(migrated, source_email_id="pg-notnull")
    with migrated() as db, pytest.raises(Exception):
        db.execute(
            text(
                "INSERT INTO processing_attempts "
                "(document_id, attempt_number, stage, provider, started_at, status) "
                "VALUES (:doc, 2, 'OCR', 'tesseract', now(), 'SUCCESS')"
            ),
            {"doc": document_id},
        )
        db.commit()


def test_extracted_fields_are_immutable(migrated):
    import pytest
    from sqlalchemy import text

    from app.db.models import ExtractedField

    document_id, run_id, attempt_id = _seed(migrated, source_email_id="pg-immutable")
    with migrated() as db:
        field = ExtractedField(
            attempt_id=attempt_id,
            document_id=document_id,
            run_id=run_id,
            field_name="invoice_number",
            field_value="INV-1",
            value_type="string",
            source="LLM",
            confidence=0.9,
        )
        db.add(field)
        db.commit()
        field_id = field.id

    with migrated() as db, pytest.raises(Exception):
        db.execute(text("UPDATE extracted_fields SET field_name = 'tampered' WHERE id = :id"), {"id": field_id})
        db.commit()

    with migrated() as db, pytest.raises(Exception):
        db.execute(text("DELETE FROM extracted_fields WHERE id = :id"), {"id": field_id})
        db.commit()

    with migrated() as db:
        remaining = db.execute(text("SELECT count(*) FROM extracted_fields WHERE id = :id"), {"id": field_id}).scalar()
    assert remaining == 1


def test_extracted_fields_require_a_valid_run(migrated):
    import pytest
    from sqlalchemy import text

    document_id, run_id, attempt_id = _seed(migrated, source_email_id="pg-fk")
    with migrated() as db, pytest.raises(Exception):
        db.execute(
            text(
                "INSERT INTO extracted_fields "
                "(attempt_id, document_id, run_id, field_name, field_value, value_type, source) "
                "VALUES (:a, :d, 999999, 'invoice_number', '\"INV-1\"'::jsonb, 'string', 'LLM')"
            ),
            {"a": attempt_id, "d": document_id},
        )
        db.commit()


def test_jsonb_column_types(migrated):
    from sqlalchemy import create_engine, text

    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE (table_name, column_name) IN "
                "(('documents','extracted_data'),('documents','evidence'),"
                "('audit_events','details'),('extracted_fields','field_value'))"
            )
        ).all()
    engine.dispose()
    assert {(row[0], row[1]) for row in rows} == {
        ("documents", "extracted_data"),
        ("documents", "evidence"),
        ("audit_events", "details"),
        ("extracted_fields", "field_value"),
    }
    assert all(row[2] == "jsonb" for row in rows)
