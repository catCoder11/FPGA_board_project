"""Тесты регистрации. База данных не нужна: соединение подменяется заглушкой."""
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors

from app.main import app
from app.routes import auth
from app.security import hash_password, verify_password

client = TestClient(app)
VALID = {"login": "Test_User", "password": "strongpass123"}


class FakeConn:
    """Подменяет psycopg-соединение: либо отдаёт строку, либо бросает ошибку."""

    def __init__(self, error=None):
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, *args):
        if self.error:
            raise self.error
        return self

    def fetchone(self):
        return (42,)


def test_hash_and_verify_password():
    hashed = hash_password("strongpass123")
    assert isinstance(hashed, bytes)
    assert b"strongpass123" not in hashed
    assert verify_password("strongpass123", hashed)
    assert not verify_password("wrongpass123", hashed)


@pytest.mark.parametrize(
    "payload",
    [
        {"login": "ab", "password": "strongpass123"},         # логин слишком короткий
        {"login": "bad login", "password": "strongpass123"},  # пробел в логине
        {"login": "user_1", "password": "short"},             # пароль слишком короткий
        {"login": "user_1"},                                  # нет пароля
    ],
)
def test_register_validation(payload):
    assert client.post("/register", json=payload).status_code == 422


def test_register_ok(monkeypatch):
    monkeypatch.setattr(auth, "connect", lambda: FakeConn())
    r = client.post("/register", json=VALID)
    assert r.status_code == 201
    assert r.json() == {"user_id": 42, "login": "test_user"}  # логин приведён к нижнему регистру
    assert "password" not in r.text


def test_register_duplicate(monkeypatch):
    monkeypatch.setattr(auth, "connect", lambda: FakeConn(errors.UniqueViolation("dup")))
    assert client.post("/register", json=VALID).status_code == 409


def test_register_db_down(monkeypatch):
    monkeypatch.setattr(auth, "connect", lambda: FakeConn(psycopg.OperationalError("down")))
    assert client.post("/register", json=VALID).status_code == 503

