import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# Argon2 — современный алгоритм хеширования паролей. Соль генерируется сама
# и хранится внутри хеша, отдельной колонки для неё не нужно.
_hasher = PasswordHasher()


def hash_password(password: str) -> bytes:
    """Хеш пароля в виде bytes — колонка users.password_hash имеет тип bytea."""
    return _hasher.hash(password).encode()


def verify_password(password: str, password_hash: bytes) -> bool:
    """Сравнивает пароль с сохранённым хешем."""
    try:
        return _hasher.verify(bytes(password_hash).decode(), password)
    except (VerificationError, InvalidHashError):
        return False


def hash_token(token: str) -> bytes:
    """SHA-256 от токена. В БД хранится только он, сам токен знает лишь пользователь.

    Для паролей так делать нельзя (они короткие и угадываются), а токен — это
    256 случайных бит, перебрать его невозможно, поэтому быстрого хеша достаточно.
    """
    return hashlib.sha256(token.encode()).digest()


def new_session_token() -> tuple[str, bytes]:
    """Возвращает (токен для пользователя, его хеш для БД)."""
    token = secrets.token_urlsafe(32)  # 32 случайных байта = 256 бит
    return token, hash_token(token)