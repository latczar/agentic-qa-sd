"""Same approach as worker/tests: the tools manage their own sessions, so
point shared.db at the test database rather than trying to wrap them in a
transaction that would be invisible to a separate connection.
"""

import os

os.environ.setdefault("EMBEDDING_PROVIDER", "fake")

import pytest
from sqlalchemy.orm import sessionmaker

import shared.db as shared_db
from shared.models import Base
from shared.testing import test_engine

shared_db.engine = test_engine
shared_db.SessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    from sqlalchemy import text

    with test_engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
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
