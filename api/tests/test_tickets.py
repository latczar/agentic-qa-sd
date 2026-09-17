from shared.models import User, UserRole


def _make_user(db_session, email: str = "test.user@example.com") -> User:
    user = User(name="Test User", email=email, role=UserRole.END_USER)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_create_ticket(client, db_session):
    user = _make_user(db_session)

    response = client.post(
        "/tickets",
        json={
            "submitted_by_id": user.id,
            "subject": "Cannot log in after password reset",
            "description": "I reset my password but I still cannot log in.",
        },
    )

    assert response.status_code == 201
    body = response.json()
    # QUEUED, not NEW: creating a ticket also publishes it to RabbitMQ (Phase 3),
    # and this test runs against a real broker, so the publish really succeeds.
    assert body["status"] == "QUEUED"
    assert body["submitted_by_id"] == user.id
    assert body["priority"] is None


def test_get_ticket_not_found(client):
    response = client.get("/tickets/999999")
    assert response.status_code == 404


def test_list_tickets(client, db_session):
    user = _make_user(db_session, "lister@example.com")
    client.post(
        "/tickets",
        json={"submitted_by_id": user.id, "subject": "VPN not connecting", "description": "..."},
    )

    response = client.get("/tickets")

    assert response.status_code == 200
    assert len(response.json()) >= 1


def test_update_ticket_status_and_priority(client, db_session):
    user = _make_user(db_session, "updater@example.com")
    created = client.post(
        "/tickets",
        json={"submitted_by_id": user.id, "subject": "Email sync broken", "description": "..."},
    ).json()

    response = client.patch(f"/tickets/{created['id']}", json={"status": "QUEUED", "priority": "HIGH"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "QUEUED"
    assert body["priority"] == "HIGH"


def test_update_ticket_not_found(client):
    response = client.patch("/tickets/999999", json={"status": "QUEUED"})
    assert response.status_code == 404


def test_add_comment(client, db_session):
    user = _make_user(db_session, "commenter@example.com")
    created = client.post(
        "/tickets",
        json={"submitted_by_id": user.id, "subject": "MFA failing", "description": "..."},
    ).json()

    response = client.post(
        f"/tickets/{created['id']}/comments",
        json={"body": "Looking into it", "author_id": user.id},
    )

    assert response.status_code == 201
    assert response.json()["body"] == "Looking into it"


def test_add_comment_ticket_not_found(client):
    response = client.post("/tickets/999999/comments", json={"body": "..."})
    assert response.status_code == 404
