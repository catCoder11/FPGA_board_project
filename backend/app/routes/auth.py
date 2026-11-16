import logging

import psycopg
from fastapi import APIRouter, HTTPException, status
from psycopg import errors
from pydantic import BaseModel, Field

from app.db import connect
from app.security import hash_password

router = APIRouter()
log = logging.getLogger(__name__)


class RegisterRequest(BaseModel):
    login: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=8, max_length=128)


class RegisterResponse(BaseModel):
    user_id: int
    login: str


# Обычная функция (не async): FastAPI сам запускает такие в отдельном потоке,
# поэтому блокирующий psycopg и тяжёлое хеширование не вешают весь сервер.
@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
def register(data: RegisterRequest) -> RegisterResponse:
    # Логин храним в нижнем регистре, чтобы "Ivan" и "ivan" не были двумя разными людьми
    login = data.login.lower()
    password_hash = hash_password(data.password)

    try:
        with connect() as conn:
            row = conn.execute(
                "INSERT INTO users (login, password_hash) VALUES (%s, %s) RETURNING user_id",
                (login, password_hash),
            ).fetchone()
    except errors.UniqueViolation:
        # Сработало ограничение users_unique_login: такой логин уже есть.
        # Проверку и вставку делает одна команда, поэтому гонки двух запросов нет.
        raise HTTPException(status.HTTP_409_CONFLICT, "Пользователь с таким логином уже существует")
    except psycopg.Error:
        log.exception("Ошибка БД при регистрации")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "База данных недоступна")

    return RegisterResponse(user_id=row[0], login=login)