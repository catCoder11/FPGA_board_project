"""Проверки сохранённого маршрута загрузки из dima_changes_in_progress."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routes import files

client = TestClient(app)


@pytest.fixture
def configured_agent(monkeypatch):
    monkeypatch.setattr(files, "BOARD_AGENT_URL", "http://board.test:9000")
    monkeypatch.setattr(files, "BOARD_AGENT_TOKEN", "test-agent-token")
    monkeypatch.setitem(app.dependency_overrides, files.get_board_session, lambda: SimpleNamespace(board_id=files.BOARD_ID))


def upload(name="design.sof", content=b"binary-data"):
    return client.post("/send_file", files={"file": (name, content)})


def test_send_file_requires_login():
    assert upload().status_code == 401


def test_send_file_rejects_another_board(configured_agent, monkeypatch):
    monkeypatch.setitem(
        app.dependency_overrides, files.get_board_session,
        lambda: SimpleNamespace(board_id=files.BOARD_ID + 1),
    )
    assert upload().status_code == 403


def test_send_file_rejects_wrong_extension(configured_agent):
    assert upload(name="design.exe").status_code == 400


def test_send_file_rejects_empty_file(configured_agent):
    assert upload(content=b"").status_code == 400


def test_send_file_goes_to_configured_agent(configured_agent, monkeypatch):
    calls = []

    class FakeAgentClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(files.httpx, "AsyncClient", FakeAgentClient)
    response = upload()
    assert response.status_code == 200
    assert response.json()["board_id"] == files.BOARD_ID
    url, kwargs = calls[0]
    assert url == f"{files.BOARD_AGENT_URL}/upload"
    assert kwargs["data"] == {"board_id": str(files.BOARD_ID)}
    assert kwargs["headers"]["Authorization"] == f"Bearer {files.BOARD_AGENT_TOKEN}"


def test_results_requires_login():
    assert client.get("/results").status_code == 401
