import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from shared.db import get_db
from shared.testing import _schema, db_session  # noqa: F401 - re-exported as pytest fixtures


@pytest.fixture
def client(db_session: Session):
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()
