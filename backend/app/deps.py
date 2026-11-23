import logging
from dataclasses import dataclass
from datetime import datetime

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


@dataclass
class ActiveBoardSession:
    board_session_id: int
    board_id: int
    board_code: str
    expires_at: datetime
    seconds_left: int


def get_board_session(user: CurrentUser = Depends(get_current_user)) -> ActiveBoardSession:
    """Пользователь вошёл И сейчас подключён к плате (срок сессии не истёк).

    Подключается к эндпоинтам «работы с платой» (прошивка, камера): так
    нельзя прошить плату, которую пользователь не занимал или время которой вышло.
    """
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT bs.board_session_id, bs.board_id, b.board_code, bs.expires_at, "
                "EXTRACT(EPOCH FROM (bs.expires_at - now()))::int "
                "FROM board_sessions bs JOIN boards b ON b.board_id = bs.board_id "
                "WHERE bs.user_id = %s AND bs.expires_at > now() AND b.is_enabled",
                (user.user_id,),
            ).fetchone()
    except psycopg.Error:
        log.exception("Ошибка БД при проверке сессии платы")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "База данных недоступна")

    if row is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Сначала подключитесь к плате")
    return ActiveBoardSession(*row)