import importlib.util

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Клиент для запросов к приложению без запуска сервера."""
    if importlib.util.find_spec("app.main") is None:
        pytest.skip("app/main.py ещё не написан")

    from app.main import app

    return TestClient(app)
