"""Тесты списка плат и подключения. БД не нужна: соединение подменяется заглушкой."""
from datetime import datetime, timezone
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors

from app import deps
from app.main import app
from app.routes import boards

client = TestClient(app)
BEARER = {"Authorization": "Bearer sometoken"}
EXPIRES = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def configure_hosts(monkeypatch):
    monkeypatch.setattr(
        boards, "load_hosts", lambda: {
            f"board-{i}": {"is_enabled": i != 5, "board_token": "test-only"}
            for i in range(1, 6)
        }
    )


class FakeResult:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return self.value

    def fetchall(self):
        return self.value


class FakeConn:
    """Подменяет psycopg-соединение. handler(sql, params) решает, что вернуть на запрос."""

    def __init__(self, handler=None, error=None):
        self.handler = handler or (lambda sql, params: None)
        self.error = error
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        if self.error:
            raise self.error
        sql = " ".join(sql.split())
        self.queries.append((sql, params))
        return FakeResult(self.handler(sql, params))

    def ran(self, prefix):
        return [p for s, p in self.queries if s.startswith(prefix)]


class FakeUniqueViolation(errors.UniqueViolation):
    """Ошибка уникальности с заданным именем ограничения (в настоящей его заполняет Postgres)."""

    def __init__(self, constraint):
        super().__init__("duplicate key")
        self._constraint = constraint

    @property
    def diag(self):
        return SimpleNamespace(constraint_name=self._constraint)


def logged_in(extra=None):
    """БД, в которой токен BEARER принадлежит пользователю 1."""

    def handler(sql, params):
        if "FROM user_sessions" in sql:
            return (1, "test_user")
        return extra(sql, params) if extra else None

    return FakeConn(handler)


def use_db(monkeypatch, module, conn):
    monkeypatch.setattr(module, "connect", lambda: conn)


def connect_handler(board=("board-1", True), existing=None, inserted=(EXPIRES, 3600)):
    def handler(sql, params):
        if sql.startswith("SELECT b.board_code"):
            return board
        if sql.startswith("SELECT expires_at"):
            return existing
        if sql.startswith("INSERT"):
            if isinstance(inserted, Exception):
                raise inserted
            return inserted
        return None  # DELETE

    return handler


# ---------- доступ только после входа ----------

@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/boards"),
        ("get", "/boards/current"),
        ("post", "/boards/1/connect"),
        ("post", "/boards/1/disconnect"),
    ],
)
def test_requires_login(method, path):
    assert getattr(client, method)(path).status_code == 401


# ---------- список ----------

def test_list_boards_statuses(monkeypatch):
    rows = [
        (1, "board-1", True, True, None, 0),    # свободна (GREATEST в SQL даёт 0, а не NULL)
        (2, "board-2", True, True, 7, 1800),    # занята другим
        (3, "board-3", True, True, 1, 600),     # занята мной
        (4, "board-4", False, True, None, 0),   # включена, но недоступна
        (5, "board-5", True, True, None, 0),    # отключена, но доступна по БД
        (6, "board-6", True, True, None, 0),    # не указана в hosts.json
    ]
    use_db(monkeypatch, deps, logged_in())
    use_db(monkeypatch, boards, FakeConn(lambda sql, params: rows))
    r = client.get("/boards", headers=BEARER)
    assert r.status_code == 200
    data = r.json()
    assert [b["status"] for b in data] == ["free", "busy", "busy", "unavailable", "unavailable", "unavailable"]
    assert [b["busy_by_me"] for b in data] == [False, False, True, False, False, False]
    assert [b["seconds_left"] for b in data] == [None, 1800, 600, None, None, None]
    assert [b["enabled"] for b in data] == [True, True, True, True, False, False]
    assert [b["is_available"] for b in data] == [True, True, True, False, True, True]
    # Наружу не уходит лишнее: ни адреса станций, ни чужие id
    for b in data:
        assert set(b) == {"board_id", "board_code", "enabled", "is_available", "status", "busy_by_me", "seconds_left"}


def test_list_boards_pagination_is_passed_to_db(monkeypatch):
    conn = FakeConn(lambda sql, params: [])
    use_db(monkeypatch, deps, logged_in())
    use_db(monkeypatch, boards, conn)
    assert client.get("/boards?offset=10&limit=5", headers=BEARER).status_code == 200
    assert conn.queries[0][1] == (10, 5)


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "limit=abc"])
def test_list_boards_rejects_bad_pagination(monkeypatch, query):
    use_db(monkeypatch, deps, logged_in())
    assert client.get(f"/boards?{query}", headers=BEARER).status_code == 422


