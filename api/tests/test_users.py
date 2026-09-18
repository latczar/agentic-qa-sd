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


def test_list_users_returns_everyone(client, db_session):
    db_session.add(User(name="Zoe Lister", email="zoe.lister@example.com", role=UserRole.AGENT))
    db_session.commit()

    response = client.get("/users")

    assert response.status_code == 200
    emails = [u["email"] for u in response.json()]
    assert "zoe.lister@example.com" in emails


def test_ui_is_served_at_the_root(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "AI Service Desk" in response.text
