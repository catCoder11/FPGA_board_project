"""Сессии пользователей: таблица user_sessions.

Клиент получает сам токен, а в БД лежит только его sha256 (token_hash).
Если база утечёт, по хешам войти в систему нельзя.
"""
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from app.db import connect

# Сколько живёт сессия после входа (по умолчанию 12 часов)
SESSION_TTL = timedelta(hours=int(os.environ.get("SESSION_TTL_HOURS", "12")))


def new_token() -> str:
    """Случайный токен, 32 байта энтропии (в тексте около 43 символов)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    """sha256 токена в виде bytes — колонка user_sessions.token_hash имеет тип bytea."""
    return hashlib.sha256(token.encode()).digest()


def create_session(conn, user_id: int) -> tuple[str, datetime]:
    """Создаёт сессию в уже открытом соединении. Возвращает (токен, время истечения)."""
    token = new_token()
    expires_at = datetime.now(timezone.utc) + SESSION_TTL
    conn.execute(
        "INSERT INTO user_sessions (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
        (user_id, hash_token(token), expires_at),
    )
    return token, expires_at


def get_user_id(token: str) -> int | None:
    """id пользователя по токену.

    None, если токен неизвестен, просрочен или пользователь отключён.
    """
    with connect() as conn:
        row = conn.execute(
            """
            SELECT s.user_id
            FROM user_sessions s
            JOIN users u ON u.user_id = s.user_id
            WHERE s.token_hash = %s AND s.expires_at > now() AND u.is_active
            """,
            (hash_token(token),),
        ).fetchone()
    return row[0] if row else None


def delete_session(token: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM user_sessions WHERE token_hash = %s", (hash_token(token),))