from shared.models import User, UserRole


def test_get_user(client, db_session):
    user = User(name="Test User", email="get.user.test@example.com", role=UserRole.AGENT)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    response = client.get(f"/users/{user.id}")

    assert response.status_code == 200
    assert response.json()["email"] == "get.user.test@example.com"


def test_get_user_not_found(client):
    response = client.get("/users/999999")
    assert response.status_code == 404
