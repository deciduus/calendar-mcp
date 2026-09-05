"""FastAPI TestClient tests for src/server.py.

Startup authentication is stubbed out and the credentials dependency is
overridden, so no network access and no Google credentials are required.
"""
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src import server
from src.models import CalendarListEntry, CalendarListResponse

CALENDARS = CalendarListResponse(
    items=[
        CalendarListEntry(etag='"1"', id="primary", summary="Personal", accessRole="owner", primary=True),
        CalendarListEntry(etag='"2"', id="team@example.com", summary="Team", accessRole="writer"),
    ]
)


@pytest.fixture
def client():
    """TestClient with startup auth stubbed and the creds dependency overridden."""
    fake_creds = MagicMock(name="credentials")
    fake_creds.valid = True
    server.app.dependency_overrides[server.get_current_credentials] = lambda: fake_creds
    with patch.object(server, "get_credentials", return_value=fake_creds):
        with TestClient(server.app) as test_client:
            yield test_client
    server.app.dependency_overrides.clear()


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "authentication" in body


def test_list_calendars_returns_calendar_list(client):
    with patch.object(server.calendar_actions, "find_calendars", return_value=CALENDARS) as mocked:
        response = client.get("/calendars")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == ["primary", "team@example.com"]
    assert mocked.call_args.kwargs["min_access_role"] is None


def test_list_calendars_passes_min_access_role(client):
    with patch.object(server.calendar_actions, "find_calendars", return_value=CALENDARS) as mocked:
        response = client.get("/calendars", params={"min_access_role": "writer"})

    assert response.status_code == 200
    assert mocked.call_args.kwargs["min_access_role"] == "writer"


def test_list_calendars_returns_500_when_action_fails(client):
    with patch.object(server.calendar_actions, "find_calendars", return_value=None):
        response = client.get("/calendars")

    assert response.status_code == 500
    assert "Failed to retrieve calendar list" in response.json()["detail"]
