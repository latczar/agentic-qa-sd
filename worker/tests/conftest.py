"""Worker tests need a different isolation strategy than api/tests.

on_message manages its own database session per call (SessionLocal()) — there's
no request-scoped dependency to swap out like FastAPI's Depends(get_db). That
also means the API's "wrap everything in one uncommitted transaction and roll
it back" trick doesn't work here: on_message's session would be a *different*
connection than a test's setup session, and an uncommitted transaction on one
Postgres connection is invisible to another. So instead, we point shared.db's
engine at the real test database for the whole test session, let writes
actually commit, and rely on unique test data per test instead of rollback.
"""

import os

# Set before any shared.* import: shared.config reads the environment at import
# time, and orchestrator binds RETRIEVAL_MODE by value. Orchestration tests
# exercise everything downstream of retrieval and shouldn't need a running MCP
# server to do it.
os.environ.setdefault("RETRIEVAL_MODE", "direct")

import pytest
from sqlalchemy.orm import sessionmaker

import shared.db as shared_db
from shared.models import Base
from shared.testing import test_engine

shared_db.engine = test_engine
shared_db.SessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.create_all(test_engine)
    yield
    Base.metadata.drop_all(test_engine)


@pytest.fixture
def db_session():
    session = shared_db.SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
