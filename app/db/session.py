from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings
from app.services.text_sanitization import sanitize_session_text


class Base(DeclarativeBase):
    pass


engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

# Persistence boundary: PostgreSQL text columns must never receive a NUL byte.
# Covers every ORM insert/update - API intake, worker, retry/reprocess, audit,
# attempts and scripts - without touching adapter or business code.
event.listen(Session, "before_flush", sanitize_session_text)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
