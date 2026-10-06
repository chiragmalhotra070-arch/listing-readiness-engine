from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, insert
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.models import Base, Customer
from app.db.session import get_db
from app.main import app

# Hostnames a test run is allowed to resolve for DATABASE_URL. Anything else
# (a managed Postgres, a staging/production Supabase project, ...) is refused.
LOCAL_DATABASE_HOSTS = {"localhost", "127.0.0.1", "::1", "db"}


@pytest.fixture(scope="session", autouse=True)
def guard_against_non_local_database():
    """Refuse to run the suite against a non-local database.

    Every test builds its own SQLite or temporary-Postgres engine, so the
    configured ``DATABASE_URL`` should never be reached. If it ever resolves
    to a shared or production host - for example because a ``DATABASE_URL``
    was added to ``.env`` - this trips before any test can touch it.

    The fixture only *reads* configuration; it never mutates it, and it does
    not override the scratch URL used by the PostgreSQL migration tests.
    """
    from sqlalchemy.engine import make_url

    url = make_url(get_settings().database_url)
    if url.get_backend_name() == "sqlite":
        return
    if (url.host or "") in LOCAL_DATABASE_HOSTS:
        return
    pytest.fail(
        "Refusing to run tests: DATABASE_URL resolves to non-local host "
        f"{url.host!r}. Point it at SQLite or a throwaway local database, "
        "or unset DATABASE_URL so app.config falls back to its default.",
    )


@event.listens_for(Customer.__table__, "after_create")
def seed_demo_customers_for_test_db(target, connection, **kwargs):
    connection.execute(insert(Customer.__table__), [
        {"customer_id": "CUST-001", "customer_name": "Fixture Customer"},
        {"customer_id": "CN0044", "customer_name": "Awthentikz"},
        {"customer_id": "LG001", "customer_name": "La Galerie"},
        {"customer_id": "VO001", "customer_name": "Vision Operations"},
        {"customer_id": "GC001", "customer_name": "GE Capital"},
    ])


@pytest.fixture(autouse=True)
def isolate_llm_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "mock-v1")
    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DOCUMENT_STORAGE_ROOT", str(tmp_path / "documents"))
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "mock-v1")
    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app, headers={"X-Intake-API-Key": "dev-intake-key"})
    app.dependency_overrides.clear()
    get_settings.cache_clear()
