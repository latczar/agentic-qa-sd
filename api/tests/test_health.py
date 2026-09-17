from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_ok_when_database_reachable():
    with patch("app.main.ping_database", return_value=True):
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "connected"}


def test_health_returns_503_when_database_unreachable():
    with patch("app.main.ping_database", side_effect=ConnectionError("connection refused")):
        response = client.get("/health")

    assert response.status_code == 503
    assert "database unreachable" in response.json()["detail"]
