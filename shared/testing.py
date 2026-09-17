"""Postgres test harness shared by api/tests and worker/tests.

Both suites need the same thing: a real Postgres (never SQLite), a dedicated
test database so tests never touch dev data, and one rolled-back transaction
per test so tests never see each other's data either.
"""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from shared.models import Base

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://sd_user:sd_password@localhost:5432/service_desk_test",
)


def ensure_test_database_exists() -> None:
    """Postgres has no CREATE DATABASE IF NOT EXISTS, so check first, then create."""
    admin_url = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/postgres"
    db_name = TEST_DATABASE_URL.rsplit("/", 1)[1]
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": db_name}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        admin_engine.dispose()


ensure_test_database_exists()
test_engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.create_all(test_engine)
    yield
    Base.metadata.drop_all(test_engine)


@pytest.fixture
def db_session():
    """One test = one transaction, rolled back at the end so tests never see
    each other's data.

    Code under test calls db.commit() itself. join_transaction_mode="create_savepoint"
    makes those commits land on a SAVEPOINT nested inside our outer transaction,
    so the transaction.rollback() below still discards everything the test did.
    """
    connection = test_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
