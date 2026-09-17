from shared.models import Service, ServiceStatus


def test_list_services(client, db_session):
    db_session.add(Service(name="Test Service", description="...", status=ServiceStatus.OPERATIONAL))
    db_session.commit()

    response = client.get("/services")

    assert response.status_code == 200
    names = [s["name"] for s in response.json()]
    assert "Test Service" in names
