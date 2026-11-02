"""Проверяют, что запущенные контейнеры отвечают. Нужен docker compose up."""

import os
import time

import httpx2
import psycopg

BASE_URL = os.environ["SMOKE_BASE_URL"]
STARTUP_TIMEOUT_SECONDS = 30
RETRY_DELAY_SECONDS = 1
REQUEST_TIMEOUT_SECONDS = 5
DB_CONNECT_TIMEOUT_SECONDS = 5
DB_PING_VALUE = 1


def test_backend_responds():
    # Контейнер может быть запущен раньше, чем uvicorn начнёт принимать запросы
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while True:
        try:
            response = httpx2.get(f"{BASE_URL}/openapi.json", timeout=REQUEST_TIMEOUT_SECONDS)
            break
        except httpx2.TransportError:
            if time.monotonic() > deadline:
                raise
            time.sleep(RETRY_DELAY_SECONDS)

    assert response.status_code == httpx2.codes.OK


def test_database_responds():
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=DB_CONNECT_TIMEOUT_SECONDS) as conn:
        row = conn.execute("select %s::int", (DB_PING_VALUE,)).fetchone()

    assert row == (DB_PING_VALUE,)
