import logging
import os

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from psycopg import errors
from pydantic import BaseModel, Field

from app.db import connect
from app.deps import CurrentUser, get_current_user
from app.ratelimit import FailureLimiter
from app.security import hash_password, new_session_token, verify_password

router = APIRouter()
log = logging.getLogger(__name__)

# Сколько живёт токен входа. Можно переопределить переменной окружения.
SESSION_TTL_MINUTES = int(os.environ.get("SESSION_TTL_MINUTES", "720"))

# Не больше 5 неудач за 5 минут для пары (IP, логин) и 30 для одного IP
login_limiter = FailureLimiter(max_failures=5, window_seconds=300)
ip_limiter = FailureLimiter(max_failures=30, window_seconds=300)

# Хеш для несуществующего пользователя: чтобы ответ «нет такого логина»
# занимал столько же времени, сколько «неверный пароль»
_DUMMY_HASH = hash_password("dummy-password-for-timing-only")


class RegisterRequest(BaseModel):
    login: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=8, max_length=128)


class RegisterResponse(BaseModel):
    user_id: int
    login: str


class LoginRequest(BaseModel):
    login: str = Field(min_length=1, max_length=64)
    # Верхний предел защищает от запросов с огромным паролем: Argon2 на них тратит ресурсы
    password: str = Field(min_length=1, max_length=128)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # секунды


class MeResponse(BaseModel):
    user_id: int
    login: str


# Обычные функции (не async): FastAPI сам запускает такие в отдельном потоке,
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


@router.post("/login", response_model=LoginResponse)
def sign_in(data: LoginRequest, request: Request, response: Response) -> LoginResponse:
    login = data.login.lower()
    ip = request.client.host if request.client else "unknown"
    pair_key = f"{ip}|{login}"

    # 1. Не слишком ли много неудач. Проверка идёт до пароля, поэтому
    #    во время блокировки даже верный пароль не принимается.
    wait = max(login_limiter.retry_after(pair_key), ip_limiter.retry_after(ip))
    if wait:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Слишком много неудачных попыток входа, попробуйте позже",
            headers={"Retry-After": str(wait)},
        )

    # 2. Ищем пользователя
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT user_id, password_hash, is_active FROM users WHERE login = %s",
                (login,),
            ).fetchone()
    except psycopg.Error:
        log.exception("Ошибка БД при входе")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "База данных недоступна")

    # 3. Проверяем пароль. Хеш считается и для несуществующего логина.
    if row is None:
        verify_password(data.password, _DUMMY_HASH)
        password_ok = False
    else:
        user_id, password_hash, is_active = row
        password_ok = verify_password(data.password, password_hash)

    if not password_ok:
        login_limiter.register_failure(pair_key)
        ip_limiter.register_failure(ip)
        # Одинаковый ответ для «нет логина» и «неверный пароль»: нельзя узнать, кто зарегистрирован
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный логин или пароль")

    if not is_active:
        # Дошли сюда только с верным паролем, поэтому честное сообщение ничего не раскрывает
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Учётная запись отключена")

    # 4. Выдаём токен. В БД попадает только его хеш.
    token, token_hash = new_session_token()
    try:
        with connect() as conn:  # обе команды — одна транзакция
            conn.execute(
                "DELETE FROM user_sessions WHERE user_id = %s AND expires_at <= now()",
                (user_id,),
            )
            conn.execute(
                "INSERT INTO user_sessions (user_id, token_hash, expires_at) "
                "VALUES (%s, %s, now() + make_interval(mins => %s::int))",
                (user_id, token_hash, SESSION_TTL_MINUTES),
            )
    except psycopg.Error:
        log.exception("Ошибка БД при создании сессии")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "База данных недоступна")

    login_limiter.reset(pair_key)
    response.headers["Cache-Control"] = "no-store"  # токен не должен кэшироваться
    return LoginResponse(access_token=token, expires_in=SESSION_TTL_MINUTES * 60)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def sign_out(user: CurrentUser = Depends(get_current_user)) -> None:
    """Выход: токен перестаёт работать сразу, а не когда истечёт срок."""
    try:
        with connect() as conn:
            conn.execute("DELETE FROM user_sessions WHERE token_hash = %s", (user.token_hash,))
    except psycopg.Error:
        log.exception("Ошибка БД при выходе")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "База данных недоступна")


@router.get("/me", response_model=MeResponse)
def me(user: CurrentUser = Depends(get_current_user)) -> MeResponse:
    """Кто я. Удобно для проверки токена и для фронтенда."""
    return MeResponse(user_id=user.user_id, login=user.login)