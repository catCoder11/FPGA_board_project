from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# Argon2 — современный алгоритм хеширования паролей. Соль генерируется сама
# и хранится внутри хеша, отдельной колонки для неё не нужно.
_hasher = PasswordHasher()


def hash_password(password: str) -> bytes:
    """Хеш пароля в виде bytes — колонка users.password_hash имеет тип bytea."""
    return _hasher.hash(password).encode()


def verify_password(password: str, password_hash: bytes) -> bool:
    """Нужна для логина: сравнивает пароль с сохранённым хешем."""
    try:
        return _hasher.verify(bytes(password_hash).decode(), password)
    except (VerificationError, InvalidHashError):
        return False