def test_list_boards_db_down(monkeypatch):
    use_db(monkeypatch, deps, logged_in())
    use_db(monkeypatch, boards, FakeConn(error=psycopg.OperationalError("down")))
    assert client.get("/boards", headers=BEARER).status_code == 503


# ---------- подключение ----------

def test_connect_ok(monkeypatch):
    conn = FakeConn(connect_handler())
    use_db(monkeypatch, deps, logged_in())
    use_db(monkeypatch, boards, conn)
    r = client.post("/boards/1/connect", headers=BEARER)
    assert r.status_code == 200
    body = r.json()
    assert body["board_id"] == 1
    assert body["board_code"] == "board-1"
    assert body["seconds_left"] == 3600
    assert body["expires_at"].startswith("2026-11-23T12:00:00")
    assert conn.ran("DELETE")  # просроченные сессии убраны
    assert conn.ran("INSERT")[0] == (1, 1, boards.BOARD_SESSION_MINUTES)


def test_connect_again_returns_existing_session(monkeypatch):
    conn = FakeConn(connect_handler(existing=(EXPIRES, 1200)))
    use_db(monkeypatch, deps, logged_in())
    use_db(monkeypatch, boards, conn)
    r = client.post("/boards/1/connect", headers=BEARER)
    assert r.status_code == 200
    assert r.json()["seconds_left"] == 1200
    assert not conn.ran("INSERT")  # новая сессия не создаётся


def test_connect_unknown_board(monkeypatch):
    use_db(monkeypatch, deps, logged_in())
    use_db(monkeypatch, boards, FakeConn(connect_handler(board=None)))
    assert client.post("/boards/1/connect", headers=BEARER).status_code == 404


def test_connect_unavailable_board(monkeypatch):
    conn = FakeConn(connect_handler(board=("board-1", False)))
    use_db(monkeypatch, deps, logged_in())
    use_db(monkeypatch, boards, conn)
    assert client.post("/boards/1/connect", headers=BEARER).status_code == 409
    assert not conn.ran("INSERT")


def test_connect_board_already_taken(monkeypatch):
    inserted = FakeUniqueViolation("board_sessions_unique")
    use_db(monkeypatch, deps, logged_in())
    use_db(monkeypatch, boards, FakeConn(connect_handler(inserted=inserted)))
    r = client.post("/boards/1/connect", headers=BEARER)
    assert r.status_code == 409
    assert "занята" in r.json()["detail"]


def test_connect_user_already_on_another_board(monkeypatch):
    inserted = FakeUniqueViolation("board_sessions_unique_user")
    use_db(monkeypatch, deps, logged_in())
    use_db(monkeypatch, boards, FakeConn(connect_handler(inserted=inserted)))
    r = client.post("/boards/1/connect", headers=BEARER)
    assert r.status_code == 409
    assert "другой плате" in r.json()["detail"]


def test_connect_db_down(monkeypatch):
    use_db(monkeypatch, deps, logged_in())
    use_db(monkeypatch, boards, FakeConn(error=psycopg.OperationalError("down")))
    assert client.post("/boards/1/connect", headers=BEARER).status_code == 503


@pytest.mark.parametrize("board_id", ["0", "-1", "abc", "99999999999"])
def test_connect_rejects_bad_board_id(monkeypatch, board_id):
    use_db(monkeypatch, deps, logged_in())
    assert client.post(f"/boards/{board_id}/connect", headers=BEARER).status_code == 422


# ---------- отключение ----------

def test_disconnect_removes_only_own_session(monkeypatch):
    conn = FakeConn()
    use_db(monkeypatch, deps, logged_in())
    use_db(monkeypatch, boards, conn)
    r = client.post("/boards/5/disconnect", headers=BEARER)
    assert r.status_code == 204
    assert conn.ran("DELETE")[0] == (5, 1)  # (плата, пользователь из токена)


def test_disconnect_db_down(monkeypatch):
    use_db(monkeypatch, deps, logged_in())
    use_db(monkeypatch, boards, FakeConn(error=psycopg.OperationalError("down")))
    assert client.post("/boards/5/disconnect", headers=BEARER).status_code == 503


# ---------- текущая сессия ----------

def test_current_board_ok(monkeypatch):
    def extra(sql, params):
        if "FROM board_sessions" in sql:
            return (5, 2, "board-2", EXPIRES, 900)

    use_db(monkeypatch, deps, logged_in(extra))
    r = client.get("/boards/current", headers=BEARER)
    assert r.status_code == 200
    assert r.json()["board_id"] == 2
    assert r.json()["seconds_left"] == 900


def test_current_board_when_not_connected(monkeypatch):
    use_db(monkeypatch, deps, logged_in())
    assert client.get("/boards/current", headers=BEARER).status_code == 403
