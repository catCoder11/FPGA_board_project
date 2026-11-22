import logging
from dataclasses import dataclass

import psycopg
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import connect
from app.security import hash_token

log = logging.getLogger(__name__)

# auto_error=False: ошибку 401 формируем сами, с правильным заголовком WWW-Authenticate
bearer = HTTPBearer(auto_error=False)
MAX_TOKEN_LENGTH = 128  # настоящий токен — 43 символа, всё длиннее отбрасываем сразу


@dataclass
class CurrentUser:
    user_id: int
    login: str
    token_hash: bytes


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Требуется вход в систему",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> CurrentUser:
    """Проверяет заголовок `Authorization: Bearer <токен>` и возвращает пользователя.

    Подключается к любому эндпоинту, который доступен только после входа:
    `user: CurrentUser = Depends(get_current_user)`.
    """
    if creds is None or not creds.credentials or len(creds.credentials) > MAX_TOKEN_LENGTH:
        raise _unauthorized()

    token_hash = hash_token(creds.credentials)
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT u.user_id, u.login "
                "FROM user_sessions s JOIN users u ON u.user_id = s.user_id "
                "WHERE s.token_hash = %s AND s.expires_at > now() AND u.is_active",
                (token_hash,),
            ).fetchone()
    except psycopg.Error:
        log.exception("Ошибка БД при проверке токена")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "База данных недоступна")

    if row is None:
        # Токена нет, он просрочен, пользователь отключён — снаружи это неразличимо
        raise _unauthorized()
    return CurrentUser(user_id=row[0], login=row[1], token_hash=token_hash)