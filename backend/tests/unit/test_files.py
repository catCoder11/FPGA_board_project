"""Тесты загрузки файла на плату. БД и агент подменяются заглушками."""
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import deps
from app.main import app
from app.routes import files

client = TestClient(app)
BEARER = {"Authorization": "Bearer sometoken"}
EXPIRES = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConn:
    def __init__(self, handler):
        self.handler = handler

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        return FakeResult(self.handler(" ".join(sql.split()), params))


def db(connected: bool):
    """БД, где пользователь 1 вошёл, и (если connected) занимает плату №2."""

    def handler(sql, params):
        if "FROM user_sessions" in sql:
            return (1, "test_user")
        if "FROM board_sessions" in sql and connected:
            return (5, 2, "board-2", EXPIRES, 900)
        return None

    return FakeConn(handler)


class FakeAgentClient:
    """Вместо настоящего httpx.AsyncClient: запоминает запрос к агенту и отвечает 200."""

    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        FakeAgentClient.calls.append((url, kwargs))
        return SimpleNamespace(status_code=200)


def upload(name="design.sof", content=b"binary-data"):
    return client.post("/send_file", headers=BEARER, files={"file": (name, content)})


def test_send_file_requires_login():
    r = client.post("/send_file", files={"file": ("design.sof", b"x")})
    assert r.status_code == 401


def test_send_file_requires_board_session(monkeypatch):
    monkeypatch.setattr(deps, "connect", lambda: db(connected=False))
    assert upload().status_code == 403


def test_send_file_rejects_wrong_extension(monkeypatch):
    monkeypatch.setattr(deps, "connect", lambda: db(connected=True))
    r = upload(name="design.exe")
    assert r.status_code == 400


def test_send_file_rejects_empty_file(monkeypatch):
    monkeypatch.setattr(deps, "connect", lambda: db(connected=True))
    assert upload(content=b"").status_code == 400


def test_send_file_goes_to_the_users_board(monkeypatch):
    FakeAgentClient.calls.clear()
    monkeypatch.setattr(deps, "connect", lambda: db(connected=True))
    monkeypatch.setattr(files.httpx, "AsyncClient", FakeAgentClient)
    r = upload()
    assert r.status_code == 200
    assert r.json()["board_id"] == 2  # плата берётся из сессии, а не из константы
    url, kwargs = FakeAgentClient.calls[0]
    assert url.endswith("/upload")
    assert kwargs["data"] == {"board_id": "2"}
    assert kwargs["headers"]["Authorization"] == f"Bearer {files.BOARD_AGENT_TOKEN}"


def test_results_stub_requires_login():
    assert client.get("/results").status_code == 401