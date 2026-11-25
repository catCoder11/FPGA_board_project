"""Тесты входа, выхода и защиты токеном. БД не нужна: соединение подменяется заглушкой."""
import psycopg
import pytest
from fastapi.testclient import TestClient

from app import deps
from app.main import app
from app.ratelimit import FailureLimiter
from app.routes import auth
from app.security import hash_password, hash_token, new_session_token

client = TestClient(app)
PASSWORD = "strongpass123"
PASSWORD_HASH = hash_password(PASSWORD)  # считаем один раз: Argon2 намеренно медленный
BEARER = {"Authorization": "Bearer sometoken"}


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConn:
    """Подменяет psycopg-соединение. handler решает, что вернуть на запрос."""

    def __init__(self, handler=None, error=None):
        self.handler = handler or (lambda sql, params: None)
        self.error = error
        self.queries = []  # сюда складываются все выполненные запросы

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        if self.error:
            raise self.error
        self.queries.append((" ".join(sql.split()), params))
        return FakeResult(self.handler(sql, params))


def user_row(is_active=True):
    """БД, где есть пользователь с паролем PASSWORD."""

    def handler(sql, params):
        if sql.lstrip().upper().startswith("SELECT"):
            return (1, PASSWORD_HASH, is_active)
        return None

    return FakeConn(handler)


def session_row(row):
    """БД для проверки токена: на любой запрос отдаёт row."""
    return FakeConn(lambda sql, params: row)


def use_db(monkeypatch, conn, *modules):
    for module in modules:
        monkeypatch.setattr(module, "connect", lambda: conn)


@pytest.fixture(autouse=True)
def clean_limiters():
    auth.login_limiter.clear()
    auth.ip_limiter.clear()


# ---------- вход ----------

def test_login_ok(monkeypatch):
    conn = user_row()
    use_db(monkeypatch, conn, auth)
    r = client.post("/login", json={"login": "Test_User", "password": PASSWORD})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert len(body["access_token"]) >= 40
    assert r.headers["cache-control"] == "no-store"
    insert = next(p for s, p in conn.queries if s.startswith("INSERT"))
    assert insert[1] == hash_token(body["access_token"])  # в БД уходит хеш
    assert body["access_token"] not in str(conn.queries)  # а сам токен — нет


def test_login_same_error_for_wrong_password_and_unknown_user(monkeypatch):
    use_db(monkeypatch, user_row(), auth)
    wrong_password = client.post("/login", json={"login": "test_user", "password": "wrongpass123"})
    use_db(monkeypatch, FakeConn(), auth)  # пользователя в базе нет
    unknown_user = client.post("/login", json={"login": "nobody", "password": PASSWORD})
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()


def test_login_inactive_user(monkeypatch):
    use_db(monkeypatch, user_row(is_active=False), auth)
    r = client.post("/login", json={"login": "test_user", "password": PASSWORD})
    assert r.status_code == 403


def test_login_rate_limit(monkeypatch):
    use_db(monkeypatch, user_row(), auth)
    for _ in range(5):
        r = client.post("/login", json={"login": "test_user", "password": "wrongpass123"})
        assert r.status_code == 401
    r = client.post("/login", json={"login": "test_user", "password": PASSWORD})  # пароль верный
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0


def test_login_db_down(monkeypatch):
    use_db(monkeypatch, FakeConn(error=psycopg.OperationalError("down")), auth)
    r = client.post("/login", json={"login": "test_user", "password": PASSWORD})
    assert r.status_code == 503


def test_login_validation():
    assert client.post("/login", json={"login": "test_user", "password": ""}).status_code == 422
    assert client.post("/login", json={"login": "test_user"}).status_code == 422


# ---------- защита токеном ----------

def test_me_ok(monkeypatch):
    use_db(monkeypatch, session_row((1, "test_user")), deps)
    r = client.get("/me", headers=BEARER)
    assert r.status_code == 200
    assert r.json() == {"user_id": 1, "login": "test_user"}


def test_me_without_token():
    r = client.get("/me")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_me_unknown_or_expired_token(monkeypatch):
    use_db(monkeypatch, session_row(None), deps)
    assert client.get("/me", headers=BEARER).status_code == 401


def test_me_too_long_token():
    r = client.get("/me", headers={"Authorization": "Bearer " + "a" * 500})
    assert r.status_code == 401


def test_me_db_down(monkeypatch):
    use_db(monkeypatch, FakeConn(error=psycopg.OperationalError("down")), deps)
    assert client.get("/me", headers=BEARER).status_code == 503


def test_logout_deletes_session(monkeypatch):
    conn = session_row((1, "test_user"))
    use_db(monkeypatch, conn, deps, auth)
    r = client.post("/logout", headers=BEARER)
    assert r.status_code == 204
    delete = next(p for s, p in conn.queries if s.startswith("DELETE"))
    assert delete[0] == hash_token("sometoken")


# ---------- токены и ограничитель ----------

def test_new_session_token_is_random_and_hashed():
    token1, hash1 = new_session_token()
    token2, _ = new_session_token()
    assert token1 != token2
    assert len(token1) >= 40
    assert hash1 == hash_token(token1)
    assert len(hash1) == 32


def test_limiter_blocks_and_then_expires():
    now = [0.0]
    limiter = FailureLimiter(max_failures=2, window_seconds=60, clock=lambda: now[0])
    assert limiter.retry_after("k") == 0
    limiter.register_failure("k")
    limiter.register_failure("k")
    assert 0 < limiter.retry_after("k") <= 61
    now[0] = 61
    assert limiter.retry_after("k") == 0


def test_limiter_reset():
    limiter = FailureLimiter(max_failures=1, window_seconds=60)
    limiter.register_failure("k")
    assert limiter.retry_after("k") > 0
    limiter.reset("k")
    assert limiter.retry_after("k") == 0