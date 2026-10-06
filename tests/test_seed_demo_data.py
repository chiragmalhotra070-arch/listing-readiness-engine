from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import Base, BusinessIdentityOwnership, Customer, Document, Email  # noqa: E402
from scripts.seed_demo_data import (  # noqa: E402
    EXPECTED_ALEMBIC_VERSION,
    EXPECTED_DATABASE,
    SeedGuardError,
    apply_seed,
    check_guards,
    main,
)


def build_db(tmp_path: Path, name: str = "seed_target.db"):
    path = tmp_path / name
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)  # conftest hook seeds the 5 demo customers
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": EXPECTED_ALEMBIC_VERSION})
    return engine, path


def counts(engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            table: int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0)
            for table in ("customers", "emails", "documents", "business_identity_ownership")
        }


def test_apply_seed_creates_the_ten_row_baseline_and_is_idempotent(tmp_path):
    engine, _ = build_db(tmp_path)
    factory = sessionmaker(bind=engine)

    with factory() as db:
        report = apply_seed(db)
        db.commit()

    assert counts(engine) == {"customers": 5, "emails": 1, "documents": 2, "business_identity_ownership": 2}
    assert report["existing"]["customers"] == 5
    assert report["created"]["emails"] == 1
    assert report["created"]["documents"] == 2
    assert report["created"]["business_identity_ownership"] == 2

    with factory() as db:
        email = db.scalar(select(Email))
        assert email.source_email_id == "seed-structural-0001"
        assert email.status == "COMPLETED"
        assert email.received_at.isoformat().startswith("2026-01-01")

        docs = {doc.document_name: doc for doc in db.scalars(select(Document))}
        assert set(docs) == {"baseline-inv-2026-1042.pdf", "baseline-rem-2026-009.pdf"}
        invoice = docs["baseline-inv-2026-1042.pdf"]
        assert invoice.decision == "READY_FOR_PROCESSING"
        assert invoice.processing_status == "COMPLETED"
        assert invoice.current_stage == "COMPLETE"
        assert invoice.stage_status == "SUCCESS"
        assert invoice.attempt_count == 0
        assert invoice.duplicate is False
        assert invoice.extracted_data == {"invoice_number": "INV-2026-1042"}
        remittance = docs["baseline-rem-2026-009.pdf"]
        assert remittance.extracted_data == {"remittance_number": "REM-2026-009"}

        ownership = {
            (row.document_type, row.business_identity): row
            for row in db.scalars(select(BusinessIdentityOwnership))
        }
        assert set(ownership) == {("INVOICE", '["INV-2026-1042"]'), ("REMITTANCE", '["REM-2026-009"]')}
        assert ownership[("INVOICE", '["INV-2026-1042"]')].owner_document_id == invoice.id
        assert ownership[("REMITTANCE", '["REM-2026-009"]')].owner_document_id == remittance.id

    with factory() as db:
        rerun = apply_seed(db)
        db.commit()
    assert sum(rerun["created"].values()) == 0
    assert counts(engine) == {"customers": 5, "emails": 1, "documents": 2, "business_identity_ownership": 2}


def test_main_seeds_and_reruns_with_exit_zero(tmp_path, capsys):
    engine, path = build_db(tmp_path)
    url = f"sqlite:///{path}"
    allow = engine.url.database

    assert main(["--database-url", url, "--allow-database", allow]) == 0
    first = capsys.readouterr()
    assert "created=5 rows" in first.out
    assert "REFUSED" not in first.err

    assert main(["--database-url", url, "--allow-database", allow]) == 0
    second = capsys.readouterr()
    assert "created=0 rows" in second.out
    assert counts(engine) == {"customers": 5, "emails": 1, "documents": 2, "business_identity_ownership": 2}


def test_main_refuses_destination_name_without_allow_flag(tmp_path, capsys):
    _, path = build_db(tmp_path)
    assert main(["--database-url", f"sqlite:///{path}"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("REFUSED:")
    assert EXPECTED_DATABASE in err


def test_main_accepts_the_expected_destination_name(tmp_path):
    _, path = build_db(tmp_path, name=EXPECTED_DATABASE)
    assert main(["--database-url", f"sqlite:///{path}"]) == 0


def test_main_refuses_wrong_alembic_version(tmp_path, capsys):
    engine, path = build_db(tmp_path)
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = '0006_document_work_items'"))
    assert main(["--database-url", f"sqlite:///{path}", "--allow-database", engine.url.database]) == 1
    assert "alembic_version" in capsys.readouterr().err


def test_main_refuses_missing_alembic_table(tmp_path, capsys):
    engine, path = build_db(tmp_path)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
    assert main(["--database-url", f"sqlite:///{path}", "--allow-database", engine.url.database]) == 1
    assert "alembic_version" in capsys.readouterr().err


def test_main_refuses_existing_history_rows(tmp_path, capsys):
    engine, path = build_db(tmp_path)
    with engine.begin() as conn:
        customer_id = conn.execute(text("SELECT id FROM customers LIMIT 1")).scalar()
        conn.execute(
            text("INSERT INTO customer_email_mappings (customer_id, email_address, verified) VALUES (:c, :e, 0)"),
            {"c": customer_id, "e": "someone@example.com"},
        )
    assert main(["--database-url", f"sqlite:///{path}", "--allow-database", engine.url.database]) == 1
    assert "customer_email_mappings" in capsys.readouterr().err


def test_main_refuses_customer_count_outside_seed_contract(tmp_path, capsys):
    engine, path = build_db(tmp_path)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO customers (customer_id, customer_name, created_at, updated_at) VALUES ('EXTRA-1', 'Extra Customer', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
    assert main(["--database-url", f"sqlite:///{path}", "--allow-database", engine.url.database]) == 1
    assert "customers" in capsys.readouterr().err


def test_main_refuses_partial_seed_state(tmp_path, capsys):
    engine, path = build_db(tmp_path)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        email = Email(
            source_email_id="partial-state",
            received_at=datetime.now(timezone.utc),
            sender_email="x@example.com",
        )
        db.add(email)
        db.flush()
        db.add(Document(document_name="one-row.pdf", mime_type="application/pdf", email_id=email.id))
        db.commit()
    assert main(["--database-url", f"sqlite:///{path}", "--allow-database", engine.url.database]) == 1
    assert "documents" in capsys.readouterr().err


def test_check_guards_reports_missing_required_table(tmp_path):
    engine, _ = build_db(tmp_path)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE business_identity_ownership"))
    with pytest.raises(SeedGuardError, match="business_identity_ownership"):
        check_guards(engine, allow_database=engine.url.database)
